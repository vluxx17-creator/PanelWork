import asyncio
import logging
import sqlite3
import random
import json
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# --- Конфиг ---
BOT_TOKEN = "8997816663:AAGyPl4aj69g3xeax5AZHmixw7nmhJ5SuLw"
ADMIN_IDS = [8297446667,]  # ЗАМЕНИТЕ НА СВОЙ TELEGRAM ID (можно несколько)

# --- База данных SQLite ---
conn = sqlite3.connect('bot_data.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    registered_at TEXT
)
''')
cursor.execute('''
CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    nft TEXT,
    wallet TEXT,
    amount REAL,
    status TEXT DEFAULT 'pending',
    requested_at TEXT,
    accepted_at TEXT,
    admin_id INTEGER
)
''')
cursor.execute('''
CREATE TABLE IF NOT EXISTS captcha_sessions (
    user_id INTEGER PRIMARY KEY,
    answer INTEGER,
    created_at TEXT
)
''')
conn.commit()

# --- Состояния FSM ---
class CaptchaState(StatesGroup):
    waiting_answer = State()

class PayoutState(StatesGroup):
    waiting_wallet = State()
    waiting_nft = State()

# --- Инициализация бота ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Утилиты ---
def generate_captcha():
    a = random.randint(1, 9)
    b = random.randint(1, 9)
    op = random.choice(['+', '-'])
    if op == '+':
        answer = a + b
    else:
        if a < b:
            a, b = b, a
        answer = a - b
    question = f"{a} {op} {b} = ?"
    return question, answer

def save_user(user_id, username, first_name):
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username, first_name, registered_at) VALUES (?, ?, ?, ?)',
                   (user_id, username, first_name, datetime.now().isoformat()))
    conn.commit()

def add_payout(user_id, nft, wallet):
    cursor.execute('INSERT INTO payouts (user_id, nft, wallet, requested_at, status) VALUES (?, ?, ?, ?, ?)',
                   (user_id, nft, wallet, datetime.now().isoformat(), 'pending'))
    conn.commit()
    return cursor.lastrowid

def get_pending_payouts():
    cursor.execute('SELECT id, user_id, nft, wallet, requested_at FROM payouts WHERE status="pending" ORDER BY id')
    return cursor.fetchall()

def update_payout(payout_id, amount, status, admin_id):
    cursor.execute('UPDATE payouts SET amount=?, status=?, accepted_at=?, admin_id=? WHERE id=?',
                   (amount, status, datetime.now().isoformat(), admin_id, payout_id))
    conn.commit()

# --- Обработчики команд ---

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user = message.from_user
    save_user(user.id, user.username, user.first_name)

    # Проверяем, проходил ли капчу
    cursor.execute('SELECT answer FROM captcha_sessions WHERE user_id=?', (user.id,))
    if cursor.fetchone():
        await send_welcome(message)
        return

    question, answer = generate_captcha()
    cursor.execute('INSERT OR REPLACE INTO captcha_sessions (user_id, answer, created_at) VALUES (?, ?, ?)',
                   (user.id, answer, datetime.now().isoformat()))
    conn.commit()
    await state.set_state(CaptchaState.waiting_answer)
    await message.answer(f"🧠 *Пожалуйста, решите простой пример:*\n{question}\n\nВведите ответ цифрой.", parse_mode="Markdown")

@dp.message(CaptchaState.waiting_answer)
async def captcha_answer(message: Message, state: FSMContext):
    user = message.from_user
    try:
        user_answer = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите число!")
        return

    cursor.execute('SELECT answer FROM captcha_sessions WHERE user_id=?', (user.id,))
    row = cursor.fetchone()
    if not row:
        await message.answer("❌ Ошибка, попробуйте /start заново.")
        await state.clear()
        return

    correct_answer = row[0]
    if user_answer == correct_answer:
        cursor.execute('DELETE FROM captcha_sessions WHERE user_id=?', (user.id,))
        conn.commit()
        await state.clear()
        await send_welcome(message)
    else:
        question, answer = generate_captcha()
        cursor.execute('UPDATE captcha_sessions SET answer=?, created_at=? WHERE user_id=?',
                       (answer, datetime.now().isoformat(), user.id))
        conn.commit()
        await message.answer(f"❌ Неверно! Попробуйте ещё раз:\n{question}")

async def send_welcome(message: Message):
    user = message.from_user
    # Укажите URL вашего мини-приложения (после деплоя на Render)
    webapp_url = "https://ваш-хост-на-render.com"  # ЗАМЕНИТЕ
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Перейти в приложение", web_app=WebAppInfo(url=webapp_url))]
    ])
    await message.answer(
        f"👋 *Добро пожаловать, {user.first_name}!*\n\n"
        "Вы успешно прошли капчу. Теперь вы можете пользоваться нашим сервисом.\n"
        "Нажмите кнопку ниже, чтобы открыть мини-приложение.",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

# --- Обработка данных из WebApp ---
@dp.message(F.web_app_data)
async def handle_webapp_data(message: Message):
    data = message.web_app_data.data
    try:
        payload = json.loads(data)
        nft = payload.get('nft')
        wallet = payload.get('wallet')
        if not nft or not wallet:
            await message.answer("❌ Некорректные данные.")
            return
        user_id = message.from_user.id
        payout_id = add_payout(user_id, nft, wallet)
        await message.answer(f"✅ Заявка на выплату создана! ID: {payout_id}\nNFT: {nft}\nКошелёк: {wallet}\n\nОжидайте решения администратора.")
        # Уведомление админов
        for admin_id in ADMIN_IDS:
            await bot.send_message(admin_id, f"📩 Новая заявка #{payout_id}\n"
                                             f"Пользователь: @{message.from_user.username or message.from_user.first_name}\n"
                                             f"NFT: {nft}\nКошелёк: {wallet}")
    except Exception as e:
        await message.answer("❌ Ошибка обработки данных.")

# --- Административные команды ---
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещён.")
        return
    pending = get_pending_payouts()
    if not pending:
        await message.answer("📭 Нет заявок на выплату.")
        return
    builder = InlineKeyboardBuilder()
    for p in pending:
        p_id, user_id, nft, wallet, requested = p
        cursor.execute('SELECT username, first_name FROM users WHERE user_id=?', (user_id,))
        user_info = cursor.fetchone()
        name = user_info[1] if user_info else str(user_id)
        builder.button(text=f"#{p_id} {name} - {nft}", callback_data=f"payout_{p_id}")
    builder.adjust(1)
    await message.answer("📋 *Список заявок:*", parse_mode="Markdown", reply_markup=builder.as_markup())

@dp.callback_query(lambda c: c.data and c.data.startswith('payout_'))
async def payout_detail(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    payout_id = int(callback.data.split('_')[1])
    cursor.execute('SELECT id, user_id, nft, wallet, requested_at FROM payouts WHERE id=?', (payout_id,))
    row = cursor.fetchone()
    if not row:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    p_id, user_id, nft, wallet, requested = row
    cursor.execute('SELECT username, first_name FROM users WHERE user_id=?', (user_id,))
    user_info = cursor.fetchone()
    name = user_info[1] if user_info else str(user_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Принять", callback_data=f"accept_{p_id}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_{p_id}")
    builder.adjust(2)
    await callback.message.answer(
        f"📄 *Заявка #{p_id}*\n"
        f"Пользователь: {name}\n"
        f"NFT: {nft}\n"
        f"Кошелёк: {wallet}\n"
        f"Дата: {requested}\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith('accept_'))
async def accept_payout(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    payout_id = int(callback.data.split('_')[1])
    await state.update_data(payout_id=payout_id)
    await callback.message.answer("💰 Введите сумму выплаты (в ETH, например 0.1):")
    await state.set_state("waiting_amount")
    await callback.answer()

@dp.message(StateFilter("waiting_amount"))
async def process_amount(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.")
        return
    try:
        amount = float(message.text.strip().replace(',', '.'))
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число (например, 0.05)")
        return
    data = await state.get_data()
    payout_id = data.get('payout_id')
    if not payout_id:
        await message.answer("❌ Ошибка, попробуйте заново.")
        await state.clear()
        return

    update_payout(payout_id, amount, 'accepted', message.from_user.id)
    cursor.execute('SELECT user_id, nft, wallet FROM payouts WHERE id=?', (payout_id,))
    row = cursor.fetchone()
    if row:
        user_id, nft, wallet = row
        try:
            await bot.send_message(
                user_id,
                f"✅ *Ваша выплата одобрена!*\n\n"
                f"Сумма: {amount} ETH\n"
                f"NFT: {nft}\n"
                f"Кошелёк: {wallet}\n"
                f"Статус: принята администратором.\n\n"
                f"💸 Средства отправлены на ваш кошелёк (автоматически).",
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить пользователя {user_id}: {e}")
        # Здесь можно вызвать реальную отправку TON (заглушка)
        # await send_ton(wallet, amount)   # реализовать отдельно
        await message.answer(f"✅ Заявка #{payout_id} принята, сумма {amount} ETH. Пользователь уведомлён.")
    else:
        await message.answer("❌ Заявка не найдена.")
    await state.clear()

@dp.callback_query(lambda c: c.data and c.data.startswith('reject_'))
async def reject_payout(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    payout_id = int(callback.data.split('_')[1])
    update_payout(payout_id, 0, 'rejected', callback.from_user.id)
    cursor.execute('SELECT user_id, nft FROM payouts WHERE id=?', (payout_id,))
    row = cursor.fetchone()
    if row:
        user_id, nft = row
        try:
            await bot.send_message(
                user_id,
                f"❌ *Ваша выплата отклонена.*\n\n"
                f"NFT: {nft}\n"
                f"Причина: администратор отклонил заявку.",
                parse_mode="Markdown"
            )
        except:
            pass
    await callback.message.edit_text(f"❌ Заявка #{payout_id} отклонена.")
    await callback.answer()

# --- Управление группой (примеры) ---
@dp.message(Command("ban"))
async def ban_user(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.")
        return
    if not message.reply_to_message:
        await message.answer("Ответьте на сообщение пользователя.")
        return
    user_id = message.reply_to_message.from_user.id
    try:
        await bot.ban_chat_member(message.chat.id, user_id)
        await message.answer(f"✅ Пользователь {user_id} забанен.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("mute"))
async def mute_user(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.")
        return
    if not message.reply_to_message:
        await message.answer("Ответьте на сообщение пользователя.")
        return
    user_id = message.reply_to_message.from_user.id
    try:
        await bot.restrict_chat_member(message.chat.id, user_id, permissions=types.ChatPermissions(can_send_messages=False))
        await message.answer(f"🔇 Пользователь {user_id} заглушен.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# --- Запуск ---
async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
