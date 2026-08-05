import asyncio
import logging
import sqlite3
import random
import json
import os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Message, CallbackQuery, ChatPermissions
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web

# ========== КОНФИГ ==========
BOT_TOKEN = "8997816663:AAGyPl4aj69g3xeax5AZHmixw7nmhJ5SuLw"
ADMIN_IDS = [8297446667]          # твой Telegram ID
WEBAPP_URL = "https://vluxx17-creator.github.io/Ryzenteam/"
WEBHOOK_HOST = "https://panelwork.onrender.com"
PORT = int(os.environ.get("PORT", 8443))
WEBHOOK_PATH = "/webhook"

# ID группы, куда отправлять уведомления о выплатах
GROUP_ID = -1004421628533

# ========== БАЗА ДАННЫХ ==========
conn = sqlite3.connect('bot_data.db', check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    registered_at TEXT,
    is_banned INTEGER DEFAULT 0,
    captcha_passed INTEGER DEFAULT 0
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
    admin_id INTEGER,
    admin_message_id INTEGER,
    code TEXT,
    link TEXT
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

# ========== СОСТОЯНИЯ FSM ==========
class CaptchaState(StatesGroup):
    waiting_answer = State()

class BroadcastState(StatesGroup):
    waiting_message = State()

class AdminStates(StatesGroup):
    waiting_amount = State()
    waiting_reason = State()

# ========== ИНИЦИАЛИЗАЦИЯ БОТА ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ========== УТИЛИТЫ ==========
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
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username, first_name, registered_at, captcha_passed) VALUES (?, ?, ?, ?, ?)',
                   (user_id, username, first_name, datetime.now().isoformat(), 0))
    conn.commit()

def mark_captcha_passed(user_id):
    cursor.execute('UPDATE users SET captcha_passed=1 WHERE user_id=?', (user_id,))
    conn.commit()

def is_captcha_passed(user_id):
    cursor.execute('SELECT captcha_passed FROM users WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    return row and row[0] == 1

def is_user_banned(user_id):
    cursor.execute('SELECT is_banned FROM users WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    return row and row[0] == 1

def add_payout(user_id, nft, wallet, code, link):
    cursor.execute('INSERT INTO payouts (user_id, nft, wallet, requested_at, status, code, link) VALUES (?, ?, ?, ?, ?, ?, ?)',
                   (user_id, nft, wallet, datetime.now().isoformat(), 'pending', code, link))
    conn.commit()
    return cursor.lastrowid

def get_pending_payouts():
    cursor.execute('SELECT id, user_id, nft, wallet, requested_at FROM payouts WHERE status="pending" ORDER BY id')
    return cursor.fetchall()

def update_payout(payout_id, amount, status, admin_id, admin_msg_id=None):
    if admin_msg_id:
        cursor.execute('UPDATE payouts SET amount=?, status=?, accepted_at=?, admin_id=?, admin_message_id=? WHERE id=?',
                       (amount, status, datetime.now().isoformat(), admin_id, admin_msg_id, payout_id))
    else:
        cursor.execute('UPDATE payouts SET amount=?, status=?, accepted_at=?, admin_id=? WHERE id=?',
                       (amount, status, datetime.now().isoformat(), admin_id, payout_id))
    conn.commit()

def get_payout(payout_id):
    cursor.execute('SELECT user_id, nft, wallet, admin_message_id, code, link FROM payouts WHERE id=?', (payout_id,))
    return cursor.fetchone()

def get_all_users():
    cursor.execute('SELECT user_id FROM users WHERE is_banned=0')
    return [row[0] for row in cursor.fetchall()]

def get_stats():
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM payouts')
    total_payouts = cursor.fetchone()[0]
    cursor.execute('SELECT SUM(amount) FROM payouts WHERE status="accepted"')
    total_amount = cursor.fetchone()[0] or 0
    return total_users, total_payouts, total_amount

def get_top_users(period):
    if period == 'day':
        since = datetime.now() - timedelta(days=1)
    elif period == 'week':
        since = datetime.now() - timedelta(days=7)
    elif period == 'month':
        since = datetime.now() - timedelta(days=30)
    else:
        return []
    since_str = since.isoformat()
    cursor.execute('''
        SELECT user_id, SUM(amount) as total 
        FROM payouts 
        WHERE status="accepted" AND accepted_at > ? 
        GROUP BY user_id 
        ORDER BY total DESC 
        LIMIT 10
    ''', (since_str,))
    rows = cursor.fetchall()
    result = []
    for user_id, total in rows:
        cursor.execute('SELECT username, first_name FROM users WHERE user_id=?', (user_id,))
        user_info = cursor.fetchone()
        name = user_info[1] if user_info else str(user_id)
        username = user_info[0] if user_info else None
        result.append((name, username, total))
    return result

# ========== ОБРАБОТЧИКИ КОМАНД (личные) ==========
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user = message.from_user
    save_user(user.id, user.username, user.first_name)

    if is_user_banned(user.id):
        await message.answer("⛔ Вы забанены. Обратитесь к администратору.")
        return

    if user.id in ADMIN_IDS:
        await message.answer("👑 *Добро пожаловать, Администратор!*\n\n"
                             "Доступные команды:\n"
                             "/admin – управление заявками\n"
                             "/ban – забанить (ответом на сообщение)\n"
                             "/unban – разбанить\n"
                             "/broadcast – рассылка\n"
                             "/mute – заглушить",
                             parse_mode="Markdown")
        return

    if is_captcha_passed(user.id):
        await send_welcome(message)
        return

    question, answer = generate_captcha()
    cursor.execute('INSERT OR REPLACE INTO captcha_sessions (user_id, answer, created_at) VALUES (?, ?, ?)',
                   (user.id, answer, datetime.now().isoformat()))
    conn.commit()
    await state.set_state(CaptchaState.waiting_answer)
    await message.answer(f"🧠 *Решите простой пример:*\n{question}\n\nВведите ответ цифрой.", parse_mode="Markdown")

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
        mark_captcha_passed(user.id)
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
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Перейти в приложение", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        f"👋 *Добро пожаловать, {user.first_name}!*\n\n"
        "Вы успешно прошли капчу. Теперь вы можете пользоваться сервисом.\n"
        "Нажмите кнопку ниже, чтобы открыть мини-приложение.",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

# ========== ОБРАБОТКА ЗАЯВОК ИЗ WEBAPP ==========
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
        code = payload.get('code', 'нет')
        link = payload.get('link', '')
        if not nft or not wallet:
            await message.answer("❌ Некорректные данные.")
            return
        user_id = message.from_user.id
        payout_id = add_payout(user_id, nft, wallet, code, link)

        await message.answer(f"✅ *Заявка на выплату создана!*\n"
                             f"🆔 ID: {payout_id}\n"
                             f"🖼 NFT: {nft}\n"
                             f"💳 Кошелёк: {wallet}\n\n"
                             f"⏳ Ожидайте решения администратора.",
                             parse_mode="Markdown")

        # Уведомление админов
        user = message.from_user
        username = f"@{user.username}" if user.username else user.first_name
        admin_text = (
            f"📩 *Новая заявка #ID{payout_id}*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👤 *Пользователь:* {username}\n"
            f"🆔 ID: `{user.id}`\n"
            f"🖼 *NFT:* {nft}\n"
            f"💳 *Кошелёк:* `{wallet}`\n"
            f"🔗 *Ссылка:* {link or '—'}\n"
            f"📌 *Код:* {code}\n"
            f"📅 *Дата:* {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Выберите действие:"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{payout_id}"),
             InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{payout_id}")]
        ])
        for admin_id in ADMIN_IDS:
            sent = await bot.send_message(admin_id, admin_text, parse_mode="Markdown", reply_markup=keyboard)
            cursor.execute('UPDATE payouts SET admin_message_id=? WHERE id=?', (sent.message_id, payout_id))
            conn.commit()

    except Exception as e:
        logging.error(f"Ошибка web_app_data: {e}")
        await message.answer("❌ Ошибка обработки данных.")

# ========== АДМИН-КОМАНДЫ ==========
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
    cursor.execute('SELECT id, user_id, nft, wallet, requested_at, code, link FROM payouts WHERE id=?', (payout_id,))
    row = cursor.fetchone()
    if not row:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    p_id, user_id, nft, wallet, requested, code, link = row
    cursor.execute('SELECT username, first_name FROM users WHERE user_id=?', (user_id,))
    user_info = cursor.fetchone()
    name = user_info[1] if user_info else str(user_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Принять", callback_data=f"accept_{p_id}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_{p_id}")
    builder.adjust(2)
    await callback.message.answer(
        f"📄 *Заявка #{p_id}*\n"
        f"👤 Пользователь: {name}\n"
        f"🖼 NFT: {nft}\n"
        f"💳 Кошелёк: `{wallet}`\n"
        f"🔗 Ссылка: {link or '—'}\n"
        f"📌 Код: {code or '—'}\n"
        f"📅 Дата: {requested}\n\n"
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
    await callback.message.answer("💰 Введите сумму выплаты (в TON):")
    await state.set_state(AdminStates.waiting_amount)
    await callback.answer()

@dp.message(AdminStates.waiting_amount)
async def process_amount(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.")
        return
    try:
        amount = float(message.text.strip().replace(',', '.'))
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число (например, 0.5)")
        return
    data = await state.get_data()
    payout_id = data.get('payout_id')
    if not payout_id:
        await message.answer("❌ Ошибка, попробуйте заново.")
        await state.clear()
        return

    payout = get_payout(payout_id)
    if not payout:
        await message.answer("❌ Заявка не найдена.")
        await state.clear()
        return
    user_id, nft, wallet, admin_msg_id, code, link = payout

    # Обновляем заявку
    update_payout(payout_id, amount, 'accepted', message.from_user.id, admin_msg_id)

    # Уведомление пользователю
    try:
        await bot.send_message(
            user_id,
            f"✅ *Ваша выплата одобрена!*\n\n"
            f"💰 Сумма: {amount} TON\n"
            f"🖼 NFT: {nft}\n"
            f"💳 Кошелёк: `{wallet}`\n"
            f"📅 Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"Статус: *принята* администратором.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить пользователя {user_id}: {e}")

    # Обновляем сообщение админа
    for admin_id in ADMIN_IDS:
        try:
            await bot.edit_message_text(
                f"✅ *Заявка #{payout_id} принята*\n"
                f"Сумма: {amount} TON\n"
                f"Пользователь: @{message.from_user.username or message.from_user.first_name}\n"
                f"Кошелёк: {wallet}",
                chat_id=admin_id,
                message_id=admin_msg_id,
                parse_mode="Markdown"
            )
        except:
            pass

    # Отправляем сообщение в группу
    if GROUP_ID:
        try:
            await bot.send_message(
                GROUP_ID,
                f"🎉 *Выплата произведена!*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Пользователь: @{message.from_user.username or message.from_user.first_name}\n"
                f"🖼 NFT: {nft}\n"
                f"💰 Сумма: {amount} TON\n"
                f"📅 Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"#выплата #ryzenteam",
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение в группу: {e}")

    await message.answer(f"✅ Заявка #{payout_id} принята, сумма {amount} TON. Пользователь уведомлён.")
    await state.clear()

@dp.callback_query(lambda c: c.data and c.data.startswith('reject_'))
async def reject_payout(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет прав.", show_alert=True)
        return
    payout_id = int(callback.data.split('_')[1])
    payout = get_payout(payout_id)
    if not payout:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    user_id, nft, wallet, admin_msg_id, code, link = payout

    await callback.message.answer("✏️ Введите причину отклонения:")
    await callback.answer()
    await state.set_state(AdminStates.waiting_reason)
    await state.update_data(payout_id=payout_id)

@dp.message(AdminStates.waiting_reason)
async def process_reject_reason(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.")
        return
    reason = message.text.strip()
    if not reason:
        await message.answer("❌ Причина не может быть пустой.")
        return

    data = await state.get_data()
    payout_id = data.get('payout_id')
    if not payout_id:
        await message.answer("❌ Ошибка, попробуйте /admin заново.")
        await state.clear()
        return

    payout = get_payout(payout_id)
    if not payout:
        await message.answer("❌ Заявка не найдена.")
        await state.clear()
        return
    user_id, nft, wallet, admin_msg_id, code, link = payout

    update_payout(payout_id, 0, 'rejected', message.from_user.id, admin_msg_id)

    try:
        await bot.send_message(
            user_id,
            f"❌ *Ваша выплата отклонена.*\n\n"
            f"🖼 NFT: {nft}\n"
            f"💳 Кошелёк: `{wallet}`\n"
            f"Причина: {reason}",
            parse_mode="Markdown"
        )
    except:
        pass

    for admin_id in ADMIN_IDS:
        try:
            await bot.edit_message_text(
                f"❌ *Заявка #{payout_id} отклонена*\n"
                f"Причина: {reason}",
                chat_id=admin_id,
                message_id=admin_msg_id,
                parse_mode="Markdown"
            )
        except:
            pass

    await message.answer(f"❌ Заявка #{payout_id} отклонена. Причина: {reason}")
    await state.clear()

# ========== КОМАНДЫ ДЛЯ ГРУППЫ ==========
@dp.message(Command("topm"))
async def top_month(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    top = get_top_users('month')
    if not top:
        await message.answer("📊 За месяц нет данных.")
        return
    text = "🏆 *ТОП за месяц*\n━━━━━━━━━━━━━━\n"
    for i, (name, username, total) in enumerate(top, 1):
        user_str = f"@{username}" if username else name
        text += f"{i}. {user_str} — {total:.2f} TON\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("topn"))
async def top_week(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    top = get_top_users('week')
    if not top:
        await message.answer("📊 За неделю нет данных.")
        return
    text = "🏆 *ТОП за неделю*\n━━━━━━━━━━━━━━\n"
    for i, (name, username, total) in enumerate(top, 1):
        user_str = f"@{username}" if username else name
        text += f"{i}. {user_str} — {total:.2f} TON\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("stat"))
async def bot_stats(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    total_users, total_payouts, total_amount = get_stats()
    text = (
        f"📊 *Статистика бота*\n"
        f"━━━━━━━━━━━━━━\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"📦 Всего заявок: {total_payouts}\n"
        f"💰 Выплачено всего: {total_amount:.2f} TON"
    )
    await message.answer(text, parse_mode="Markdown")

# ========== КОМАНДЫ УПРАВЛЕНИЯ ==========
@dp.message(Command("ban"))
async def ban_user(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.")
        return
    if not message.reply_to_message:
        await message.answer("Ответьте на сообщение пользователя.")
        return
    user_id = message.reply_to_message.from_user.id
    if user_id in ADMIN_IDS:
        await message.answer("⛔ Нельзя забанить админа.")
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
        await message.answer("Укажите ID или @username")
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
        await message.answer("❌ Нет пользователей.")
        await state.clear()
        return
    sent = 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 *Рассылка:*\n\n{text}", parse_mode="Markdown")
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
        await message.answer("Ответьте на сообщение.")
        return
    user_id = message.reply_to_message.from_user.id
    try:
        await bot.restrict_chat_member(message.chat.id, user_id, permissions=ChatPermissions(can_send_messages=False))
        await message.answer(f"🔇 Пользователь {user_id} заглушен.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ========== ЗАПУСК ==========
async def on_startup():
    webhook_url = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
    await bot.set_webhook(webhook_url)
    logging.info(f"Webhook установлен на {webhook_url}")

async def main():
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    await on_startup()
    logging.info(f"✅ Бот запущен, слушает порт {PORT}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
