import logging
import sys
import os
import signal
import json
import sqlite3
from contextlib import contextmanager
from uuid import uuid4
from typing import Dict, List, Optional
from datetime import datetime

logging.basicConfig(level=logging.CRITICAL)
logger = logging.getLogger()
logger.handlers = []
logger.addHandler(logging.NullHandler())

@contextmanager
def suppress_stderr():
    with open(os.devnull, 'w') as devnull:
        old_stderr = sys.stderr
        sys.stderr = devnull
        try:
            yield
        finally:
            sys.stderr = old_stderr

import nest_asyncio
nest_asyncio.apply()

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto,
    InputFile,
    Document,
    Voice
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters
)
from telegram.error import BadRequest

(
    SELECTING_TOPIC, WRITING_MESSAGE, CONFIRM_ANONYMITY, ADMIN_RESPONSE,
    BROADCAST_MESSAGE, ADDING_ADMIN, CREATING_TOPIC, ADDING_FAQ,
    SEARCHING_FAQ, MANAGING_PRIORITY, ADDING_NOTE, REASSIGNING_DIALOG,
    RATING_RESPONSE, RECEIVING_RATING_COMMENT
) = range(14)

STATUS_NEW = "new"
STATUS_IN_PROGRESS = "in_progress"
STATUS_RESOLVED = "resolved"
STATUS_CLOSED = "closed"

PRIORITY_LOW = "low"
PRIORITY_NORMAL = "normal"
PRIORITY_HIGH = "high"
PRIORITY_URGENT = "urgent"

if not os.path.exists('config.json'):
    with open('config.json', 'w') as f:
        json.dump({
            "BOT_TOKEN": "YOUR_BOT_TOKEN",
            "ADMIN_ID": 123456789,
            "MAX_ATTACHMENTS": 5,
            "MAX_URGENT_PER_DAY": 3,
            "SUPPORTED_LANGUAGES": ["ru", "en"],
            "DEFAULT_LANGUAGE": "ru"
        }, f)
    exit()

with open('config.json') as f:
    config = json.load(f)

BOT_TOKEN = config['BOT_TOKEN']
ADMIN_ID = config['ADMIN_ID']
MAX_ATTACHMENTS = config.get('MAX_ATTACHMENTS', 5)
MAX_URGENT_PER_DAY = config.get('MAX_URGENT_PER_DAY', 3)
SUPPORTED_LANGUAGES = config.get('SUPPORTED_LANGUAGES', ["ru"])
DEFAULT_LANGUAGE = config.get('DEFAULT_LANGUAGE', "ru")

if not os.path.exists('attachments'):
    os.makedirs('attachments')

def init_db():
    conn = sqlite3.connect('feedback.db')
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS faq")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_banned BOOLEAN DEFAULT FALSE,
            registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            language TEXT DEFAULT 'ru',
            urgent_messages_today INTEGER DEFAULT 0,
            last_urgent_date TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS topics (
            topic_id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_name TEXT UNIQUE,
            description TEXT,
            is_quick_action BOOLEAN DEFAULT FALSE
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
            status TEXT DEFAULT 'new',
            priority TEXT DEFAULT 'normal',
            is_anonymous BOOLEAN DEFAULT FALSE,
            assigned_admin_id INTEGER,
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

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attachments (
            attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            file_id TEXT NOT NULL,
            file_type TEXT,
            file_path TEXT,
            FOREIGN KEY (message_id) REFERENCES messages(message_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ratings (
            rating_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            admin_id INTEGER,
            rating INTEGER,
            comments TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (admin_id) REFERENCES users(user_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS faq (
            faq_id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT,
            answer TEXT,
            topic_id INTEGER,
            keywords TEXT,
            FOREIGN KEY (topic_id) REFERENCES topics(topic_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notes (
            note_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            admin_id INTEGER,
            note_text TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (admin_id) REFERENCES users(user_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS message_status_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            status TEXT,
            admin_id INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES messages(message_id),
            FOREIGN KEY (admin_id) REFERENCES users(user_id)
        )
    ''')

    default_topics = [
        ("Общие вопросы", "Вопросы общего характера", False),
        ("Техническая помощь", "Проблемы с использованием сервиса", False),
        ("Предложения", "Предложения по улучшению", False),
        ("Жалобы", "Жалобы на работу сервиса или сотрудников", False),
        ("Сообщить об ошибке", "Критическая ошибка в работе сервиса", True),
        ("Вопрос по оплате", "Проблемы с платежами или возвратами", True),
        ("Срочный запрос", "Требуется немедленное внимание", True)
    ]

    cursor.execute("SELECT COUNT(*) FROM topics")
    if cursor.fetchall()[0][0] == 0:
        cursor.executemany(
            "INSERT INTO topics (topic_name, description, is_quick_action) VALUES (?, ?, ?)",
            default_topics
        )

    cursor.execute("SELECT 1 FROM admins WHERE admin_id = ?", (ADMIN_ID,))
    if not cursor.fetchone():
        cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (ADMIN_ID,))
        cursor.execute(
            "INSERT INTO admins (admin_id, added_by) VALUES (?, ?)",
            (ADMIN_ID, ADMIN_ID)
        )

    conn.commit()
    conn.close()

init_db()

def save_attachment(message_id: int, file_id: str, file_type: str, file_path: str = None):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO attachments (message_id, file_id, file_type, file_path) VALUES (?, ?, ?, ?)",
            (message_id, file_id, file_type, file_path)
        )
        conn.commit()
        conn.close()

def get_attachment(message_id: int) -> List[Dict]:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute(
            "SELECT attachment_id, file_id, file_type, file_path FROM attachments WHERE message_id = ?",
            (message_id,)
        )
        attachments = [
            {
                "attachment_id": row[0],
                "file_id": row[1],
                "file_type": row[2],
                "file_path": row[3]
            } for row in cursor.fetchall()
        ]
        conn.close()
        return attachments

def add_rating(user_id: int, admin_id: int, rating: int, comments: str = None):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO ratings (user_id, admin_id, rating, comments) VALUES (?, ?, ?, ?)",
            (user_id, admin_id, rating, comments)
        )
        conn.commit()
        conn.close()

def get_ratings(admin_id: int = None) -> List[Dict]:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()

        if admin_id:
            cursor.execute('''
                SELECT r.rating_id, r.rating, r.comments, r.timestamp,
                       u.user_id, u.first_name, u.last_name
                FROM ratings r
                JOIN users u ON r.user_id = u.user_id
                WHERE r.admin_id = ?
                ORDER BY r.timestamp DESC
            ''', (admin_id,))
        else:
            cursor.execute('''
                SELECT r.rating_id, r.rating, r.comments, r.timestamp,
                       u.user_id, u.first_name, u.last_name, a.admin_id, a.username
                FROM ratings r
                JOIN users u ON r.user_id = u.user_id
                JOIN admins a ON r.admin_id = a.admin_id
                ORDER BY r.timestamp DESC
            ''')

        ratings = [
            {
                "rating_id": row[0],
                "rating": row[1],
                "comments": row[2],
                "timestamp": row[3],
                "user_id": row[4],
                "user_name": f"{row[5]} {row[6]}",
                "admin_id": row[7] if not admin_id else admin_id,
                "admin_username": row[8] if not admin_id else None
            } for row in cursor.fetchall()
        ]
        conn.close()
        return ratings

def get_user_ratings(user_id: int) -> List[Dict]:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT r.rating_id, r.rating, r.comments, r.timestamp,
                   a.user_id, a.first_name, a.last_name
            FROM ratings r
            JOIN users a ON r.admin_id = a.user_id
            WHERE r.user_id = ?
            ORDER BY r.timestamp DESC
        ''', (user_id,))
        ratings = [
            {
                "rating_id": row[0],
                "rating": row[1],
                "comments": row[2],
                "timestamp": row[3],
                "admin_id": row[4],
                "admin_name": f"{row[5]} {row[6]}"
            } for row in cursor.fetchall()
        ]
        conn.close()
        return ratings

def add_faq(question: str, answer: str, topic_id: int = None):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO faq (question, answer, topic_id) VALUES (?, ?, ?)",
            (question, answer, topic_id)
        )
        conn.commit()
        conn.close()

def search_faq(query: str) -> List[Dict]:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()

        cursor.execute('''
            SELECT
                f.faq_id,
                f.question,
                f.answer,
                COALESCE(t.topic_name, 'Без темы') as topic_name
            FROM faq f
            LEFT JOIN topics t ON f.topic_id = t.topic_id
            WHERE f.question LIKE ?
               OR f.answer LIKE ?
               OR (f.keywords IS NOT NULL AND f.keywords LIKE ?)
        ''', (f"%{query}%", f"%{query}%", f"%{query}%"))

        results = [
            {
                "faq_id": row[0],
                "question": row[1],
                "answer": row[2],
                "topic_name": row[3]
            } for row in cursor.fetchall()
        ]
        conn.close()
        return results

def add_note(user_id: int, admin_id: int, note_text: str):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO notes (user_id, admin_id, note_text) VALUES (?, ?, ?)",
            (user_id, admin_id, note_text)
        )
        conn.commit()
        conn.close()

def get_notes(user_id: int) -> List[Dict]:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT n.note_id, n.note_text, n.timestamp, u.user_id, u.username, u.first_name, u.last_name
            FROM notes n
            JOIN users u ON n.admin_id = u.user_id
            WHERE n.user_id = ?
            ORDER BY n.timestamp DESC
        ''', (user_id,))

        notes = [
            {
                "note_id": row[0],
                "note_text": row[1],
                "timestamp": row[2],
                "admin_id": row[3],
                "admin_username": row[4],
                "admin_name": f"{row[5]} {row[6]}"
            } for row in cursor.fetchall()
        ]
        conn.close()
        return notes

def update_message_status(message_id: int, status: str, admin_id: int = None):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()

        cursor.execute(
            "UPDATE messages SET status = ? WHERE message_id = ?",
            (status, message_id)
        )

        cursor.execute(
            "INSERT INTO message_status_history (message_id, status, admin_id) VALUES (?, ?, ?)",
            (message_id, status, admin_id)
        )

        conn.commit()
        conn.close()

def get_message_status_history(message_id: int) -> List[Dict]:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT h.history_id, h.status, h.timestamp, u.user_id, u.username, u.first_name, u.last_name
            FROM message_status_history h
            LEFT JOIN users u ON h.admin_id = u.user_id
            WHERE h.message_id = ?
            ORDER BY h.timestamp DESC
        ''', (message_id,))

        history = [
            {
                "history_id": row[0],
                "status": row[1],
                "timestamp": row[2],
                "admin_id": row[3],
                "admin_username": row[4],
                "admin_name": f"{row[5]} {row[6]}" if row[5] else "Система"
            } for row in cursor.fetchall()
        ]
        conn.close()
        return history

def reassign_message(message_id: int, admin_id: int):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE messages SET assigned_admin_id = ? WHERE message_id = ?",
            (admin_id, message_id)
        )
        conn.commit()
        conn.close()

def can_send_urgent(user_id: int) -> bool:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()

        cursor.execute(
            "SELECT urgent_messages_today, last_urgent_date FROM users WHERE user_id = ?",
            (user_id,)
        )
        result = cursor.fetchone()

        if not result:
            return False

        count, last_date = result
        today = datetime.now().strftime("%Y-%m-%d")

        if last_date != today:
            cursor.execute(
                "UPDATE users SET urgent_messages_today = 0, last_urgent_date = ? WHERE user_id = ?",
                (today, user_id)
            )
            conn.commit()
            count = 0

        conn.close()
        return count < MAX_URGENT_PER_DAY

def increment_urgent_count(user_id: int):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")

        cursor.execute('''
            UPDATE users
            SET urgent_messages_today = urgent_messages_today + 1,
                last_urgent_date = ?
            WHERE user_id = ?
        ''', (today, user_id))

        conn.commit()
        conn.close()


def get_user(user_id: int, update_from_telegram: bool = True, context: ContextTypes.DEFAULT_TYPE = None) -> Optional[
    Dict]:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()

        if update_from_telegram and context:
            try:
                tg_user = context.bot.get_chat(user_id)
                if tg_user:
                    username = tg_user.username
                    first_name = tg_user.first_name
                    last_name = tg_user.last_name or ''

                    if user and (user[1] != username or user[2] != first_name or user[3] != last_name):
                        cursor.execute(
                            "UPDATE users SET username = ?, first_name = ?, last_name = ? WHERE user_id = ?",
                            (username, first_name, last_name, user_id)
                        )
                        conn.commit()
                        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                        user = cursor.fetchone()
            except Exception as e:
                logger.error(f"Error updating user data from Telegram: {e}")

        conn.close()

        if user:
            return {
                "user_id": user[0],
                "username": user[1],
                "first_name": user[2],
                "last_name": user[3],
                "is_banned": bool(user[4]),
                "registration_date": user[5],
                "language": user[6],
                "urgent_messages_today": user[7],
                "last_urgent_date": user[8]
            }
        return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user(user.id, update_from_telegram=True, context=context)

def update_user(user_id: int, username: str, first_name: str, last_name: str):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET username = ?, first_name = ?, last_name = ? WHERE user_id = ?",
            (username, first_name, last_name, user_id)
        )
        conn.commit()
        conn.close()


def add_user(user_id: int, username: str = None, first_name: str = None, last_name: str = None,
             update_from_telegram: bool = True, context: ContextTypes.DEFAULT_TYPE = None):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()

        if update_from_telegram and context:
            try:
                tg_user = context.bot.get_chat(user_id)
                if tg_user:
                    username = tg_user.username
                    first_name = tg_user.first_name
                    last_name = tg_user.last_name or ''
            except Exception as e:
                logger.error(f"Error getting user data from Telegram: {e}")

        cursor.execute(
            """INSERT OR REPLACE INTO users 
               (user_id, username, first_name, last_name) 
               VALUES (?, ?, ?, ?)""",
            (user_id, username, first_name, last_name)
        )
        conn.commit()
        conn.close()


async def check_user_updates(context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        user_ids = [row[0] for row in cursor.fetchall()]

        for user_id in user_ids:
            try:
                tg_user = await context.bot.get_chat(user_id)
                if tg_user:
                    cursor.execute(
                        """UPDATE users SET 
                           username = ?, first_name = ?, last_name = ?
                           WHERE user_id = ?""",
                        (tg_user.username, tg_user.first_name, tg_user.last_name or '', user_id)
                    )
            except Exception as e:
                logger.error(f"Error updating user {user_id}: {e}")

        conn.commit()
        conn.close()

def get_topics() -> List[Dict]:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM topics")
        topics = [{"topic_id": row[0], "topic_name": row[1], "description": row[2], "is_quick_action": bool(row[3])} for row in cursor.fetchall()]
        conn.close()
        return topics

def add_topic(topic_name: str, description: str):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute("INSERT INTO topics (topic_name, description) VALUES (?, ?)", (topic_name, description))
        conn.commit()
        conn.close()

def add_message(user_id: int, topic_id: int, message_text: str, is_anonymous: bool = False, priority: str = PRIORITY_NORMAL):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO messages (user_id, topic_id, message_text, is_anonymous, priority, assigned_admin_id, status) VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (user_id, topic_id, message_text, is_anonymous, priority, STATUS_NEW)
        )
        message_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return message_id

def get_user_messages(user_id: int, page: int = 1, per_page: int = 5) -> List[Dict]:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        offset = (page - 1) * per_page
        cursor.execute('''
            SELECT m.message_id, m.message_text, m.timestamp, t.topic_name,
                   (SELECT COUNT(*) FROM replies WHERE message_id = m.message_id) as reply_count,
                   m.status, m.priority
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
                "reply_count": row[4],
                "status": row[5],
                "priority": row[6]
            })

        conn.close()
        return messages

def get_message_details(message_id: int) -> Optional[Dict]:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT m.message_id, m.user_id, m.message_text, m.timestamp,
                   t.topic_name, u.username, u.first_name, u.last_name,
                   m.is_anonymous, m.status, m.priority, m.assigned_admin_id
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

        attachments = get_attachment(message_id)
        notes = get_notes(message[1])
        status_history = get_message_status_history(message_id)

        conn.close()

        return {
            "message_id": message[0],
            "user_id": message[1],
            "message_text": message[2],
            "timestamp": message[3],
            "topic_name": message[4],
            "username": message[5],
            "first_name": message[6],
            "last_name": message[7],
            "is_anonymous": bool(message[8]),
            "status": message[9],
            "priority": message[10],
            "assigned_admin_id": message[11],
            "replies": replies,
            "attachments": attachments,
            "notes": notes,
            "status_history": status_history
        }

def add_reply(message_id: int, admin_id: int, reply_text: str):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO replies (message_id, admin_id, reply_text) VALUES (?, ?, ?)",
            (message_id, admin_id, reply_text)
        )
        cursor.execute("UPDATE messages SET is_read = TRUE, status = ? WHERE message_id = ?", (STATUS_IN_PROGRESS, message_id))
        conn.commit()
        conn.close()

def get_all_messages(page: int = 1, per_page: int = 10) -> List[Dict]:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        offset = (page - 1) * per_page
        cursor.execute('''
            SELECT m.message_id, m.message_text, m.timestamp, m.is_read,
                   t.topic_name, u.user_id, u.username, u.first_name, u.last_name,
                   (SELECT COUNT(*) FROM replies WHERE message_id = m.message_id) as reply_count,
                   m.status, m.priority, m.is_anonymous
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
                "reply_count": row[9],
                "status": row[10],
                "priority": row[11],
                "is_anonymous": bool(row[12])
            })

        conn.close()
        return messages

def get_total_messages_count() -> int:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM messages")
        count = cursor.fetchone()[0]
        conn.close()
        return count

def get_all_users() -> List[Dict]:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, first_name, last_name FROM users WHERE is_banned = FALSE")
        users = [{"user_id": row[0], "username": row[1], "first_name": row[2], "last_name": row[3]} for row in cursor.fetchall()]
        conn.close()
        return users

def ban_user(user_id: int):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_banned = TRUE WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

def unban_user(user_id: int):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_banned = FALSE WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

def is_admin(user_id: int) -> bool:
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM admins WHERE admin_id = ?", (user_id,))
        result = bool(cursor.fetchone())
        conn.close()
        return result

def add_admin(admin_id: int, added_by: int, username: str = None):
    with suppress_stderr():
        conn = sqlite3.connect('feedback.db')
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (admin_id, username))
        cursor.execute("INSERT OR IGNORE INTO admins (admin_id, added_by, username) VALUES (?, ?, ?)",
                       (admin_id, added_by, username))
        conn.commit()
        conn.close()

def get_all_admins() -> List[Dict]:
    with suppress_stderr():
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
    with suppress_stderr():
        try:
            user = update.effective_user
            add_user(user.id, user.username, user.first_name, user.last_name,
                    update_from_telegram=True, context=context)

            photo_url = "https://via.placeholder.com/600x400?text=Welcome+to+Feedback+Bot"
            caption = (
                f"👋 Привет, {user.first_name}!\n\n"
                "Я бот для обратной связи. С моей помощью ты можешь:\n"
                "📨 Написать сообщение администрации\n"
                "🔍 Найти ответ в ЧаВо (FAQ)\n"
                "📖 Просмотреть историю своих обращений\n"
                "👤 Управлять своим профилем\n\n"
                "Выбери действие в меню ниже:"
            )

            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=photo_url,
                caption=caption,
                reply_markup=main_menu_keyboard(is_admin(user.id)),
                parse_mode='HTML'
            )
        except BadRequest:
            await send_menu(update, context, f"👋 Привет, {user.first_name}!\n\nЯ бот для обратной связи. Выбери действие в меню ниже:", "main")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def main_menu_keyboard(is_admin_user: bool):
    keyboard = [
        [InlineKeyboardButton("📨 Написать сообщение", callback_data="write_message")],
        [InlineKeyboardButton("🔍 Поиск в ЧаВо", callback_data="search_faq")],
        [InlineKeyboardButton("📖 История диалогов", callback_data="message_history")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="user_profile")]
    ]
    if is_admin_user:
        keyboard.append([InlineKeyboardButton("🔐 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

def admin_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📂 Все диалоги", callback_data="admin_all_dialogs")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton("👥 Управление админами", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("📝 Управление темами", callback_data="admin_manage_topics")],
        [InlineKeyboardButton("❓ Управление ЧаВо", callback_data="admin_manage_faq")],
        [InlineKeyboardButton("📊 Статистика оценок", callback_data="admin_view_ratings")],
        [InlineKeyboardButton("🔙 В главное меню", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, menu_type: str):
    user = update.effective_user
    if menu_type == "main":
        keyboard = main_menu_keyboard(is_admin(user.id))
    elif menu_type == "admin":
        keyboard = admin_menu_keyboard()
    else:
        return
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text=text, reply_markup=keyboard, parse_mode='HTML')
        except BadRequest:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=keyboard,
            parse_mode='HTML'
        )

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            await send_menu(update, context, "Главное меню:", "main")
        except Exception:
            pass
        return ConversationHandler.END

async def write_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            quick_actions = [t for t in get_topics() if t['is_quick_action']]
            topics = [t for t in get_topics() if not t['is_quick_action']]

            keyboard = []

            for action in quick_actions:
                keyboard.append([InlineKeyboardButton(
                    f"⚡ {action['topic_name']}",
                    callback_data=f"select_topic_{action['topic_id']}"
                )])

            if quick_actions and topics:
                keyboard.append([InlineKeyboardButton("──────────────", callback_data="none")])

            for topic in topics:
                keyboard.append([InlineKeyboardButton(
                    f"{topic['topic_name']} - {topic['description']}",
                    callback_data=f"select_topic_{topic['topic_id']}"
                )])

            keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_conversation")])

            if update.callback_query:
                await update.callback_query.edit_message_text(
                    "📝 Выберите тему для вашего сообщения:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    "📝 Выберите тему для вашего сообщения:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
            return SELECTING_TOPIC
        except Exception:
            await send_menu(update, context, "Ошибка при выборе темы.", "main")
            return ConversationHandler.END

async def select_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()

            if query.data == "cancel_topic_selection":
                await query.edit_message_text(
                    "Вы отменили создание сообщения.",
                    reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
                )
                return ConversationHandler.END

            topic_id = int(query.data.split("_")[-1])
            context.user_data['selected_topic'] = topic_id

            topics = get_topics()
            topic = next((t for t in topics if t['topic_id'] == topic_id), None)

            if not topic:
                await query.edit_message_text("Ошибка: тема не найдена.")
                return ConversationHandler.END

            context.user_data['topic_name'] = topic['topic_name']

            if topic['topic_name'] == "Срочный запрос":
                if not can_send_urgent(query.from_user.id):
                    await query.edit_message_text(
                        "Вы исчерпали лимит срочных запросов на сегодня.",
                        reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
                    )
                    return ConversationHandler.END
                increment_urgent_count(query.from_user.id)
                context.user_data['priority'] = PRIORITY_URGENT
            elif topic['is_quick_action']:
                context.user_data['priority'] = PRIORITY_HIGH
            else:
                context.user_data['priority'] = PRIORITY_NORMAL

            keyboard = [
                [InlineKeyboardButton("🔒 Анонимно", callback_data="anon_yes")],
                [InlineKeyboardButton("👤 От моего имени", callback_data="anon_no")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_anon_selection")]
            ]

            await query.edit_message_text(
                f"Вы выбрали тему: <b>{topic['topic_name']}</b>\n\n"
                "Хотите отправить сообщение анонимно?",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return CONFIRM_ANONYMITY
        except Exception:
            await query.edit_message_text("Ошибка при выборе темы.", reply_markup=main_menu_keyboard(is_admin(query.from_user.id)))
            return ConversationHandler.END

async def confirm_anonymity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()

            if query.data == "cancel_anon_selection":
                await query.edit_message_text(
                    "Вы отменили создание сообщения.",
                    reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
                )
                return ConversationHandler.END

            context.user_data['is_anonymous'] = query.data == "anon_yes"

            await query.edit_message_text(
                f"Вы выбрали тему: <b>{context.user_data['topic_name']}</b>\n"
                f"Режим: {'🔒 Анонимно' if context.user_data['is_anonymous'] else '👤 От моего имени'}\n\n"
                "Напишите ваше сообщение. Можно прикрепить фото, документ или голосовое:",
                parse_mode='HTML'
            )
            return WRITING_MESSAGE
        except Exception:
            await query.edit_message_text("Ошибка при выборе анонимности.", reply_markup=main_menu_keyboard(is_admin(query.from_user.id)))
            return ConversationHandler.END

async def receive_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            user_id = update.effective_user.id
            if 'dialog_message_id' in context.user_data:
                message_id = context.user_data['dialog_message_id']
                message_text = update.message.text if update.message.text else "Вложение"
                add_reply(message_id, user_id, message_text)
                await notify_admins_new_message(context, message_id, user_id, message_text, False, PRIORITY_NORMAL)
                await update.message.reply_text(
                    "✅ Сообщение добавлено в диалог.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📨 Продолжить диалог", callback_data=f"continue_dialog_{message_id}")],
                        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
                    ])
                )
                return WRITING_MESSAGE
            topic_id = context.user_data.get('selected_topic')
            is_anonymous = context.user_data.get('is_anonymous', False)
            priority = context.user_data.get('priority', PRIORITY_NORMAL)
            message_text = update.message.text or "Вложение"
            message_id = add_message(user_id, topic_id, message_text, is_anonymous, priority)
            if update.message.photo:
                file_id = update.message.photo[-1].file_id
                file_type = "photo"
                save_attachment(message_id, file_id, file_type)
            elif update.message.document:
                file_id = update.message.document.file_id
                file_type = "document"
                save_attachment(message_id, file_id, file_type)
            elif update.message.voice:
                file_id = update.message.voice.file_id
                file_type = "voice"
                save_attachment(message_id, file_id, file_type)
            await notify_admins_new_message(context, message_id, user_id, message_text, is_anonymous, priority)
            await update.message.reply_text(
                "✅ Сообщение отправлено.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📨 Продолжить диалог", callback_data=f"continue_dialog_{message_id}")],
                    [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
                ])
            )
            return WRITING_MESSAGE
        except Exception:
            await update.message.reply_text(
                "Ошибка при обработке сообщения.",
                reply_markup=main_menu_keyboard(is_admin(user_id))
            )
            return ConversationHandler.END

async def continue_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            message_id = int(query.data.split("_")[-1])
            message = get_message_details(message_id)
            if not message or message['user_id'] != query.from_user.id or message['status'] == STATUS_CLOSED:
                await query.edit_message_text(
                    "Диалог недоступен или закрыт.",
                    reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
                )
                return ConversationHandler.END
            context.user_data['dialog_message_id'] = message_id
            await query.edit_message_text(
                "Введите следующее сообщение в диалоге:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Завершить диалог", callback_data=f"end_dialog_{message_id}")],
                    [InlineKeyboardButton("🔙 Отмена", callback_data="cancel_conversation")]
                ])
            )
            return WRITING_MESSAGE
        except Exception:
            await query.edit_message_text(
                "Ошибка при продолжении диалога.",
                reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
            )
            return ConversationHandler.END

async def end_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            message_id = int(query.data.split("_")[-1])
            update_message_status(message_id, STATUS_CLOSED, query.from_user.id)
            await query.edit_message_text(
                "✅ Диалог завершен.",
                reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
            )
            return ConversationHandler.END
        except Exception:
            await query.edit_message_text(
                "Ошибка при завершении диалога.",
                reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
            )
            return ConversationHandler.END

async def admin_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            message_id = int(query.data.split("_")[1])
            message_details = get_message_details(message_id)
            if not message_details:
                await query.edit_message_text("Сообщение не найдено.")
                return ConversationHandler.END
            context.user_data['replying_to'] = message_id
            context.user_data['replying_user'] = message_details['user_id']
            response = (
                f"✍ Ответ на сообщение #{message_id}\n\n"
                f"📌 Тема: {message_details['topic_name']}\n"
                f"📅 Дата: {message_details['timestamp']}\n\n"
                f"💬 Сообщение:\n{message_details['message_text']}\n\n"
            )
            if not message_details['is_anonymous']:
                response += (
                    f"👤 От: {message_details['first_name']} {message_details['last_name']} "
                    f"(@{message_details['username'] or 'нет'})\n"
                )
            await query.edit_message_text(
                response + "\nВведите ваш ответ:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Отмена", callback_data="cancel_reply")]
                ])
            )
            return ADMIN_RESPONSE
        except Exception:
            await query.edit_message_text("Ошибка при подготовке ответа.")
            return ConversationHandler.END

async def admin_receive_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            reply_text = update.message.text
            message_id = context.user_data['replying_to']
            user_id = context.user_data['replying_user']
            admin_id = update.effective_user.id
            add_reply(message_id, admin_id, reply_text)
            admin = get_user(admin_id)
            admin_name = f"{admin['first_name']} {admin['last_name']}" if admin else "Администратор"
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📨 Ответ от {admin_name}:\n\n{reply_text}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📨 Продолжить диалог", callback_data=f"continue_dialog_{message_id}")],
                    [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
                ])
            )
            await update.message.reply_text(
                "✅ Ответ отправлен.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📨 Продолжить диалог", callback_data=f"reply_{message_id}")],
                    [InlineKeyboardButton("🔙 В меню", callback_data="back_to_admin_menu")]
                ])
            )
            return ADMIN_RESPONSE
        except Exception:
            await update.message.reply_text("Ошибка при отправке ответа.")
            return ConversationHandler.END

async def back_to_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            await send_menu(update, context, "🔑 Админ-панель:", "admin")
        except Exception:
            pass
        return ConversationHandler.END

async def admin_cancel_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            await send_menu(update, context, "Ответ отменен.", "admin")
            return ConversationHandler.END
        except Exception:
            await send_menu(update, context, "Ошибка при отмене ответа.", "admin")
            return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            await send_menu(update, context, "Действие отменено.", "main")
        except Exception:
            pass
        return ConversationHandler.END

async def notify_admins_new_message(context: ContextTypes.DEFAULT_TYPE, message_id: int, user_id: int,
                                   message_text: str, is_anonymous: bool, priority: str):
    with suppress_stderr():
        try:
            admins = get_all_admins()
            user = get_user(user_id)
            topic_id = context.user_data.get('selected_topic')
            topics = get_topics()
            topic = next((t for t in topics if t['topic_id'] == topic_id), None)
            topic_name = topic['topic_name'] if topic else "Без темы"

            priority_emoji = {
                PRIORITY_LOW: "🔹",
                PRIORITY_NORMAL: "🔸",
                PRIORITY_HIGH: "🔺",
                PRIORITY_URGENT: "🚨"
            }.get(priority, "🔹")

            message = (
                f"{priority_emoji} Новое сообщение #{message_id}\n"
                f"Тема: {topic_name}\n"
                f"Сообщение: {message_text[:100]}...\n"
            )

            if not is_anonymous and user:
                message += f"От: {user['first_name']} {user['last_name']} (@{user['username'] or 'нет'})"

            for admin in admins:
                await context.bot.send_message(
                    chat_id=admin['admin_id'],
                    text=message,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✍ Ответить", callback_data=f"reply_{message_id}")]
                    ])
                )
        except Exception:
            pass

async def message_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            user_id = update.effective_user.id
            messages = get_user_messages(user_id)
            if not messages:
                await send_menu(update, context, "📖 У вас пока нет сообщений.", "main")
                return
            response = "📖 Ваши диалоги:\n\n"
            for msg in messages:
                status_emoji = {
                    STATUS_NEW: "🆕",
                    STATUS_IN_PROGRESS: "🔄",
                    STATUS_RESOLVED: "✅",
                    STATUS_CLOSED: "🔒"
                }.get(msg['status'], "❓")
                priority_emoji = {
                    PRIORITY_LOW: "🔹",
                    PRIORITY_NORMAL: "🔸",
                    PRIORITY_HIGH: "🔺",
                    PRIORITY_URGENT: "🚨"
                }.get(msg['priority'], "🔹")
                response += (
                    f"{status_emoji}{priority_emoji} #{msg['message_id']} - {msg['topic_name']}\n"
                    f"📅 {msg['timestamp']}\n"
                    f"💬 {msg['message_text'][:50]}...\n"
                    f"↩ Ответов: {msg['reply_count']}\n\n"
                )
            keyboard = []
            for msg in messages:
                keyboard.append([InlineKeyboardButton(
                    f"#{msg['message_id']} - {msg['topic_name']}",
                    callback_data=f"view_dialog_{msg['message_id']}"
                )])
            keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")])
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    response,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    response,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
        except Exception:
            await send_menu(update, context, "Ошибка при загрузке истории.", "main")

async def view_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            message_id = int(query.data.split("_")[-1])
            message = get_message_details(message_id)
            if not message or message['user_id'] != query.from_user.id:
                await query.edit_message_text(
                    "Диалог не найден или недоступен.",
                    reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
                )
                return
            response = (
                f"💬 Диалог #{message['message_id']} - {message['topic_name']}\n"
                f"📅 {message['timestamp']}\n"
                f"📌 Приоритет: {message['priority']}\n"
                f"📌 Статус: {message['status']}\n\n"
                f"✉ Ваше сообщение:\n{message['message_text']}\n\n"
            )
            for reply in message['replies']:
                response += (
                    f"↩ Ответ от @{reply['username'] or 'Администратор'} ({reply['timestamp']}):\n"
                    f"{reply['text']}\n\n"
                )
            keyboard = [
                [InlineKeyboardButton("📨 Продолжить диалог", callback_data=f"continue_dialog_{message_id}")],
                [InlineKeyboardButton("❌ Завершить диалог", callback_data=f"end_dialog_{message_id}")],
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
            ]
            await query.edit_message_text(response, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            await query.edit_message_text(
                "Ошибка при просмотре диалога.",
                reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
            )

async def user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            user = update.effective_user
            user_data = get_user(user.id)
            if not user_data:
                await send_menu(update, context, "Профиль не найден.", "main")
                return
            ratings = get_user_ratings(user.id)
            notes = get_notes(user.id)
            response = (
                f"👤 Ваш профиль\n\n"
                f"🆔 ID: {user_data['user_id']}\n"
                f"👤 Имя: {user_data['first_name']} {user_data['last_name'] or ''}\n"
                f"📛 Username: @{user_data['username'] or 'нет'}\n"
                f"📅 Регистрация: {user_data['registration_date']}\n"
                f"🚫 Бан: {'Да' if user_data['is_banned'] else 'Нет'}\n"
                f"🌐 Язык: {user_data['language']}\n"
                f"🔥 Срочных сообщений сегодня: {user_data['urgent_messages_today']}/{MAX_URGENT_PER_DAY}\n\n"
            )
            if ratings:
                response += "⭐ Ваши оценки:\n"
                for rating in ratings[:3]:
                    response += (
                        f"{rating['rating']}⭐ ({rating['timestamp']})\n"
                        f"👤 Админ: {rating['admin_name']}\n"
                        f"📝 {rating['comments'] or 'Без комментария'}\n\n"
                    )
            if notes:
                response += "📝 Заметки о вас:\n"
                for note in notes[:3]:
                    response += (
                        f"📅 {note['timestamp']}\n"
                        f"👤 Админ: @{note['admin_username'] or 'неизвестно'}\n"
                        f"📜 {note['note_text']}\n\n"
                    )
            keyboard = [
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
            ]
            if user_data['is_banned']:
                keyboard.insert(0, [InlineKeyboardButton("🔓 Разбанить себя", callback_data=f"unban_me_{user.id}")])
            else:
                keyboard.insert(0, [InlineKeyboardButton("🚫 Забанить себя", callback_data=f"ban_me_{user.id}")])
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    response,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    response,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
        except Exception:
            await send_menu(update, context, "Ошибка при загрузке профиля.", "main")

async def ban_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            user_id = int(query.data.split("_")[-1])
            ban_user(user_id)
            await query.edit_message_text(
                "🚫 Вы забанили себя.",
                reply_markup=main_menu_keyboard(is_admin(user_id))
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при бане.",
                reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
            )

async def unban_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            user_id = int(query.data.split("_")[-1])
            unban_user(user_id)
            await query.edit_message_text(
                "🔓 Вы разбанили себя.",
                reply_markup=main_menu_keyboard(is_admin(user_id))
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при разбане.",
                reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
            )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            if not is_admin(update.effective_user.id):
                await send_menu(update, context, "Нет доступа.", "main")
                return
            await send_menu(update, context, "🔑 Админ-панель:", "admin")
        except Exception:
            await send_menu(update, context, "Ошибка при открытии админ-панели.", "main")

async def admin_all_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            if not is_admin(update.effective_user.id):
                await send_menu(update, context, "Нет доступа.", "main")
                return
            messages = get_all_messages(page=1)
            total_messages = get_total_messages_count()
            total_pages = (total_messages + 9) // 10
            keyboard = []
            for msg in messages:
                status_emoji = {
                    STATUS_NEW: "🆕",
                    STATUS_IN_PROGRESS: "🔄",
                    STATUS_RESOLVED: "✅",
                    STATUS_CLOSED: "🔒"
                }.get(msg['status'], "❓")
                priority_emoji = {
                    PRIORITY_LOW: "🔹",
                    PRIORITY_NORMAL: "🔸",
                    PRIORITY_HIGH: "🔺",
                    PRIORITY_URGENT: "🚨"
                }.get(msg['priority'], "🔹")
                user_info = "Аноним" if msg['is_anonymous'] else f"{msg['first_name']} {msg['last_name']} (@{msg['username'] or 'нет'})"
                keyboard.append([InlineKeyboardButton(
                    f"{status_emoji}{priority_emoji} #{msg['message_id']} - {user_info} - {msg['topic_name']}",
                    callback_data=f"admin_view_dialog_{msg['message_id']}"
                )])
            if total_pages > 1:
                keyboard.append([InlineKeyboardButton("Вперед ➡️", callback_data="page_2")])
            keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="back_to_admin_menu")])
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    f"📂 Все диалоги (Страница 1/{total_pages}):",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    f"📂 Все диалоги (Страница 1/{total_pages}):",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
        except Exception:
            await send_menu(update, context, "Ошибка при загрузке диалогов.", "admin")

async def admin_view_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            message_id = int(query.data.split("_")[-1])
            message = get_message_details(message_id)
            if not message:
                await query.edit_message_text("Диалог не найден.")
                return
            response = (
                f"💬 Диалог #{message['message_id']}\n"
                f"📌 Тема: {message['topic_name']}\n"
                f"📅 Дата: {message['timestamp']}\n"
                f"📌 Приоритет: {message['priority']}\n"
                f"📌 Статус: {message['status']}\n\n"
            )
            if not message['is_anonymous']:
                response += (
                    f"👤 Пользователь: {message['first_name']} {message['last_name']} "
                    f"(@{message['username'] or 'нет'})\n\n"
                )
            response += f"✉ Сообщение:\n{message['message_text']}\n\n"
            for reply in message['replies']:
                response += (
                    f"↩ Ответ от @{reply['username'] or 'Админ'} ({reply['timestamp']}):\n"
                    f"{reply['text']}\n\n"
                )
            if message['attachments']:
                response += "📎 Вложения:\n"
                for att in message['attachments']:
                    response += f"- {att['file_type']} (ID: {att['file_id']})\n"
            if message['notes']:
                response += "\n📝 Заметки о пользователе:\n"
                for note in message['notes']:
                    response += f"- {note['note_text']} (@{note['admin_username']} {note['timestamp']})\n"
            if message['status_history']:
                response += "\n📜 История статусов:\n"
                for status in message['status_history']:
                    response += f"- {status['status']} ({status['timestamp']})\n"
            keyboard = [
                [InlineKeyboardButton("✍ Ответить", callback_data=f"reply_{message_id}")],
                [InlineKeyboardButton("🔄 Назначить", callback_data=f"reassign_{message_id}")],
                [InlineKeyboardButton("📝 Добавить заметку", callback_data=f"add_note_{message['user_id']}")],
                [InlineKeyboardButton("🔒 Закрыть диалог", callback_data=f"close_dialog_{message_id}")],
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_admin_menu")]
            ]
            await query.edit_message_text(response, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            await query.edit_message_text(
                "Ошибка при просмотре диалога.",
                reply_markup=admin_menu_keyboard()
            )

async def admin_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            if query.data == "back_to_admin_menu":
                await query.edit_message_text(
                    "Админ-меню:",
                    reply_markup=admin_menu_keyboard()
                )
                return
            page = int(query.data.split("_")[1])
            messages = get_all_messages(page)
            total_messages = get_total_messages_count()
            total_pages = (total_messages + 9) // 10
            keyboard = []
            for msg in messages:
                status_emoji = {
                    STATUS_NEW: "🆕",
                    STATUS_IN_PROGRESS: "🔄",
                    STATUS_RESOLVED: "✅",
                    STATUS_CLOSED: "🔒"
                }.get(msg['status'], "❓")
                priority_emoji = {
                    PRIORITY_LOW: "🔹",
                    PRIORITY_NORMAL: "🔸",
                    PRIORITY_HIGH: "🔺",
                    PRIORITY_URGENT: "🚨"
                }.get(msg['priority'], "🔹")
                user_info = "Аноним" if msg['is_anonymous'] else f"{msg['first_name']} {msg['last_name']} (@{msg['username'] or 'нет'})"
                keyboard.append([InlineKeyboardButton(
                    f"{status_emoji}{priority_emoji} #{msg['message_id']} - {user_info} - {msg['topic_name']}",
                    callback_data=f"admin_view_dialog_{msg['message_id']}"
                )])
            row = []
            if page > 1:
                row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{page - 1}"))
            if page < total_pages:
                row.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"page_{page + 1}"))
            if row:
                keyboard.append(row)
            keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data="back_to_admin_menu")])
            await query.edit_message_text(
                f"📂 Все диалоги (Страница {page}/{total_pages}):",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при загрузке диалогов.",
                reply_markup=admin_menu_keyboard()
            )

async def admin_close_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            message_id = int(query.data.split("_")[-1])
            update_message_status(message_id, STATUS_CLOSED, query.from_user.id)
            await query.edit_message_text(
                "✅ Диалог закрыт.",
                reply_markup=admin_menu_keyboard()
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при закрытии диалога.",
                reply_markup=admin_menu_keyboard()
            )

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            if not is_admin(update.effective_user.id):
                await send_menu(update, context, "Нет доступа.", "main")
                return
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    "📢 Введите сообщение для рассылки всем пользователям:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_broadcast")]
                    ]),
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    "📢 Введите сообщение для рассылки всем пользователям:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_broadcast")]
                    ]),
                    parse_mode='HTML'
                )
            return BROADCAST_MESSAGE
        except Exception:
            await send_menu(update, context, "Ошибка при начале рассылки.", "admin")
            return ConversationHandler.END

async def admin_receive_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            broadcast_message = update.message.text
            users = get_all_users()
            for user in users:
                try:
                    await context.bot.send_message(
                        chat_id=user['user_id'],
                        text=broadcast_message
                    )
                except Exception:
                    pass
            await update.message.reply_text(
                "✅ Рассылка завершена.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END
        except Exception:
            await update.message.reply_text(
                "Ошибка при отправке рассылки.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(
                "Рассылка отменена.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END
        except Exception:
            await query.edit_message_text(
                "Ошибка при отмене рассылки.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_manage_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            if not is_admin(update.effective_user.id):
                await send_menu(update, context, "Нет доступа.", "main")
                return
            keyboard = [
                [InlineKeyboardButton("➕ Добавить админа", callback_data="add_admin")],
                [InlineKeyboardButton("➖ Удалить админа", callback_data="remove_admin")],
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_admin_menu")]
            ]
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    "👥 Управление администраторами:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    "👥 Управление администраторами:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
        except Exception:
            await send_menu(update, context, "Ошибка при открытии управления админами.", "admin")

async def admin_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(
                "Введите ID нового администратора:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_admin")]
                ])
            )
            return ADDING_ADMIN
        except Exception:
            await query.edit_message_text(
                "Ошибка при добавлении админа.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_receive_new_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            admin_id = int(update.message.text.strip())
            user = get_user(admin_id)
            add_admin(admin_id, update.effective_user.id, user['username'] if user else None)
            await update.message.reply_text(
                f"✅ Админ с ID {admin_id} добавлен.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END
        except Exception:
            await update.message.reply_text(
                "Ошибка при добавлении админа.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            admins = get_all_admins()
            keyboard = []
            for admin in admins:
                if admin['admin_id'] != query.from_user.id:
                    keyboard.append([InlineKeyboardButton(
                        f"@{admin['username'] or 'нет'} ({admin['first_name']} {admin['last_name']})",
                        callback_data=f"remove_admin_{admin['admin_id']}"
                    )])
            keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_remove_admin")])
            await query.edit_message_text(
                "Выберите администратора для удаления:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при удалении админа.",
                reply_markup=admin_menu_keyboard()
            )

async def admin_confirm_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            admin_id = int(query.data.split("_")[-1])
            conn = sqlite3.connect('feedback.db')
            cursor = conn.cursor()
            cursor.execute("DELETE FROM admins WHERE admin_id = ?", (admin_id,))
            conn.commit()
            conn.close()
            await query.edit_message_text(
                "✅ Админ удален.",
                reply_markup=admin_menu_keyboard()
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при удалении админа.",
                reply_markup=admin_menu_keyboard()
            )

async def admin_cancel_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(
                "Удаление админа отменено.",
                reply_markup=admin_menu_keyboard()
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при отмене удаления.",
                reply_markup=admin_menu_keyboard()
            )

async def admin_manage_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            if not is_admin(update.effective_user.id):
                await send_menu(update, context, "Нет доступа.", "main")
                return
            keyboard = [
                [InlineKeyboardButton("➕ Добавить тему", callback_data="add_topic")],
                [InlineKeyboardButton("➖ Удалить тему", callback_data="remove_topic")],
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_admin_menu")]
            ]
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    "📝 Управление темами:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    "📝 Управление темами:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
        except Exception:
            await send_menu(update, context, "Ошибка при открытии управления темами.", "admin")

async def admin_add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(
                "Введите название новой темы:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_topic")]
                ])
            )
            return CREATING_TOPIC
        except Exception:
            await query.edit_message_text(
                "Ошибка при добавлении темы.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_receive_topic_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            context.user_data['new_topic_name'] = update.message.text
            await update.message.reply_text(
                "Введите описание темы:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_topic")]
                ])
            )
            return CREATING_TOPIC
        except Exception:
            await update.message.reply_text(
                "Ошибка при добавлении темы.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_receive_topic_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            topic_name = context.user_data['new_topic_name']
            description = update.message.text
            add_topic(topic_name, description)
            await update.message.reply_text(
                f"✅ Тема '{topic_name}' добавлена.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END
        except Exception:
            await update.message.reply_text(
                "Ошибка при добавлении темы.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_cancel_add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(
                "Добавление темы отменено.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END
        except Exception:
            await query.edit_message_text(
                "Ошибка при отмене.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_remove_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            topics = get_topics()
            keyboard = []
            for topic in topics:
                keyboard.append([InlineKeyboardButton(
                    f"{topic['topic_name']} - {topic['description']}",
                    callback_data=f"remove_topic_{topic['topic_id']}"
                )])
            keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_remove_topic")])
            await query.edit_message_text(
                "Выберите тему для удаления:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при удалении темы.",
                reply_markup=admin_menu_keyboard()
            )

async def admin_confirm_remove_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            topic_id = int(query.data.split("_")[-1])
            conn = sqlite3.connect('feedback.db')
            cursor = conn.cursor()
            cursor.execute("DELETE FROM topics WHERE topic_id = ?", (topic_id,))
            conn.commit()
            conn.close()
            await query.edit_message_text(
                "✅ Тема удалена.",
                reply_markup=admin_menu_keyboard()
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при удалении темы.",
                reply_markup=admin_menu_keyboard()
            )

async def admin_cancel_remove_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(
                "Удаление темы отменено.",
                reply_markup=admin_menu_keyboard()
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при отмене.",
                reply_markup=admin_menu_keyboard()
            )

async def admin_manage_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            if not is_admin(update.effective_user.id):
                await send_menu(update, context, "Нет доступа.", "main")
                return
            keyboard = [
                [InlineKeyboardButton("➕ Добавить вопрос", callback_data="add_faq")],
                [InlineKeyboardButton("➖ Удалить вопрос", callback_data="remove_faq")],
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_admin_menu")]
            ]
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    "❓ Управление ЧаВо:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    "❓ Управление ЧаВо:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
        except Exception:
            await send_menu(update, context, "Ошибка при открытии управления FAQ.", "admin")

async def admin_add_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(
                "Введите вопрос для FAQ:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_faq")]
                ])
            )
            return ADDING_FAQ
        except Exception:
            await query.edit_message_text(
                "Ошибка при добавлении FAQ.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_receive_faq_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            context.user_data['faq_question'] = update.message.text
            await update.message.reply_text(
                "Введите ответ для FAQ:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_faq")]
                ])
            )
            return ADDING_FAQ
        except Exception:
            await update.message.reply_text(
                "Ошибка при добавлении FAQ.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_receive_faq_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            context.user_data['faq_answer'] = update.message.text
            topics = get_topics()
            keyboard = []
            for topic in topics:
                keyboard.append([InlineKeyboardButton(
                    topic['topic_name'],
                    callback_data=f"faq_topic_{topic['topic_id']}"
                )])
            keyboard.append([InlineKeyboardButton("Без темы", callback_data="faq_no_topic")])
            keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_faq")])
            await update.message.reply_text(
                "Выберите тему для FAQ:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return ADDING_FAQ
        except Exception:
            await update.message.reply_text(
                "Ошибка при добавлении FAQ.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_save_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            if query.data == "cancel_add_faq":
                await query.edit_message_text(
                    "Добавление FAQ отменено.",
                    reply_markup=admin_menu_keyboard()
                )
                return ConversationHandler.END
            question = context.user_data.get('faq_question')
            answer = context.user_data.get('faq_answer')
            topic_id = None if query.data == "faq_no_topic" else int(query.data.split("_")[-1])
            add_faq(question, answer, topic_id)
            await query.edit_message_text(
                "✅ Вопрос добавлен в FAQ.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END
        except Exception:
            await query.edit_message_text(
                "Ошибка при сохранении FAQ.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_remove_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            faq_items = search_faq("")
            keyboard = []
            for item in faq_items:
                keyboard.append([InlineKeyboardButton(
                    f"{item['question']} (ID: {item['faq_id']})",
                    callback_data=f"remove_faq_{item['faq_id']}"
                )])
            if not keyboard:
                await query.edit_message_text(
                    "Нет вопросов для удаления.",
                    reply_markup=admin_menu_keyboard()
                )
                return
            keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_remove_faq")])
            await query.edit_message_text(
                "Выберите вопрос для удаления:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при удалении FAQ.",
                reply_markup=admin_menu_keyboard()
            )

async def admin_confirm_remove_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            faq_id = int(query.data.split("_")[-1])
            conn = sqlite3.connect('feedback.db')
            cursor = conn.cursor()
            cursor.execute("DELETE FROM faq WHERE faq_id = ?", (faq_id,))
            conn.commit()
            conn.close()
            await query.edit_message_text(
                "✅ Вопрос удален из FAQ.",
                reply_markup=admin_menu_keyboard()
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при удалении FAQ.",
                reply_markup=admin_menu_keyboard()
            )

async def admin_cancel_remove_faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(
                "Удаление FAQ отменено.",
                reply_markup=admin_menu_keyboard()
            )
        except Exception:
            await query.edit_message_text(
                "Ошибка при отмене.",
                reply_markup=admin_menu_keyboard()
            )

async def search_faq_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    "🔍 Введите запрос для поиска в FAQ:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_faq_search")]
                    ]),
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    "🔍 Введите запрос для поиска в FAQ:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_faq_search")]
                    ]),
                    parse_mode='HTML'
                )
            return SEARCHING_FAQ
        except Exception:
            await send_menu(update, context, "Ошибка при поиске FAQ.", "main")
            return ConversationHandler.END

async def receive_faq_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.message.text
            faq_items = search_faq(query)
            if not faq_items:
                await update.message.reply_text(
                    "😔 По вашему запросу ничего не найдено.",
                    reply_markup=main_menu_keyboard(is_admin(update.effective_user.id))
                )
                return ConversationHandler.END
            response = "❓ Результаты поиска:\n\n"
            for i, item in enumerate(faq_items[:5], 1):
                response += (
                    f"{i}. <b>{item['question']}</b>\n"
                    f"🔹 Ответ: {item['answer']}\n"
                    f"📌 Тема: {item['topic_name']}\n\n"
                )
            await update.message.reply_text(
                response,
                parse_mode='HTML',
                reply_markup=main_menu_keyboard(is_admin(update.effective_user.id))
            )
            return ConversationHandler.END
        except Exception:
            await update.message.reply_text(
                "Ошибка при поиске FAQ.",
                reply_markup=main_menu_keyboard(is_admin(update.effective_user.id))
            )
            return ConversationHandler.END

async def cancel_faq_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(
                "Поиск отменен.",
                reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
            )
            return ConversationHandler.END
        except Exception:
            await query.edit_message_text(
                "Ошибка при отмене поиска.",
                reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
            )
            return ConversationHandler.END

async def admin_add_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            user_id = int(query.data.split("_")[-1])
            context.user_data['note_user_id'] = user_id
            await query.edit_message_text(
                "Введите текст заметки о пользователе:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Отмена", callback_data="cancel_add_note")]
                ])
            )
            return ADDING_NOTE
        except Exception:
            await query.edit_message_text(
                "Ошибка при добавлении заметки.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def receive_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            note_text = update.message.text
            user_id = context.user_data['note_user_id']
            admin_id = update.effective_user.id
            add_note(user_id, admin_id, note_text)
            await update.message.reply_text(
                "✅ Заметка добавлена.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END
        except Exception:
            await update.message.reply_text(
                "Ошибка при добавлении заметки.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def cancel_add_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(
                "Добавление заметки отменено.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END
        except Exception:
            await query.edit_message_text(
                "Ошибка при отмене.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_reassign_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            message_id = int(query.data.split("_")[-1])
            context.user_data['reassign_message_id'] = message_id
            admins = get_all_admins()
            keyboard = []
            for admin in admins:
                keyboard.append([InlineKeyboardButton(
                    f"{admin['first_name']} {admin['last_name']} (@{admin['username'] or 'нет'})",
                    callback_data=f"reassign_to_{admin['admin_id']}"
                )])
            keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_reassign")])
            await query.edit_message_text(
                "Выберите администратора для назначения:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return REASSIGNING_DIALOG
        except Exception:
            await query.edit_message_text(
                "Ошибка при переназначении диалога.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def confirm_reassign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            admin_id = int(query.data.split("_")[-1])
            message_id = context.user_data['reassign_message_id']
            reassign_message(message_id, admin_id)
            admin = get_user(admin_id)
            admin_name = f"{admin['first_name']} {admin['last_name']}" if admin else f"ID: {admin_id}"
            await query.edit_message_text(
                f"✅ Диалог #{message_id} назначен администратору {admin_name}.",
                reply_markup=admin_menu_keyboard()
            )
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"Вам назначен диалог #{message_id}.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✍ Ответить", callback_data=f"reply_{message_id}")]
                ])
            )
            return ConversationHandler.END
        except Exception:
            await query.edit_message_text(
                "Ошибка при переназначении.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def cancel_reassign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(
                "Назначение отменено.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END
        except Exception:
            await query.edit_message_text(
                "Ошибка при отмене.",
                reply_markup=admin_menu_keyboard()
            )
            return ConversationHandler.END

async def admin_view_ratings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            if not is_admin(update.effective_user.id):
                await update.message.reply_text("Нет доступа.")
                return
            ratings = get_ratings()
            if not ratings:
                await update.message.reply_text(
                    "📊 Пока нет оценок.",
                    reply_markup=admin_menu_keyboard()
                )
                return
            response = "📊 Статистика оценок:\n\n"
            for rating in ratings[:10]:
                stars = "⭐" * rating['rating'] + "☆" * (5 - rating['rating'])
                response += (
                    f"{stars} {rating['rating']}/5\n"
                    f"👤 Пользователь: {rating['user_name']} (ID: {rating['user_id']})\n"
                    f"👨‍💼 Админ: @{rating['admin_username'] or 'неизвестно'} (ID: {rating['admin_id']})\n"
                    f"📅 Дата: {rating['timestamp']}\n"
                )
                if rating['comments']:
                    response += f"📝 Комментарий: {rating['comments']}\n"
                response += "\n"
            await update.message.reply_text(
                response,
                reply_markup=admin_menu_keyboard()
            )
        except Exception:
            await update.message.reply_text(
                "Ошибка при загрузке оценок.",
                reply_markup=admin_menu_keyboard()
            )

async def rate_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            message_id = int(query.data.split("_")[-1])
            context.user_data['rating_message_id'] = message_id
            keyboard = [
                [InlineKeyboardButton(f"{i} ⭐", callback_data=f"rate_{i}") for i in range(1, 6)],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_rating")]
            ]
            await query.edit_message_text(
                "⭐ Пожалуйста, оцените качество ответа:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return RATING_RESPONSE
        except Exception:
            await query.edit_message_text(
                "Ошибка при оценке.",
                reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
            )
            return ConversationHandler.END

async def receive_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            if query.data == "cancel_rating":
                await query.edit_message_text(
                    "Оценка отменена.",
                    reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
                )
                return ConversationHandler.END
            rating = int(query.data.split("_")[-1])
            message_id = context.user_data['rating_message_id']
            message = get_message_details(message_id)
            context.user_data['rating_value'] = rating
            context.user_data['rating_admin_id'] = message['assigned_admin_id'] or ADMIN_ID
            await query.edit_message_text(
                "📝 Хотите оставить комментарий к оценке? Напишите его или нажмите 'Пропустить':",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➖ Пропустить", callback_data="skip_comment")]
                ])
            )
            return RECEIVING_RATING_COMMENT
        except Exception:
            await query.edit_message_text(
                "Ошибка при обработке оценки.",
                reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
            )
            return ConversationHandler.END

async def receive_rating_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            comment = update.message.text
            rating = context.user_data['rating_value']
            user_id = update.effective_user.id
            admin_id = context.user_data['rating_admin_id']
            add_rating(user_id, admin_id, rating, comment)
            await update.message.reply_text(
                "✅ Спасибо за вашу оценку!",
                reply_markup=main_menu_keyboard(is_admin(user_id))
            )
            return ConversationHandler.END
        except Exception:
            await update.message.reply_text(
                "Ошибка при сохранении комментария.",
                reply_markup=main_menu_keyboard(is_admin(update.effective_user.id))
            )
            return ConversationHandler.END

async def skip_rating_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            await query.answer()
            rating = context.user_data['rating_value']
            user_id = query.from_user.id
            admin_id = context.user_data['rating_admin_id']
            add_rating(user_id, admin_id, rating)
            await query.edit_message_text(
                "✅ Спасибо за вашу оценку!",
                reply_markup=main_menu_keyboard(is_admin(user_id))
            )
            return ConversationHandler.END
        except Exception:
            await query.edit_message_text(
                "Ошибка при сохранении оценки.",
                reply_markup=main_menu_keyboard(is_admin(query.from_user.id))
            )
            return ConversationHandler.END

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with suppress_stderr():
        try:
            query = update.callback_query
            if query:
                await query.answer()
                if query.data == "back_to_menu":
                    return await back_to_menu(update, context)
                elif query.data == "back_to_admin_menu":
                    return await back_to_admin_menu(update, context)
                elif query.data == "cancel_conversation":
                    return await cancel_conversation(update, context)
                elif query.data.startswith("select_topic_"):
                    return await select_topic(update, context)
                elif query.data.startswith("anon_"):
                    return await confirm_anonymity(update, context)
                elif query.data.startswith("continue_dialog_"):
                    return await continue_dialog(update, context)
                elif query.data.startswith("end_dialog_"):
                    return await end_dialog(update, context)
                elif query.data.startswith("view_dialog_"):
                    return await view_dialog(update, context)
                elif query.data.startswith("ban_me_"):
                    return await ban_me(update, context)
                elif query.data.startswith("unban_me_"):
                    return await unban_me(update, context)
                elif query.data.startswith("admin_view_dialog_"):
                    return await admin_view_dialog(update, context)
                elif query.data.startswith("page_"):
                    return await admin_page_callback(update, context)
                elif query.data.startswith("reply_"):
                    return await admin_reply_callback(update, context)
                elif query.data.startswith("close_dialog_"):
                    return await admin_close_dialog(update, context)
                elif query.data == "admin_panel":
                    return await admin_panel(update, context)
                elif query.data == "write_message":
                    return await write_message(update, context)
                elif query.data == "message_history":
                    return await message_history(update, context)
                elif query.data == "user_profile":
                    return await user_profile(update, context)
                elif query.data == "search_faq":
                    return await search_faq_handler(update, context)
                elif query.data == "admin_all_dialogs":
                    return await admin_all_dialogs(update, context)
                elif query.data == "admin_broadcast":
                    return await admin_broadcast(update, context)
                elif query.data == "admin_manage_admins":
                    return await admin_manage_admins(update, context)
                elif query.data == "admin_manage_topics":
                    return await admin_manage_topics(update, context)
                elif query.data == "admin_manage_faq":
                    return await admin_manage_faq(update, context)
                elif query.data == "admin_view_ratings":
                    return await admin_view_ratings(update, context)
                elif query.data == "add_admin":
                    return await admin_add_admin(update, context)
                elif query.data == "remove_admin":
                    return await admin_remove_admin(update, context)
                elif query.data.startswith("remove_admin_"):
                    return await admin_confirm_remove_admin(update, context)
                elif query.data == "cancel_remove_admin":
                    return await admin_cancel_remove_admin(update, context)
                elif query.data == "add_topic":
                    return await admin_add_topic(update, context)
                elif query.data == "remove_topic":
                    return await admin_remove_topic(update, context)
                elif query.data.startswith("remove_topic_"):
                    return await admin_confirm_remove_topic(update, context)
                elif query.data == "cancel_remove_topic":
                    return await admin_cancel_remove_topic(update, context)
                elif query.data == "add_faq":
                    return await admin_add_faq(update, context)
                elif query.data.startswith("faq_topic_") or query.data == "faq_no_topic":
                    return await admin_save_faq(update, context)
                elif query.data == "cancel_add_faq":
                    return await admin_save_faq(update, context)
                elif query.data == "remove_faq":
                    return await admin_remove_faq(update, context)
                elif query.data.startswith("remove_faq_"):
                    return await admin_confirm_remove_faq(update, context)
                elif query.data == "cancel_faq_search":
                    return await cancel_faq_search(update, context)
                elif query.data.startswith("add_note_"):
                    return await admin_add_note(update, context)
                elif query.data == "cancel_add_note":
                    return await cancel_add_note(update, context)
                elif query.data.startswith("reassign_"):
                    return await admin_reassign_dialog(update, context)
                elif query.data.startswith("reassign_to_"):
                    return await confirm_reassign(update, context)
                elif query.data == "cancel_reassign":
                    return await cancel_reassign(update, context)
                elif query.data == "cancel_broadcast":
                    return await admin_cancel_broadcast(update, context)
                elif query.data.startswith("rate_") or query.data == "cancel_rating":
                    return await receive_rating(update, context)
                elif query.data == "skip_comment":
                    return await skip_rating_comment(update, context)
            else:
                await send_menu(update, context, "Ошибка: нет данных callback.", "main")
        except Exception as e:
            print(f"Exception in button_callback: {e}")
            if update.callback_query:
                try:
                    await send_menu(update, context, "错误 при обработке команды.", "main")
                except Exception as e2:
                    print(f"Failed to edit message: {e2}")
            else:
                await send_menu(update, context, "错误 при обработке команды.", "main")
        return ConversationHandler.END

def main():
    with open('config.json', 'r') as config_file:
        config = json.load(config_file)
        BOT_TOKEN = config.get("BOT_TOKEN")

    def shutdown_handler(signum, frame):
        print("🛑 Бот остановлен. До встречи!")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    with suppress_stderr():
        try:
            application = ApplicationBuilder().token(BOT_TOKEN).build()

            application.job_queue.run_repeating(check_user_updates, interval=3600, first=10)

            conv_handler = ConversationHandler(
                entry_points=[
                    CommandHandler("start", start),
                    CallbackQueryHandler(write_message, pattern="^write_message$"),
                    CallbackQueryHandler(message_history, pattern="^message_history$"),
                    CallbackQueryHandler(user_profile, pattern="^user_profile$"),
                    CallbackQueryHandler(search_faq_handler, pattern="^search_faq$"),
                    CallbackQueryHandler(admin_panel, pattern="^admin_panel$"),
                    CallbackQueryHandler(admin_all_dialogs, pattern="^admin_all_dialogs$"),
                    CallbackQueryHandler(admin_broadcast, pattern="^admin_broadcast$"),
                    CallbackQueryHandler(admin_manage_admins, pattern="^admin_manage_admins$"),
                    CallbackQueryHandler(admin_manage_topics, pattern="^admin_manage_topics$"),
                    CallbackQueryHandler(admin_manage_faq, pattern="^admin_manage_faq$"),
                    CallbackQueryHandler(admin_view_ratings, pattern="^admin_view_ratings$"),
                    CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$")
                ],
                states={
                    SELECTING_TOPIC: [CallbackQueryHandler(button_callback)],
                    WRITING_MESSAGE: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_message),
                        MessageHandler(filters.PHOTO, receive_message),
                        MessageHandler(filters.Document.ALL, receive_message),
                        MessageHandler(filters.VOICE, receive_message),
                        CallbackQueryHandler(button_callback)
                    ],
                    CONFIRM_ANONYMITY: [CallbackQueryHandler(button_callback)],
                    ADMIN_RESPONSE: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_reply),
                        CallbackQueryHandler(button_callback)
                    ],
                    BROADCAST_MESSAGE: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_broadcast),
                        CallbackQueryHandler(button_callback)
                    ],
                    ADDING_ADMIN: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_new_admin),
                        CallbackQueryHandler(button_callback)
                    ],
                    CREATING_TOPIC: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_topic_name),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_topic_description),
                        CallbackQueryHandler(button_callback)
                    ],
                    ADDING_FAQ: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_faq_question),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_faq_answer),
                        CallbackQueryHandler(button_callback)
                    ],
                    SEARCHING_FAQ: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_faq_search),
                        CallbackQueryHandler(button_callback)
                    ],
                    ADDING_NOTE: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_note),
                        CallbackQueryHandler(button_callback)
                    ],
                    REASSIGNING_DIALOG: [
                        CallbackQueryHandler(button_callback)
                    ],
                    RATING_RESPONSE: [
                        CallbackQueryHandler(button_callback)
                    ],
                    RECEIVING_RATING_COMMENT: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_rating_comment),
                        CallbackQueryHandler(button_callback)
                    ]
                },
                fallbacks=[
                    CommandHandler("cancel", cancel_conversation),
                    CallbackQueryHandler(cancel_conversation, pattern="^cancel_conversation$")
                ]
            )

            application.add_handler(conv_handler)
            application.add_handler(CallbackQueryHandler(button_callback))

            print("✅ Бот запущен и готов к работе! 🚀")

            application.run_polling()

        except Exception as e:
            print(f"Произошла ошибка при запуске: {e}")

if __name__ == '__main__':
    main()
