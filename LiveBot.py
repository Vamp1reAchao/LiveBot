import os
import json
import sqlite3
import pytz
import nest_asyncio
nest_asyncio.apply()
from datetime import datetime
from typing import Dict, List, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    JobQueue,
)
from telegram.ext import filters
from telegram.error import BadRequest

SELECTING_TOPIC, WRITING_MESSAGE, ADMIN_RESPONSE, BROADCAST_MESSAGE, ADDING_ADMIN, CREATING_TOPIC = range(6)

if not os.path.exists('config.json'):
    with open('config.json', 'w') as f:
        json.dump({"BOT_TOKEN": "YOUR_BOT_TOKEN", "ADMIN_ID": 123456789}, f)
    print("Пожалуйста, заполните config.json перед запуском бота!")
    exit()

with open('config.json') as f:
    config = json.load(f)

BOT_TOKEN = config['BOT_TOKEN']
ADMIN_ID = config['ADMIN_ID']


def init_db():
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_banned BOOLEAN DEFAULT FALSE,
            registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS topics (
            topic_id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_name TEXT UNIQUE,
            description TEXT
        )
        ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            topic_id INTEGER,
            message_text TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_read BOOLEAN DEFAULT FALSE,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (topic_id) REFERENCES topics(topic_id)
        )
        ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS replies (
            reply_id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            admin_id INTEGER,
            reply_text TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES messages(message_id),
            FOREIGN KEY (admin_id) REFERENCES users(user_id)
        )
        ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            admin_id INTEGER PRIMARY KEY,
            username TEXT,
            added_by INTEGER,
            added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (admin_id) REFERENCES users(user_id),
            FOREIGN KEY (added_by) REFERENCES users(user_id)
        )
        ''')

    default_topics = [
        ("Общие вопросы", "Вопросы общего характера"),
        ("Техническая поддержка", "Проблемы с использованием сервиса"),
        ("Предложения", "Предложения по улучшению"),
        ("Жалобы", "Жалобы на работу сервиса или сотрудников")
    ]

    cursor.execute("SELECT COUNT(*) FROM topics")
    if cursor.fetchone()[0] == 0:
        cursor.executemany("INSERT INTO topics (topic_name, description) VALUES (?, ?)", default_topics)

    cursor.execute("SELECT 1 FROM admins WHERE admin_id = ?", (ADMIN_ID,))
    if not cursor.fetchone():
        cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (ADMIN_ID,))
        cursor.execute("INSERT INTO admins (admin_id, added_by) VALUES (?, ?)", (ADMIN_ID, ADMIN_ID))

    conn.commit()
    conn.close()


init_db()


def get_user(user_id: int) -> Optional[Dict]:
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()

    if user:
        return {
            "user_id": user[0],
            "username": user[1],
            "first_name": user[2],
            "last_name": user[3],
            "is_banned": bool(user[4]),
            "registration_date": user[5]
        }
    return None


def add_user(user_id: int, username: str, first_name: str, last_name: str):
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
        (user_id, username, first_name, last_name)
    )
    conn.commit()
    conn.close()


def get_topics() -> List[Dict]:
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM topics")
    topics = [{"topic_id": row[0], "topic_name": row[1], "description": row[2]} for row in cursor.fetchall()]
    conn.close()
    return topics


def add_topic(topic_name: str, description: str):
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO topics (topic_name, description) VALUES (?, ?)", (topic_name, description))
    conn.commit()
    conn.close()


def add_message(user_id: int, topic_id: int, message_text: str):
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (user_id, topic_id, message_text) VALUES (?, ?, ?)",
        (user_id, topic_id, message_text)
    )
    message_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return message_id


def get_user_messages(user_id: int, page: int = 1, per_page: int = 5) -> List[Dict]:
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    offset = (page - 1) * per_page
    cursor.execute('''
            SELECT m.message_id, m.message_text, m.timestamp, t.topic_name, 
                   (SELECT COUNT(*) FROM replies WHERE message_id = m.message_id) as reply_count
            FROM messages m
            JOIN topics t ON m.topic_id = t.topic_id
            WHERE m.user_id = ?
            ORDER BY m.timestamp DESC
            LIMIT ? OFFSET ?
        ''', (user_id, per_page, offset))

    messages = []
    for row in cursor.fetchall():
        messages.append({
            "message_id": row[0],
            "message_text": row[1],
            "timestamp": row[2],
            "topic_name": row[3],
            "reply_count": row[4]
        })

    conn.close()
    return messages


def get_message_details(message_id: int) -> Optional[Dict]:
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT m.message_id, m.user_id, m.message_text, m.timestamp, 
               t.topic_name, u.username, u.first_name, u.last_name
        FROM messages m
        JOIN topics t ON m.topic_id = t.topic_id
        JOIN users u ON m.user_id = u.user_id
        WHERE m.message_id = ?
    ''', (message_id,))

    message = cursor.fetchone()
    if not message:
        conn.close()
        return None

    cursor.execute('''
        SELECT r.reply_text, r.timestamp, u.username, u.first_name, u.last_name
        FROM replies r
        JOIN users u ON r.admin_id = u.user_id
        WHERE r.message_id = ?
        ORDER BY r.timestamp
    ''', (message_id,))

    replies = []
    for reply in cursor.fetchall():
        replies.append({
            "text": reply[0],
            "timestamp": reply[1],
            "username": reply[2],
            "first_name": reply[3],
            "last_name": reply[4]
        })

    conn.close()

    return {
        "message_id": message[0],  # Исправлено: убрана лишняя кавычка
        "user_id": message[1],
        "message_text": message[2],
        "timestamp": message[3],  # Исправлено: используем правильный индекс
        "topic_name": message[4],
        "username": message[5],
        "first_name": message[6],
        "last_name": message[7],
        "replies": replies
    }


def add_reply(message_id: int, admin_id: int, reply_text: str):
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO replies (message_id, admin_id, reply_text) VALUES (?, ?, ?)",
        (message_id, admin_id, reply_text)
    )
    cursor.execute("UPDATE messages SET is_read = TRUE WHERE message_id = ?", (message_id,))
    conn.commit()
    conn.close()


def get_all_messages(page: int = 1, per_page: int = 10) -> List[Dict]:
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    offset = (page - 1) * per_page
    cursor.execute('''
            SELECT m.message_id, m.message_text, m.timestamp, m.is_read,
                   t.topic_name, u.user_id, u.username, u.first_name, u.last_name,
                   (SELECT COUNT(*) FROM replies WHERE message_id = m.message_id) as reply_count
            FROM messages m
            JOIN topics t ON m.topic_id = t.topic_id
            JOIN users u ON m.user_id = u.user_id
            ORDER BY m.timestamp DESC
            LIMIT ? OFFSET ?
        ''', (per_page, offset))

    messages = []
    for row in cursor.fetchall():
        messages.append({
            "message_id": row[0],
            "message_text": row[1],
            "timestamp": row[2],
            "is_read": bool(row[3]),
            "topic_name": row[4],
            "user_id": row[5],
            "username": row[6],
            "first_name": row[7],
            "last_name": row[8],
            "reply_count": row[9]
        })

    conn.close()
    return messages


def get_total_messages_count() -> int:
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM messages")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_all_users() -> List[Dict]:
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username, first_name, last_name FROM users WHERE is_banned = FALSE")
    users = [{"user_id": row[0], "username": row[1], "first_name": row[2], "last_name": row[3]} for row in
             cursor.fetchall()]
    conn.close()
    return users


def ban_user(user_id: int):
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_banned = TRUE WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def unban_user(user_id: int):
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_banned = FALSE WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM admins WHERE admin_id = ?", (user_id,))
    result = bool(cursor.fetchone())
    conn.close()
    return result


def add_admin(admin_id: int, added_by: int, username: str = None):
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (admin_id, username))
    cursor.execute("INSERT OR IGNORE INTO admins (admin_id, added_by, username) VALUES (?, ?, ?)",
                   (admin_id, added_by, username))
    conn.commit()
    conn.close()


def get_all_admins() -> List[Dict]:
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute('''
            SELECT a.admin_id, u.username, u.first_name, u.last_name, a.added_date
            FROM admins a
            JOIN users u ON a.admin_id = u.user_id
        ''')
    admins = [{"admin_id": row[0], "username": row[1], "first_name": row[2], "last_name": row[3], "added_date": row[4]}
              for row in cursor.fetchall()]
    conn.close()
    return admins


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username, user.first_name, user.last_name)

    try:
        photo_url = "https://via.placeholder.com/600x400?text=Welcome+to+Feedback+Bot"
        caption = (
            f"👋 Привет, {user.first_name}!\n\n"
            "Я бот для обратной связи. С моей помощью ты можешь:\n"
            "📨 Написать сообщение администрации\n"
            "📖 Просмотреть историю своих обращений\n"
            "👤 Управлять своим профилем\n\n"
            "Выбери действие в меню ниже:"
        )

        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=photo_url,
            caption=caption,
            reply_markup=main_menu_keyboard()
        )
    except BadRequest:
        await update.message.reply_text(
            f"👋 Привет, {user.first_name}!\n\nЯ бот для обратной связи. Выбери действие в меню ниже:",
            reply_markup=main_menu_keyboard()
        )


def main_menu_keyboard():
    keyboard = [
        [KeyboardButton("📨 Написать сообщение")],
        [KeyboardButton("📖 История диалогов"), KeyboardButton("👤 Мой профиль")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def admin_menu_keyboard():
    keyboard = [
        [KeyboardButton("📂 Все диалоги"), KeyboardButton("📢 Рассылка")],
        [KeyboardButton("👥 Управление админами"), KeyboardButton("📝 Управление темами")],
        [KeyboardButton("🔙 В главное меню")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_admin(user.id):
        await update.message.reply_text("Админ-меню:", reply_markup=admin_menu_keyboard())
    else:
        await update.message.reply_text("Главное меню:", reply_markup=main_menu_keyboard())


async def write_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topics = get_topics()
    keyboard = []

    for topic in topics:
        keyboard.append([InlineKeyboardButton(
            f"{topic['topic_name']} - {topic['description']}",
            callback_data=f"select_topic_{topic['topic_id']}"
        )])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_topic_selection")])

    await update.message.reply_text(
        "📝 Выберите тему для вашего сообщения:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return SELECTING_TOPIC


async def select_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_topic_selection":
        await query.edit_message_text("Вы отменили создание сообщения.")
        return ConversationHandler.END

    topic_id = int(query.data.split("_")[-1])
    context.user_data['selected_topic'] = topic_id

    topics = get_topics()
    topic_name = next((t['topic_name'] for t in topics if t['topic_id'] == topic_id), "Неизвестная тема")

    await query.edit_message_text(f"Вы выбрали тему: <b>{topic_name}</b>\n\nТеперь напишите ваше сообщение:",
                                  parse_mode='HTML')
    return WRITING_MESSAGE


async def receive_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text
    topic_id = context.user_data['selected_topic']
    user_id = update.effective_user.id

    message_id = add_message(user_id, topic_id, message_text)

    admins = get_all_admins()
    for admin in admins:
        try:
            await context.bot.send_message(
                chat_id=admin['admin_id'],
                text=f"📨 Новое сообщение от пользователя {update.effective_user.full_name}:\n\n{message_text}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✍ Ответить", callback_data=f"reply_{message_id}")]
                ])
            )
        except Exception as e:
            print(f"Не удалось отправить уведомление администратору {admin['admin_id']}: {e}")

    await update.message.reply_text(
        "✅ Ваше сообщение отправлено администраторам. Ожидайте ответа.",
        reply_markup=main_menu_keyboard()
    )

    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def message_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    messages = get_user_messages(user_id)

    if not messages:
        await update.message.reply_text("📭 У вас пока нет отправленных сообщений.", reply_markup=main_menu_keyboard())
        return

    response = "📖 История ваших сообщений:\n\n"
    for msg in messages:
        response += (
            f"📌 Тема: {msg['topic_name']}\n"
            f"📅 Дата: {msg['timestamp']}\n"
            f"💬 Сообщение: {msg['message_text']}\n"
            f"🗨 Ответов: {msg['reply_count']}\n\n"
        )

    await update.message.reply_text(response, reply_markup=main_menu_keyboard())


async def user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Произошла ошибка при загрузке профиля.")
        return

    status = "🔴 Заблокирован" if user['is_banned'] else "🟢 Активен"
    registration_date = user['registration_date'].split('.')[0] if isinstance(user['registration_date'], str) else user[
        'registration_date']

    response = (
        f"👤 Ваш профиль:\n\n"
        f"🆔 ID: {user['user_id']}\n"
        f"👤 Имя: {user['first_name'] or 'Не указано'} {user['last_name'] or ''}\n"
        f"📛 Юзернейм: @{user['username'] or 'не указан'}\n"
        f"📅 Дата регистрации: {registration_date}\n"
        f"🔐 Статус: {status}\n\n"
    )

    keyboard = []
    if user['is_banned']:
        keyboard.append([InlineKeyboardButton("🟢 Разблокировать себя", callback_data=f"unban_me_{user['user_id']}")])
    else:
        keyboard.append([InlineKeyboardButton("🔴 Заблокировать себя", callback_data=f"ban_me_{user['user_id']}")])

    await update.message.reply_text(
        response,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
    )


async def ban_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = int(query.data.split("_")[-1])
    if user_id != query.from_user.id:
        await query.edit_message_text("Вы не можете выполнить это действие для другого пользователя.")
        return

    ban_user(user_id)
    await query.edit_message_text("🔴 Вы заблокировали себя. Теперь вы не можете отправлять сообщения.")

    await context.bot.edit_message_reply_markup(
        chat_id=query.message.chat_id,
        message_id=query.message.message_id,
        reply_markup=None
    )


async def unban_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = int(query.data.split("_")[-1])
    if user_id != query.from_user.id:
        await query.edit_message_text("Вы не можете выполнить это действие для другого пользователя.")
        return

    unban_user(user_id)
    await query.edit_message_text("🟢 Вы разблокировали себя. Теперь вы можете отправлять сообщения.")

    await context.bot.edit_message_reply_markup(
        chat_id=query.message.chat_id,
        message_id=query.message.message_id,
        reply_markup=None
    )


async def admin_all_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав доступа к этой команде.")
        return

    page = 1
    if context.args and context.args[0].isdigit():
        page = int(context.args[0])

    messages = get_all_messages(page)
    total_messages = get_total_messages_count()
    total_pages = (total_messages + 9) // 10

    if not messages:
        await update.message.reply_text("📭 Нет сообщений от пользователей.", reply_markup=admin_menu_keyboard())
        return

    response = f"📂 Все диалоги (Страница {page}/{total_pages}):\n\n"
    for msg in messages:
        status = "🟢" if msg['is_read'] else "🔴"
        response += (
            f"{status} #{msg['message_id']} - {msg['first_name']} {msg['last_name']} (@{msg['username'] or 'нет'})\n"
            f"📌 Тема: {msg['topic_name']}\n"
            f"📅 Дата: {msg['timestamp']}\n"
            f"💬 Сообщение: {msg['message_text'][:50]}...\n"
            f"🗨 Ответов: {msg['reply_count']}\n\n"
        )

    keyboard = []
    row = []
    if page > 1:
        row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{page - 1}"))
    if page < total_pages:
        row.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"page_{page + 1}"))
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="back_to_admin_menu")])

    await update.message.reply_text(
        response,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back_to_admin_menu":
        await query.edit_message_text("Админ-меню:", reply_markup=admin_menu_keyboard())
        return

    page = int(query.data.split("_")[1])
    messages = get_all_messages(page)
    total_messages = get_total_messages_count()
    total_pages = (total_messages + 9) // 10

    response = f"📂 Все диалоги (Страница {page}/{total_pages}):\n\n"
    for msg in messages:
        status = "🟢" if msg['is_read'] else "🔴"
        response += (
            f"{status} #{msg['message_id']} - {msg['first_name']} {msg['last_name']} (@{msg['username'] or 'нет'})\n"
            f"📌 Тема: {msg['topic_name']}\n"
            f"📅 Дата: {msg['timestamp']}\n"
            f"💬 Сообщение: {msg['message_text'][:50]}...\n"
            f"🗨 Ответов: {msg['reply_count']}\n\n"
        )

    keyboard = []
    row = []
    if page > 1:
        row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{page - 1}"))
    if page < total_pages:
        row.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"page_{page + 1}"))
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="back_to_admin_menu")])

    await query.edit_message_text(
        response,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    message_id = int(query.data.split("_")[1])
    message_details = get_message_details(message_id)

    if not message_details:
        await query.edit_message_text("Сообщение не найдено.")
        return

    context.user_data['replying_to'] = message_id
    context.user_data['replying_user'] = message_details['user_id']

    response = (
        f"✍ Ответ на сообщение #{message_id}\n\n"
        f"👤 От: {message_details['first_name']} {message_details['last_name']} (@{message_details['username'] or 'нет'})\n"
        f"📌 Тема: {message_details['topic_name']}\n"
        f"📅 Дата: {message_details['timestamp']}\n\n"
        f"💬 Сообщение:\n{message_details['message_text']}\n\n"
    )

    if message_details['replies']:
        response += "📨 История ответов:\n"
        for reply in message_details['replies']:
            response += f"\n👨‍💼 {reply['first_name']} {reply['last_name']} (@{reply['username'] or 'нет'}):\n{reply['text']}\n"

    await query.edit_message_text(
        response + "\n\nВведите ваш ответ:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_reply")]])
    )

    return ADMIN_RESPONSE


async def admin_receive_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_text = update.message.text
    message_id = context.user_data['replying_to']
    user_id = context.user_data['replying_user']
    admin_id = update.effective_user.id

    add_reply(message_id, admin_id, reply_text)

    try:
        admin = get_user(admin_id)
        admin_name = f"{admin['first_name']} {admin['last_name']}" if admin else "Администратор"

        await context.bot.send_message(
            chat_id=user_id,
            text=f"📨 Вы получили ответ от {admin_name}:\n\n{reply_text}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✍ Ответить", callback_data=f"reply_{message_id}")]
            ])
        )
    except Exception as e:
        print(f"Не удалось отправить ответ пользователю {user_id}: {e}")

    await update.message.reply_text(
        "✅ Ваш ответ отправлен пользователю.",
        reply_markup=admin_menu_keyboard()
    )

    return ConversationHandler.END


async def admin_cancel_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("Ответ отменен.", reply_markup=admin_menu_keyboard())
    return ConversationHandler.END


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав доступа к этой команде.")
        return

    await update.message.reply_text(
        "📢 Введите сообщение для рассылки всем пользователям:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_broadcast")]])
    )

    return BROADCAST_MESSAGE


async def admin_receive_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    broadcast_text = update.message.text
    users = get_all_users()
    admin = get_user(update.effective_user.id)
    admin_name = f"{admin['first_name']} {admin['last_name']}" if admin else "Администратор"

    success = 0
    failed = 0

    for user in users:
        try:
            await context.bot.send_message(
                chat_id=user['user_id'],
                text=f"📢 Объявление от {admin_name}:\n\n{broadcast_text}"
            )
            success += 1
        except Exception as e:
            print(f"Не удалось отправить сообщение пользователю {user['user_id']}: {e}")
            failed += 1

    await update.message.reply_text(
        f"✅ Рассылка завершена:\n\nУспешно: {success}\nНе удалось: {failed}",
        reply_markup=admin_menu_keyboard()
    )

    return ConversationHandler.END


async def admin_cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("Рассылка отменена.", reply_markup=admin_menu_keyboard())
    return ConversationHandler.END


async def admin_manage_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав доступа к этой команде.")
        return

    admins = get_all_admins()
    response = "👥 Список администраторов:\n\n"

    for admin in admins:
        response += (
            f"🆔 ID: {admin['admin_id']}\n"
            f"👤 Имя: {admin['first_name'] or 'Не указано'} {admin['last_name'] or ''}\n"
            f"📛 Юзернейм: @{admin['username'] or 'нет'}\n"
            f"📅 Дата добавления: {admin['added_date']}\n\n"
        )

    keyboard = [
        [InlineKeyboardButton("➕ Добавить админа", callback_data="add_admin")],
        [InlineKeyboardButton("➖ Удалить админа", callback_data="remove_admin")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_admin_menu")]
    ]

    await update.message.reply_text(
        response,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Введите ID пользователя, которого хотите сделать администратором:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_admin")]])
    )

    return ADDING_ADMIN


async def admin_receive_new_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_admin_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Неверный формат ID. Пожалуйста, введите числовой ID.")
        return ADDING_ADMIN

    if is_admin(new_admin_id):
        await update.message.reply_text("Этот пользователь уже является администратором.")
        return ADDING_ADMIN

    add_admin(new_admin_id, update.effective_user.id)
    await update.message.reply_text(
        f"✅ Пользователь {new_admin_id} добавлен в администраторы.",
        reply_markup=admin_menu_keyboard()
    )

    return ConversationHandler.END


async def admin_cancel_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("Добавление администратора отменено.", reply_markup=admin_menu_keyboard())
    return ConversationHandler.END


async def admin_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    admins = get_all_admins()
    keyboard = []

    for admin in admins:
        if admin['admin_id'] != query.from_user.id:
            keyboard.append([
                InlineKeyboardButton(
                    f"{admin['first_name']} {admin['last_name']} (@{admin['username'] or 'нет'}) - ID: {admin['admin_id']}",
                    callback_data=f"remove_admin_{admin['admin_id']}"
                )
            ])

    if not keyboard:
        await query.edit_message_text("Нет других администраторов для удаления.")
        return

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_remove_admin")])

    await query.edit_message_text(
        "Выберите администратора для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_confirm_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    admin_id = int(query.data.split("_")[-1])
    context.user_data['admin_to_remove'] = admin_id

    admin = get_user(admin_id)
    admin_name = f"{admin['first_name']} {admin['last_name']}" if admin else f"ID: {admin_id}"

    await query.edit_message_text(
        f"Вы уверены, что хотите удалить администратора {admin_name}?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да", callback_data=f"confirm_remove_{admin_id}")],
            [InlineKeyboardButton("❌ Нет", callback_data="cancel_remove_admin")]
        ])
    )


async def admin_remove_admin_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    admin_id = int(query.data.split("_")[-1])

    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM admins WHERE admin_id = ?", (admin_id,))
    conn.commit()
    conn.close()

    await query.edit_message_text(
        f"✅ Администратор {admin_id} удален.",
        reply_markup=admin_menu_keyboard()
    )


async def admin_cancel_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("Удаление администратора отменено.", reply_markup=admin_menu_keyboard())


async def admin_manage_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав доступа к этой команде.")
        return

    topics = get_topics()
    response = "📝 Список тем для сообщений:\n\n"

    for topic in topics:
        response += (
            f"📌 {topic['topic_name']}\n"
            f"🔹 Описание: {topic['description']}\n"
            f"🆔 ID: {topic['topic_id']}\n\n"
        )

    keyboard = [
        [InlineKeyboardButton("➕ Добавить тему", callback_data="add_topic")],
        [InlineKeyboardButton("➖ Удалить тему", callback_data="remove_topic")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_admin_menu")]
    ]

    await update.message.reply_text(
        response,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Введите название новой темы:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_topic")]])
    )

    return CREATING_TOPIC


async def admin_receive_topic_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic_name = update.message.text
    context.user_data['new_topic_name'] = topic_name

    await update.message.reply_text(
        "Теперь введите описание для этой темы:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_topic")]])
    )

    return CREATING_TOPIC + 1


async def admin_receive_topic_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    description = update.message.text
    topic_name = context.user_data['new_topic_name']

    add_topic(topic_name, description)

    await update.message.reply_text(
        f"✅ Тема '{topic_name}' успешно добавлена.",
        reply_markup=admin_menu_keyboard()
    )

    return ConversationHandler.END


async def admin_cancel_add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("Добавление темы отменено.", reply_markup=admin_menu_keyboard())
    return ConversationHandler.END


async def admin_remove_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    topics = get_topics()
    keyboard = []

    for topic in topics:
        keyboard.append([
            InlineKeyboardButton(
                f"{topic['topic_name']} (ID: {topic['topic_id']})",
                callback_data=f"remove_topic_{topic['topic_id']}"
            )
        ])

    if not keyboard:
        await query.edit_message_text("Нет тем для удаления.")
        return

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_remove_topic")])

    await query.edit_message_text(
        "Выберите тему для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_confirm_remove_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    topic_id = int(query.data.split("_")[-1])
    context.user_data['topic_to_remove'] = topic_id

    topic = next((t for t in get_topics() if t['topic_id'] == topic_id), None)
    if not topic:
        await query.edit_message_text("Тема не найдена.")
        return

    await query.edit_message_text(
        f"Вы уверены, что хотите удалить тему '{topic['topic_name']}'?\n\n"
        "⚠️ Все сообщения с этой темой будут потеряны!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да", callback_data=f"confirm_remove_topic_{topic_id}")],
            [InlineKeyboardButton("❌ Нет", callback_data="cancel_remove_topic")]
        ])
    )


async def admin_remove_topic_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    topic_id = int(query.data.split("_")[-1])

    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()

    cursor.execute("DELETE FROM messages WHERE topic_id = ?", (topic_id,))
    cursor.execute("DELETE FROM topics WHERE topic_id = ?", (topic_id,))

    conn.commit()
    conn.close()

    await query.edit_message_text(
        "✅ Тема и все связанные сообщения удалены.",
        reply_markup=admin_menu_keyboard()
    )


async def admin_cancel_remove_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("Удаление темы отменено.", reply_markup=admin_menu_keyboard())


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Ошибка: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text("Произошла ошибка. Пожалуйста, попробуйте позже.")


async def main():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    # Создаём JobQueue
    job_queue = JobQueue()
    # Настраиваем scheduler с часовым поясом
    job_queue.scheduler = AsyncIOScheduler(timezone=pytz.timezone('Europe/Moscow'))

    app = ApplicationBuilder().token(BOT_TOKEN).job_queue(job_queue).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", back_to_menu))

    app.add_handler(MessageHandler(filters.Regex('^🔩 В главное меню$'), back_to_menu))

    write_message_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^📨 Написать сообщение$'), write_message)],
        states={
            SELECTING_TOPIC: [CallbackQueryHandler(select_topic)],
            WRITING_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_message)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    app.add_handler(write_message_conv)

    app.add_handler(MessageHandler(filters.Regex('^📖 История диалогов$'), message_history))
    app.add_handler(MessageHandler(filters.Regex('^👤 Мой профиль$'), user_profile))
    app.add_handler(CallbackQueryHandler(ban_me, pattern="^ban_me_"))
    app.add_handler(CallbackQueryHandler(unban_me, pattern="^unban_me_"))

    app.add_handler(MessageHandler(filters.Regex('^📂 Все диалоги$'), admin_all_dialogs))
    app.add_handler(CallbackQueryHandler(admin_page_callback, pattern="^page_"))
    app.add_handler(CallbackQueryHandler(admin_reply_callback, pattern="^reply_"))

    reply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_reply_callback, pattern="^reply_")],
        states={
            ADMIN_RESPONSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_reply)],
        },
        fallbacks=[CallbackQueryHandler(admin_cancel_reply, pattern="^cancel_reply$")],
    )
    app.add_handler(reply_conv)

    app.add_handler(MessageHandler(filters.Regex('^📢 Рассылка$'), admin_broadcast))
    broadcast_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^📢 Рассылка$'), admin_broadcast)],
        states={
            BROADCAST_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_broadcast)],
        },
        fallbacks=[CallbackQueryHandler(admin_cancel_broadcast, pattern="^cancel_broadcast$")],
    )
    app.add_handler(broadcast_conv)

    app.add_handler(MessageHandler(filters.Regex('^👥 Управление админами$'), admin_manage_admins))
    app.add_handler(CallbackQueryHandler(admin_add_admin, pattern="^add_admin$"))

    add_admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_admin, pattern="^add_admin$")],
        states={
            ADDING_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_new_admin)],
        },
        fallbacks=[CallbackQueryHandler(admin_cancel_add_admin, pattern="^cancel_add_admin$")],
    )
    app.add_handler(add_admin_conv)

    app.add_handler(CallbackQueryHandler(admin_remove_admin, pattern="^remove_admin$"))
    app.add_handler(CallbackQueryHandler(admin_confirm_remove_admin, pattern="^remove_admin_"))
    app.add_handler(CallbackQueryHandler(admin_remove_admin_confirm, pattern="^confirm_remove_"))
    app.add_handler(CallbackQueryHandler(admin_cancel_remove_admin, pattern="^cancel_remove_admin$"))

    app.add_handler(MessageHandler(filters.Regex('^📝 Управление темами$'), admin_manage_topics))
    app.add_handler(CallbackQueryHandler(admin_add_topic, pattern="^add_topic$"))

    add_topic_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_topic, pattern="^add_topic$")],
        states={
            CREATING_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_topic_name)],
            CREATING_TOPIC + 1: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_topic_description)],
        },
        fallbacks=[CallbackQueryHandler(admin_cancel_add_topic, pattern="^cancel_add_topic$")],
    )
    app.add_handler(add_topic_conv)

    app.add_handler(CallbackQueryHandler(admin_remove_topic, pattern="^remove_topic$"))
    app.add_handler(CallbackQueryHandler(admin_confirm_remove_topic, pattern="^remove_topic_"))
    app.add_handler(CallbackQueryHandler(admin_remove_topic_confirm, pattern="^confirm_remove_topic_"))
    app.add_handler(CallbackQueryHandler(admin_cancel_remove_topic, pattern="^cancel_remove_topic$"))

    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_admin_menu$"))

    app.add_error_handler(error_handler)

    await app.run_polling()


if __name__ == '__main__':
    import asyncio

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
