import os
import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import aiohttp
import aiosqlite

# ==================== ЗАГРУЗКА ПЕРЕМЕННЫХ ====================
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
CRYPTOBOT_API_KEY = os.getenv('CRYPTOBOT_API_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
PRODUCT_LINK = os.getenv('PRODUCT_LINK')
PRICE = float(os.getenv('PRICE_AMOUNT'))
REWARD = float(os.getenv('REFERRAL_REWARD'))
CURRENCY = os.getenv('CURRENCY')
MIN_WITHDRAWAL = float(os.getenv('MIN_WITHDRAWAL'))

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ==================== СОСТОЯНИЯ (FSM) ====================
class WalletState(StatesGroup):
    waiting_for_wallet = State()

class WithdrawState(StatesGroup):
    waiting_for_amount = State()
    waiting_for_username = State()

# ==================== БАЗА ДАННЫХ ====================
async def init_db():
    async with aiosqlite.connect('users.db') as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            referrer_id INTEGER,
            is_paid BOOLEAN DEFAULT 0,
            wallet_username TEXT,
            balance REAL DEFAULT 0.0,
            joined_at TEXT
        )''')
        
        await db.execute('''CREATE TABLE IF NOT EXISTS payments (
            invoice_id TEXT PRIMARY KEY,
            user_id INTEGER,
            amount REAL,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            updated_at TEXT
        )''')
        
        await db.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            wallet_address TEXT,
            status TEXT DEFAULT 'pending',
            tx_hash TEXT,
            created_at TEXT,
            updated_at TEXT
        )''')
        
        await db.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        
        await db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', 
                         ('product_link', PRODUCT_LINK))
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect('users.db') as db:
        cursor = await db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        return await cursor.fetchone()

async def add_user(user_id, referrer_id=None):
    async with aiosqlite.connect('users.db') as db:
        await db.execute('INSERT OR IGNORE INTO users (user_id, referrer_id, joined_at) VALUES (?, ?, ?)', 
                         (user_id, referrer_id, datetime.now().isoformat()))
        await db.commit()

async def set_wallet(user_id, wallet):
    async with aiosqlite.connect('users.db') as db:
        await db.execute('UPDATE users SET wallet_username = ? WHERE user_id = ?', (wallet, user_id))
        await db.commit()

async def update_balance(user_id, amount):
    async with aiosqlite.connect('users.db') as db:
        await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
        await db.commit()

async def mark_paid(user_id):
    async with aiosqlite.connect('users.db') as db:
        await db.execute('UPDATE users SET is_paid = 1 WHERE user_id = ?', (user_id,))
        await db.commit()

async def get_payment(invoice_id):
    async with aiosqlite.connect('users.db') as db:
        cursor = await db.execute('SELECT * FROM payments WHERE invoice_id = ?', (invoice_id,))
        return await cursor.fetchone()

async def save_payment(invoice_id, user_id, amount, status='pending'):
    async with aiosqlite.connect('users.db') as db:
        now = datetime.now().isoformat()
        await db.execute('''INSERT OR REPLACE INTO payments 
                           (invoice_id, user_id, amount, status, created_at, updated_at) 
                           VALUES (?, ?, ?, ?, ?, ?)''', 
                         (invoice_id, user_id, amount, status, now, now))
        await db.commit()

async def update_payment_status(invoice_id, status):
    async with aiosqlite.connect('users.db') as db:
        await db.execute('UPDATE payments SET status = ?, updated_at = ? WHERE invoice_id = ?', 
                         (status, datetime.now().isoformat(), invoice_id))
        await db.commit()

async def get_referrer(user_id):
    async with aiosqlite.connect('users.db') as db:
        cursor = await db.execute('SELECT referrer_id FROM users WHERE user_id = ?', (user_id,))
        result = await cursor.fetchone()
        return result[0] if result else None

async def save_withdrawal(user_id, amount, wallet, status='pending', tx_hash=None):
    async with aiosqlite.connect('users.db') as db:
        now = datetime.now().isoformat()
        await db.execute('''INSERT INTO withdrawals 
                           (user_id, amount, wallet_address, status, tx_hash, created_at, updated_at) 
                           VALUES (?, ?, ?, ?, ?, ?, ?)''',
                         (user_id, amount, wallet, status, tx_hash, now, now))
        
        if status == 'completed':
            await db.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, user_id))
        await db.commit()

async def get_product_link():
    async with aiosqlite.connect('users.db') as db:
        cursor = await db.execute('SELECT value FROM settings WHERE key = ?', ('product_link',))
        result = await cursor.fetchone()
        return result[0] if result else PRODUCT_LINK

async def update_product_link(new_link):
    async with aiosqlite.connect('users.db') as db:
        await db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', 
                         ('product_link', new_link))
        await db.commit()

async def get_all_paid_users():
    async with aiosqlite.connect('users.db') as db:
        cursor = await db.execute('SELECT user_id FROM users WHERE is_paid = ?', (1,))
        return await cursor.fetchall()

# ==================== CRYPTOBOT API ====================
async def create_invoice(user_id):
    """Создание счета в CryptoBot"""
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"X-Crypto-Api-Key": CRYPTOBOT_API_KEY}
    order_id = f"order_{user_id}_{int(datetime.now().timestamp())}"
    data = {
        "amount": str(PRICE),
        "currency": CURRENCY,
        "invoice_id": order_id,
        "description": f"Оплата продукта (User: {user_id})",
        "paid_btn_name": "Вернуться в бот",
        "paid_btn_url": f"https://t.me/{bot.username}",
        "payload": str(user_id)
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            return await response.json(), order_id

async def check_invoice_status(invoice_id):
    """Проверка статуса счета в CryptoBot"""
    url = "https://pay.crypt.bot/api/getInvoiceInfo"
    headers = {"X-Crypto-Api-Key": CRYPTOBOT_API_KEY}
    data = {"invoice_id": invoice_id}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            return await response.json()

async def create_payout(username, amount):
    """Выплата через CryptoBot (ТОЛЬКО @username)"""
    url = "https://pay.crypt.bot/api/transfer"
    headers = {"X-Crypto-Api-Key": CRYPTOBOT_API_KEY}
    
    clean_username = username.replace('@', '')
    
    data = {
        "username": clean_username,
        "amount": str(amount),
        "currency": CURRENCY,
        "comment": "Реферальная выплата",
        "disable_send_notification": False
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            return await response.json()

# ==================== ФОНОВАЯ ПРОВЕРКА ПЛАТЕЖЕЙ (POLLING) ====================
async def check_pending_payments_periodically():
    """Фоновая проверка неоплаченных счетов каждые 30 секунд"""
    logging.info("🔄 Запущена фоновая проверка платежей...")
    
    while True:
        try:
            await asyncio.sleep(30)  # Ждём 30 секунд
            
            async with aiosqlite.connect('users.db') as db:
                # Берем все неоплаченные счета за последние 24 часа
                cursor = await db.execute(
                    "SELECT invoice_id, user_id, amount FROM payments WHERE status = 'pending'"
                )
                pending = await cursor.fetchall()
            
            for invoice_id, user_id, amount in pending:
                # Проверяем статус в CryptoBot
                status_data = await check_invoice_status(invoice_id)
                
                if status_data.get('ok'):
                    result = status_data.get('result', {})
                    bot_status = result.get('status', '')
                    
                    # Если оплата подтверждена
                    if bot_status == 'paid':
                        payment = await get_payment(invoice_id)
                        if payment and payment[3] != 'paid':  # Если ещё не обработано
                            await update_payment_status(invoice_id, 'paid')
                            await mark_paid(user_id)
                            
                            product_link = await get_product_link()
                            
                            try:
                                await bot.send_message(user_id, 
                                    f"✅ **ОПЛАТА ПОДТВЕРЖДЕНА!**\n\n"
                                    f"💰 Сумма: {amount} {CURRENCY}\n"
                                    f"📦 Продукт активирован!\n\n"
                                    f"Нажмите кнопку **📚 Продукт** в меню, чтобы получить доступ.",
                                    parse_mode="Markdown"
                                )
                            except Exception as e:
                                logging.error(f"Не удалось отправить уведомление: {e}")
                            
                            await bot.send_message(ADMIN_ID,
                                f"💎 **НОВАЯ ОПЛАТА!**\n\n"
                                f"👤 User ID: `{user_id}`\n"
                                f"💰 Сумма: {amount} {CURRENCY}\n"
                                f"🔗 Invoice: `{invoice_id}`",
                                parse_mode="Markdown"
                            )
                            
                            # Начисление рефереру
                            referrer_id = await get_referrer(user_id)
                            if referrer_id:
                                ref_user = await get_user(referrer_id)
                                if ref_user and ref_user[2]:
                                    await update_balance(referrer_id, REWARD)
                                    new_balance = (ref_user[4] if ref_user[4] else 0.0) + REWARD
                                    
                                    try:
                                        await bot.send_message(referrer_id,
                                            f"🎉 **ПО РЕФЕРАЛЬНОЙ ССЫЛКЕ КУПИЛИ!**\n\n"
                                            f"💰 Вам начислено: +${REWARD} USDT\n"
                                            f"📊 Текущий баланс: ${new_balance:.2f} USDT\n\n"
                                            f"Приглашайте ещё больше людей!",
                                            parse_mode="Markdown"
                                        )
                                    except:
                                        pass
                            
                            logging.info(f"✅ Оплата {invoice_id} подтверждена через polling")
                    
                    # Если оплата истекла или отменена
                    elif bot_status in ['expired', 'failed']:
                        await update_payment_status(invoice_id, bot_status)
                        logging.info(f"❌ Платеж {invoice_id} отменён: {bot_status}")
                        
        except Exception as e:
            logging.error(f"Ошибка проверки платежей: {e}")
            await asyncio.sleep(60)  # При ошибке ждём минуту

# ==================== КЛАВИАТУРЫ ====================
def get_main_menu(is_paid, balance=0.0):
    """Главное меню в стиле CryptoBot (сетка 2xN)"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="📚 Продукт", callback_data="product")
    
    if is_paid:
        builder.button(text=f"💰 Баланс: ${balance:.2f}", callback_data="balance")
        builder.button(text="💸 Вывод средств", callback_data="withdraw_menu")
        builder.button(text="👥 Партнерская программа", callback_data="referral")
        builder.button(text="⚙️ Настройки", callback_data="settings")
        builder.button(text="📞 Поддержка", callback_data="support")
    else:
        builder.button(text="💳 Купить доступ", callback_data="buy")
        builder.button(text="👥 Партнерская программа", callback_data="referral_locked")
        builder.button(text="📞 Поддержка", callback_data="support")
    
    builder.adjust(2, 2, 2)
    return builder.as_markup()

def get_product_keyboard(product_link):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Открыть продукт", url=product_link)
    builder.button(text="🏠 В меню", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_withdraw_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Вывести", callback_data="withdraw_start")
    builder.button(text="🏠 В меню", callback_data="main_menu")
    builder.adjust(2)
    return builder.as_markup()

def get_referral_keyboard(ref_link):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Копировать ссылку", callback_data="copy_ref_link")
    builder.button(text="📊 Статистика", callback_data="ref_stats")
    builder.button(text="🏠 В меню", callback_data="main_menu")
    builder.adjust(2, 1)
    return builder.as_markup()

def get_help_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Канал с инструкциями", url="https://t.me/your_channel")
    builder.button(text="🏠 В меню", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

# ==================== ХЕНДЛЕРЫ БОТА ====================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    referrer_id = None
    
    args = message.text.split()
    if len(args) > 1 and args[1].startswith('ref_'):
        try:
            referrer_id = int(args[1].split('_')[1])
            if referrer_id == user_id:
                referrer_id = None
        except:
            referrer_id = None

    await add_user(user_id, referrer_id)
    user = await get_user(user_id)
    is_paid = user[2]
    balance = user[4] if user[4] else 0.0
    
    await message.answer(
        f"👋 **Добро пожаловать!**\n\n"
        f"{'✅ Доступ активен!' if is_paid else '🔒 Купите продукт для доступа'}\n\n"
        f"Используйте меню ниже:",
        parse_mode="Markdown",
        reply_markup=get_main_menu(is_paid, balance)
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        f"📖 **ИНСТРУКЦИЯ ПО ВЫПЛАТАМ**\n\n"
        f"Для получения реферальных выплат вам нужно:\n\n"
        f"1️⃣ Зайдите в @CryptoBot\n"
        f"2️⃣ Нажмите **Профиль** → **Изменить профиль**\n"
        f"3️⃣ Придумайте и установите Username (например: @myname)\n"
        f"4️⃣ Вернитесь в этот бот\n"
        f"5️⃣ Перейдите в **⚙️ Настройки** → **💳 Кошелек**\n"
        f"6️⃣ Введите ваш @username из CryptoBot\n\n"
        f"⚠️ **Важно:**\n"
        f"- Выплаты работают ТОЛЬКО через @username\n"
        f"- Адреса кошельков (TRC20, BEP20) НЕ принимаются\n"
        f"- Минимальная сумма вывода: ${MIN_WITHDRAWAL} USDT\n"
        f"- За каждого приглашенного: ${REWARD} USDT\n\n"
        f"📢 Больше инструкций в нашем канале!",
        parse_mode="Markdown",
        reply_markup=get_help_keyboard()
    )

@dp.callback_query(F.data == "main_menu")
async def main_menu(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    is_paid = user[2]
    balance = user[4] if user[4] else 0.0
    
    await callback.message.edit_text(
        f"🏠 **Главное меню**\n\n"
        f"{'✅ Доступ активен!' if is_paid else '🔒 Купите продукт для доступа'}\n"
        f"💰 Баланс: ${balance:.2f} USDT",
        parse_mode="Markdown",
        reply_markup=get_main_menu(is_paid, balance)
    )
    await callback.answer()

@dp.callback_query(F.data == "product")
async def product_handler(callback: types.CallbackQuery):
    product_link = await get_product_link()
    
    await callback.message.answer(
        f"📚 **Ваш продукт**\n\n"
        f"Нажмите кнопку ниже, чтобы открыть доступ:",
        parse_mode="Markdown",
        reply_markup=get_product_keyboard(product_link)
    )
    await callback.answer()

@dp.callback_query(F.data == "buy")
async def process_buy(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    invoice_data, order_id = await create_invoice(user_id)
    
    if invoice_data.get('ok'):
        url = invoice_data['result']['invoice_url']
        await save_payment(order_id, user_id, PRICE, 'pending')
        
        await callback.message.answer(
            f"💳 **Счет создан!**\n\n"
            f"💰 Сумма: ${PRICE} USDT\n"
            f"⏳ Статус: Ожидается оплата\n\n"
            f"Перейдите по ссылке для оплаты:",
            parse_mode="Markdown"
        )
        await callback.message.answer(url)
    else:
        error_msg = invoice_data.get('error', 'Неизвестная ошибка')
        await callback.message.answer(
            f"❌ **Ошибка создания счета!**\n\n"
            f"Причина: {error_msg}\n\n"
            f"Попробуйте позже или обратитесь в поддержку.",
            parse_mode="Markdown"
        )
    await callback.answer()

@dp.callback_query(F.data == "balance")
async def balance_handler(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    balance = user[4] if user[4] else 0.0
    
    await callback.message.answer(
        f"💰 **Ваш баланс**\n\n"
        f"Доступно: ${balance:.2f} USDT\n"
        f"Минимум для вывода: ${MIN_WITHDRAWAL:.2f} USDT\n"
        f"Доступно к выводу: ${max(0, balance - MIN_WITHDRAWAL):.2f} USDT",
        parse_mode="Markdown",
        reply_markup=get_withdraw_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "withdraw_menu")
async def withdraw_menu(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    balance = user[4] if user[4] else 0.0
    has_wallet = bool(user[3])
    
    await callback.message.answer(
        f"💸 **Вывод средств**\n\n"
        f"Баланс: ${balance:.2f} USDT\n"
        f"Минимум: ${MIN_WITHDRAWAL:.2f} USDT\n"
        f"{'✅ Кошелек настроен' if has_wallet else '⚠️ Настройте кошелек в Настройках'}",
        parse_mode="Markdown",
        reply_markup=get_withdraw_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "withdraw_start")
async def withdraw_start(callback: types.CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    balance = user[4] if user[4] else 0.0
    
    if balance < MIN_WITHDRAWAL:
        await callback.message.answer(
            f"❌ **Недостаточно средств!**\n\n"
            f"Ваш баланс: ${balance:.2f} USDT\n"
            f"Минимум для вывода: ${MIN_WITHDRAWAL:.2f} USDT\n\n"
            f"Приглашайте больше людей!",
            parse_mode="Markdown"
        )
        await callback.answer()
        return
    
    if not user[3]:
        await callback.message.answer(
            f"⚠️ **Сначала настройте кошелек!**\n\n"
            f"Перейдите в **⚙️ Настройки** → **💳 Кошелек**\n"
            f"и укажите ваш @username из CryptoBot.",
            parse_mode="Markdown"
        )
        await callback.answer()
        return
    
    await callback.message.answer(
        f"💰 Введите сумму для вывода (мин. ${MIN_WITHDRAWAL}):"
    )
    await state.set_state(WithdrawState.waiting_for_amount)
    await callback.answer()

@dp.message(WithdrawState.waiting_for_amount)
async def process_withdraw_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(',', '.'))
    except:
        await message.answer("❌ Введите корректное число (например: 50)")
        return
    
    user = await get_user(message.from_user.id)
    balance = user[4] if user[4] else 0.0
    
    if amount < MIN_WITHDRAWAL:
        await message.answer(f"❌ Минимальная сумма: ${MIN_WITHDRAWAL:.2f} USDT")
        return
    if amount > balance:
        await message.answer(f"❌ Недостаточно средств. Баланс: ${balance:.2f} USDT")
        return
    
    await state.update_data(withdraw_amount=amount)
    await state.set_state(WithdrawState.waiting_for_username)
    await message.answer(
        f"💰 Сумма: ${amount:.2f} USDT\n\n"
        f"Введите ваш **@username в CryptoBot** для получения выплаты:\n\n"
        f"⚠️ Только @username (адреса кошельков не принимаются)",
        parse_mode="Markdown"
    )

@dp.message(WithdrawState.waiting_for_username)
async def process_withdraw_username(message: types.Message, state: FSMContext):
    username = message.text.strip()
    data = await state.get_data()
    amount = data.get('withdraw_amount')
    user_id = message.from_user.id
    
    if not username.startswith('@'):
        await message.answer(
            f"❌ **Вводите только @username!**\n\n"
            f"Например: @ivan",
            parse_mode="Markdown"
        )
        return
    
    await state.clear()
    await message.answer("⏳ **Обработка выплаты...**\nСтатус: В процессе", parse_mode="Markdown")
    
    payout_result = await create_payout(username, amount)
    
    if payout_result.get('ok'):
        tx_hash = payout_result.get('result', {}).get('tx_hash', 'N/A')
        await save_withdrawal(user_id, amount, username, 'completed', tx_hash)
        
        await message.answer(
            f"✅ **ВЫПЛАТА УСПЕШНА!**\n\n"
            f"💰 Сумма: ${amount:.2f} USDT\n"
            f"🏦 Получатель: {username}\n"
            f"🔗 TX: `{tx_hash}`\n\n"
            f"Средства поступят в @CryptoBot в течение 10-60 минут.",
            parse_mode="Markdown"
        )
        
        await bot.send_message(ADMIN_ID,
            f"✅ **ВЫПЛАТА ПРОВЕДЕНА**\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"💰 Сумма: ${amount:.2f} USDT\n"
            f"🏦 Кошелек: {username}\n"
            f"🔗 TX: `{tx_hash}`",
            parse_mode="Markdown"
        )
    else:
        error_msg = payout_result.get('error', 'Неизвестная ошибка')
        await save_withdrawal(user_id, amount, username, 'failed', None)
        await update_balance(user_id, amount)
        
        await message.answer(
            f"❌ **ВЫПЛАТА НЕ УДАЛАСЬ!**\n\n"
            f"Ошибка: {error_msg}\n\n"
            f"Средства возвращены на ваш баланс.",
            parse_mode="Markdown"
        )
        
        await bot.send_message(ADMIN_ID,
            f"❌ **ОШИБКА ВЫПЛАТЫ**\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"💰 Сумма: ${amount:.2f} USDT\n"
            f"❗ Ошибка: {error_msg}",
            parse_mode="Markdown"
        )

@dp.callback_query(F.data == "referral")
async def referral_handler(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    
    if not user[2]:
        await callback.message.answer(
            f"🔒 **Партнерская программа доступна только после покупки!**\n\n"
            f"Купите продукт, чтобы начать зарабатывать.",
            parse_mode="Markdown"
        )
        await callback.answer()
        return
    
    ref_link = f"https://t.me/{bot.username}?start=ref_{callback.from_user.id}"
    balance = user[4] if user[4] else 0.0
    
    await callback.message.answer(
        f"👥 **Партнерская программа**\n\n"
        f"💰 Ваш баланс: ${balance:.2f} USDT\n"
        f"🎁 За каждого друга: ${REWARD} USDT\n"
        f"📉 Минимум на вывод: ${MIN_WITHDRAWAL:.2f} USDT\n\n"
        f"Ваша реферальная ссылка:\n`{ref_link}`",
        parse_mode="Markdown",
        reply_markup=get_referral_keyboard(ref_link)
    )
    await callback.answer()

@dp.callback_query(F.data == "referral_locked")
async def referral_locked(callback: types.CallbackQuery):
    await callback.message.answer(
        f"🔒 **Партнерская программа доступна только после покупки!**\n\n"
        f"Купите продукт, чтобы начать зарабатывать на приглашениях.",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "copy_ref_link")
async def copy_ref_link(callback: types.CallbackQuery):
    ref_link = f"https://t.me/{bot.username}?start=ref_{callback.from_user.id}"
    await callback.answer(f"🔗 Ссылка: {ref_link}", show_alert=True)

@dp.callback_query(F.data == "ref_stats")
async def ref_stats(callback: types.CallbackQuery):
    await callback.message.answer("📊 **Статистика**\n\nРаздел в разработке...")
    await callback.answer()

@dp.callback_query(F.data == "settings")
async def settings_handler(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    wallet = user[3] if user[3] else "Не настроен"
    
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Изменить кошелек", callback_data="set_wallet")
    builder.button(text="📖 Инструкция по выплатам", callback_data="help_instructions")
    builder.button(text="🏠 В меню", callback_data="main_menu")
    builder.adjust(1)
    
    await callback.message.answer(
        f"⚙️ **Настройки**\n\n"
        f"💳 Кошелек для выплат:\n`{wallet}`\n\n"
        f"⚠️ Выплаты работают ТОЛЬКО через @username в CryptoBot!",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "help_instructions")
async def help_instructions(callback: types.CallbackQuery):
    await callback.message.answer(
        f"📖 **ИНСТРУКЦИЯ ПО НАСТРОЙКЕ ВЫПЛАТ**\n\n"
        f"Для получения реферальных выплат:\n\n"
        f"1️⃣ Зайдите в @CryptoBot\n"
        f"2️⃣ Нажмите **Профиль** → **Изменить профиль**\n"
        f"3️⃣ Придумайте Username (например: @myname)\n"
        f"4️⃣ Вернитесь сюда и введите этот @username\n\n"
        f"⚠️ **Важно:**\n"
        f"- Только @username (не адреса кошельков!)\n"
        f"- Минимум на вывод: ${MIN_WITHDRAWAL} USDT\n"
        f"- За друга: ${REWARD} USDT",
        parse_mode="Markdown",
        reply_markup=get_help_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "set_wallet")
async def set_wallet_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        f"💳 **Настройка кошелька**\n\n"
        f"Введите ваш **@username в CryptoBot**:\n\n"
        f"⚠️ Проверьте внимательно! Ошибка приведет к потере средств.\n"
        f"❌ Адреса кошельков (TRC20, BEP20) НЕ принимаются!",
        parse_mode="Markdown"
    )
    await state.set_state(WalletState.waiting_for_wallet)
    await callback.answer()

@dp.message(WalletState.waiting_for_wallet)
async def process_wallet(message: types.Message, state: FSMContext):
    wallet = message.text.strip()
    
    if not wallet.startswith('@'):
        await message.answer(
            f"❌ **Некорректный формат!**\n\n"
            f"Вводите только **@username** в CryptoBot (например: @ivan)\n\n"
            f"Адреса кошельков НЕ принимаются!",
            parse_mode="Markdown"
        )
        return
    
    if ' ' in wallet or len(wallet) < 6:
        await message.answer(
            f"❌ **Некорректный формат!**\n\n"
            f"Проверьте чтобы не было пробелов и username был введен верно.",
            parse_mode="Markdown"
        )
        return
    
    await set_wallet(message.from_user.id, wallet)
    await state.clear()
    
    await message.answer(
        f"✅ **Кошелек сохранен!**\n\n"
        f"Username: `{wallet}`\n\n"
        f"Теперь вы можете выводить средства.",
        parse_mode="Markdown",
        reply_markup=get_main_menu(True, (await get_user(message.from_user.id))[4] or 0)
    )

@dp.callback_query(F.data == "support")
async def support_handler(callback: types.CallbackQuery):
    # ⚠️ ИЗМЕНИТЕ @YOUR_SUPPORT_USERNAME НА ВАШ РЕАЛЬНЫЙ НИК!
    await callback.message.answer(
        f"📞 **Поддержка**\n\n"
        f"По всем вопросам обращайтесь:\n"
        f"@YOUR_SUPPORT_USERNAME\n\n"
        f"⏰ Время работы: 24/7\n\n"
        f"📖 Перед обращением прочитайте /help",
        parse_mode="Markdown"
    )
    await callback.answer()

# ==================== АДМИН КОМАНДЫ ====================

@dp.message(Command("update_product"))
async def admin_update_product(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ Использование: /update_product <новая_ссылка>")
        return
    
    new_link = args[1]
    await update_product_link(new_link)
    
    paid_users = await get_all_paid_users()
    success_count = 0
    
    for user_tuple in paid_users:
        user_id = user_tuple[0]
        try:
            await bot.send_message(user_id,
                f"🔔 **ОБНОВЛЕНИЕ ПРОДУКТА!**\n\n"
                f"Ваш доступ к продукту обновлен!\n"
                f"Нажмите **📚 Продукт** в меню, чтобы открыть новую версию.",
                parse_mode="Markdown"
            )
            success_count += 1
        except:
            pass
    
    await message.answer(
        f"✅ **Продукт обновлен!**\n\n"
        f"Новая ссылка: {new_link}\n"
        f"Уведомлено пользователей: {success_count}",
        parse_mode="Markdown"
    )

@dp.message(Command("payments"))
async def admin_payments(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    async with aiosqlite.connect('users.db') as db:
        cursor = await db.execute('SELECT * FROM payments ORDER BY created_at DESC LIMIT 10')
        payments = await cursor.fetchall()
    
    if not payments:
        await message.answer("Нет платежей.")
        return
    
    text = "📊 **Последние платежи:**\n\n"
    for p in payments:
        status_emoji = {'paid': '✅', 'pending': '⏳', 'failed': '❌'}.get(p[3], '❓')
        text += f"{status_emoji} `{p[0][:20]}...` | ${p[2]} | {p[3]}\n"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("withdrawals"))
async def admin_withdrawals(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    async with aiosqlite.connect('users.db') as db:
        cursor = await db.execute('SELECT * FROM withdrawals ORDER BY created_at DESC LIMIT 10')
        withdrawals = await cursor.fetchall()
    
    if not withdrawals:
        await message.answer("Нет выплат.")
        return
    
    text = "💸 **Последние выплаты:**\n\n"
    for w in withdrawals:
        status_emoji = {'completed': '✅', 'processing': '⏳', 'failed': '❌', 'pending': '⏳'}.get(w[4], '❓')
        text += f"{status_emoji} ID:{w[0]} | ${w[2]} | {w[4]}\n"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    
    async with aiosqlite.connect('users.db') as db:
        cursor = await db.execute('SELECT COUNT(*) FROM users')
        total_users = (await cursor.fetchone())[0]
        
        cursor = await db.execute('SELECT COUNT(*) FROM users WHERE is_paid = ?', (1,))
        paid_users = (await cursor.fetchone())[0]
        
        cursor = await db.execute('SELECT SUM(balance) FROM users')
        total_balance = (await cursor.fetchone())[0] or 0.0
    
    await message.answer(
        f"📊 **Статистика бота**\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"✅ Оплативших: {paid_users}\n"
        f"💰 Общий баланс пользователей: ${total_balance:.2f} USDT\n"
        f"📈 Конверсия: {(paid_users/total_users*100) if total_users > 0 else 0:.1f}%",
        parse_mode="Markdown"
    )

# ==================== ЗАПУСК С LONG POLLING ====================

async def on_startup_polling(bot: Bot):
    """Действия при запуске бота (для polling)"""
    await bot.delete_webhook()
    logging.info("✅ Бот запущен в режиме Long Polling")

async def main():
    await init_db()
    await bot.delete_webhook()
    dp.startup.register(on_startup_polling)
    
    # Запускаем фоновую задачу проверки платежей
    asyncio.create_task(check_pending_payments_periodically())
    
    # Запуск бота в режиме POLLING
    logging.info("🚀 Запуск бота...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🛑 Бот остановлен")
