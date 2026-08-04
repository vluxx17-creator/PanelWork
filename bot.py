import asyncio
import logging
import sqlite3
import random
import json
import os
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# --- Конфиг ---
BOT_TOKEN = "8997816663:AAGyPl4aj69g3xeax5AZHmixw7nmhJ5SuLw"
ADMIN_IDS = [8297446667]  # ваш админ
GROUP_LINK = "https://t.me/+RIv8Upp6kptkYTVk"
WEBAPP_URL = "https://vluxx17-creator.github.io/Ryzenteam/"  # ЗАМЕНИТЕ

# Порт для вебхука (Render задаёт через PORT)
PORT = int(os.environ.get("PORT", 8443))
WEBHOOK_PATH = "/webhook"
# Для локального теста можно закомментировать WEBHOOK_HOST,
# но на Render нужно указать публичный URL вашего сервиса
WEBHOOK_HOST = os.environ.get("WEBHOOK_HOST", "https://ваш-сервис.onrender.com")

# --- База данных SQLite ---
conn = sqlite3.connect('bot_data.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    registered_at TEXT,
    filled_form INTEGER DEFAULT 0,
    form_answers TEXT,
    is_banned INTEGER DEFAULT 0
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

class FormState(StatesGroup):
    waiting_work_hours = State()
    waiting_goals = State()
    waiting_success = State()

class BroadcastState(StatesGroup):
    waiting_message = State()

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
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username, first_name, registered_at, filled_form, is_banned) VALUES (?, ?, ?, ?, ?, ?)',
                   (user_id, username, first_name, datetime.now().isoformat(), 0, 0))
    conn.commit()

def update_user_form(user_id, answers):
    cursor.execute('UPDATE users SET filled_form=1, form_answers=? WHERE user_id=?', (answers, user_id))
    conn.commit()

def is_user_banned(user_id):
    cursor.execute('SELECT is_banned FROM users WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    return row and row[0] == 1

def is_form_filled(user_id):
    cursor.execute('SELECT filled_form FROM users WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    return row and row[0] == 1

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

def get_all_users():
    cursor.execute('SELECT user_id FROM users WHERE is_banned=0')
    return [row[0] for row in cursor.fetchall()]

# --- Обработчики команд (все те же, что были) ---
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user = message.from_user
    cursor.execute('SELECT user_id FROM users WHERE user_id=?', (user.id,))
    if not cursor.fetchone():
        save_user(user.id, user.username, user.first_name)

    if is_user_banned(user.id):
        await message.answer("⛔ Вы забанены. Обратитесь к администратору.")
        return

    if user.id in ADMIN_IDS:
        await send_admin_welcome(message)
        return

    cursor.execute('SELECT answer FROM captcha_sessions WHERE user_id=?', (user.id,))
    if not cursor.fetchone():
        question, answer = generate_captcha()
        cursor.execute('INSERT OR REPLACE INTO captcha_sessions (user_id, answer, created_at) VALUES (?, ?, ?)',
                       (user.id, answer, datetime.now().isoformat()))
        conn.commit()
        await state.set_state(CaptchaState.waiting_answer)
        await message.answer(f"🧠 *Пожалуйста, решите простой пример:*\n{question}\n\nВведите ответ цифрой.", parse_mode="Markdown")
        return

    if not is_form_filled(user.id):
        await start_form(message, state)
        return

    await send_welcome(message)

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
        if not is_form_filled(user.id):
            await start_form(message, state)
        else:
            await send_welcome(message)
    else:
        question, answer = generate_captcha()
        cursor.execute('UPDATE captcha_sessions SET answer=?, created_at=? WHERE user_id=?',
                       (answer, datetime.now().isoformat(), user.id))
        conn.commit()
        await message.answer(f"❌ Неверно! Попробуйте ещё раз:\n{question}")

async def start_form(message: Message, state: FSMContext):
    await state.set_state(FormState.waiting_work_hours)
    await message.answer("📝 *Заполните анкету для вступления в команду*\n\n"
                         "1️⃣ Сколько часов в день вы готовы воркать? (цифрой)")

@dp.message(FormState.waiting_work_hours)
async def form_work_hours(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введите число часов.")
        return
    await state.update_data(work_hours=message.text)
    await state.set_state(FormState.waiting_goals)
    await message.answer("2️⃣ Какие ваши цели в нашей команде?")

@dp.message(FormState.waiting_goals)
async def form_goals(message: Message, state: FSMContext):
    await state.update_data(goals=message.text)
    await state.set_state(FormState.waiting_success)
    await message.answer("3️⃣ Хотите ли вы добиться успеха в нашей команде? (да/нет)")

@dp.message(FormState.waiting_success)
async def form_success(message: Message, state: FSMContext):
    answer = message.text.lower()
    if answer not in ['да', 'нет']:
        await message.answer("❌ Ответьте 'да' или 'нет'.")
        return
    data = await state.get_data()
    work_hours = data.get('work_hours')
    goals = data.get('goals')
    full_answer = f"Часы: {work_hours}\nЦели: {goals}\nУспех: {answer}"
    update_user_form(message.from_user.id, full_answer)
    await state.clear()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Перейти в приложение", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton(text="👥 Вступить в группу", url=GROUP_LINK)]
    ])
    await message.answer(
        f"✅ *Вы приняты в команду Ryzen Team!*\n\n"
        f"🎉 Поздравляем! Теперь вы часть нашего сообщества.\n"
        f"🔥 Подайте заявку в нашу группу: {GROUP_LINK}\n\n"
        f"А также можете перейти в мини-приложение для управления выплатами.",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def send_welcome(message: Message):
    user = message.from_user
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Перейти в приложение", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        f"👋 *Добро пожаловать обратно, {user.first_name}!*\n\n"
        "Вы уже прошли анкету и являетесь участником команды.\n"
        "Нажмите кнопку ниже, чтобы открыть мини-приложение.",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def send_admin_welcome(message: Message):
    await message.answer(
        "👑 *Добро пожаловать, Администратор!*\n\n"
        "Доступные команды:\n"
        "/admin – управление заявками\n"
        "/ban – забанить пользователя (ответом на сообщение)\n"
        "/unban – разбанить (по ID или username)\n"
        "/broadcast – сделать рассылку всем пользователям",
        parse_mode="Markdown"
    )

@dp.message(F.web_app_data)
async def handle_webapp_data(message: Message):
    if is_user_banned(message.from_user.id):
        await message.answer("⛔ Вы забанены.")
        return
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
        for admin_id in ADMIN_IDS:
            await bot.send_message(admin_id, f"📩 Новая заявка #{payout_id}\n"
                                             f"Пользователь: @{message.from_user.username or message.from_user.first_name}\n"
                                             f"NFT: {nft}\nКошелёк: {wallet}")
    except Exception as e:
        await message.answer("❌ Ошибка обработки данных.")

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
                f"Статус: принята администратором.",
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить пользователя {user_id}: {e}")
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

@dp.message(Command("ban"))
async def ban_user(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.")
        return
    if not message.reply_to_message:
        await message.answer("Ответьте на сообщение пользователя, которого хотите забанить.")
        return
    user_id = message.reply_to_message.from_user.id
    if user_id in ADMIN_IDS:
        await message.answer("⛔ Нельзя забанить администратора.")
        return
    cursor.execute('UPDATE users SET is_banned=1 WHERE user_id=?', (user_id,))
    conn.commit()
    await message.answer(f"✅ Пользователь {user_id} забанен.")

@dp.message(Command("unban"))
async def unban_user(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажите ID или @username пользователя.\nПример: /unban 123456789")
        return
    target = args[1].strip()
    if target.startswith('@'):
        username = target[1:]
        cursor.execute('SELECT user_id FROM users WHERE username=?', (username,))
        row = cursor.fetchone()
        if not row:
            await message.answer("❌ Пользователь не найден.")
            return
        user_id = row[0]
    else:
        try:
            user_id = int(target)
        except ValueError:
            await message.answer("❌ Некорректный ID.")
            return
    cursor.execute('UPDATE users SET is_banned=0 WHERE user_id=?', (user_id,))
    conn.commit()
    await message.answer(f"✅ Пользователь {user_id} разбанен.")

@dp.message(Command("broadcast"))
async def broadcast_start(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.")
        return
    await state.set_state(BroadcastState.waiting_message)
    await message.answer("✍️ Введите сообщение для рассылки:")

@dp.message(BroadcastState.waiting_message)
async def broadcast_send(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.")
        return
    text = message.text
    users = get_all_users()
    if not users:
        await message.answer("❌ Нет пользователей для рассылки.")
        await state.clear()
        return
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 *Рассылка от администрации:*\n\n{text}", parse_mode="Markdown")
            sent += 1
        except:
            pass
    await message.answer(f"✅ Рассылка отправлена {sent} пользователям.")
    await state.clear()

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

# --- Запуск через вебхук (с портом) ---
async def on_startup():
    # Устанавливаем вебхук
    webhook_url = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
    await bot.set_webhook(webhook_url)
    logging.info(f"Webhook set to {webhook_url}")

async def main():
    logging.basicConfig(level=logging.INFO)

    # Создаём aiohttp приложение
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=None,  # можно добавить SECRET_TOKEN для безопасности
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    # Настраиваем запуск (установка вебхука)
    app.router.post(WEBHOOK_PATH, webhook_requests_handler.handle)

    # Запускаем веб-сервер
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    # Устанавливаем вебхук при старте
    await on_startup()

    logging.info(f"Bot started, listening on port {PORT}")
    # Бесконечное ожидание
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
