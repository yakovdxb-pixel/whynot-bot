import logging
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, ContextTypes,
                           CallbackQueryHandler)
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('BOT_TOKEN')
DB_PATH = 'agency.db'


# ─── База данных ──────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS projects (
            group_id INTEGER PRIMARY KEY,
            name     TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id         INTEGER NOT NULL,
            title            TEXT NOT NULL,
            assigned_username TEXT,
            deadline         TEXT,
            status           TEXT DEFAULT 'active',
            created_by       TEXT,
            created_at       TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS kpi (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT NOT NULL,
            group_id    INTEGER,
            month       TEXT NOT NULL,
            retention   INTEGER DEFAULT 0,
            csat        REAL    DEFAULT 0,
            deadlines_ok INTEGER DEFAULT 0,
            deadlines_total INTEGER DEFAULT 0,
            initiatives  INTEGER DEFAULT 0
        );
    ''')
    conn.commit()
    conn.close()


# ─── /start ───────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ['group', 'supergroup']:
        conn = get_db()
        existing = conn.execute(
            'SELECT * FROM projects WHERE group_id = ?', (chat.id,)
        ).fetchone()
        if not existing:
            conn.execute(
                'INSERT INTO projects (group_id, name) VALUES (?, ?)',
                (chat.id, chat.title)
            )
            conn.commit()
            await update.message.reply_text(
                f"✅ Группа *{chat.title}* зарегистрирована!\n\n"
                f"Команды:\n"
                f"/задача — создать задачу\n"
                f"/задачи — список задач\n"
                f"/готово — отметить выполненной\n"
                f"/статус — обзор проекта\n"
                f"/кпи — KPI команды",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                f"👋 Группа *{chat.title}* уже подключена!", parse_mode='Markdown'
            )
        conn.close()
    else:
        await update.message.reply_text(
            "👋 Привет! Я бот WHYNOT Agency.\n"
            "Добавь меня в группу клиента и напиши /start"
        )


# ─── /задача ──────────────────────────────────────────────────────────────────

async def new_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Команда работает только в группах клиентов")
        return

    if not context.args:
        await update.message.reply_text(
            "📝 Формат: `/задача Описание @username дедлайн`\n\n"
            "Пример:\n`/задача Сделать 3 поста для инсты @bekmotion 25.01`",
            parse_mode='Markdown'
        )
        return

    text = ' '.join(context.args)
    assigned = None
    deadline = None
    title_words = []

    for word in text.split():
        if word.startswith('@'):
            assigned = word[1:]
        elif len(word) >= 4 and '.' in word and word.replace('.', '').isdigit():
            deadline = word
        else:
            title_words.append(word)

    title = ' '.join(title_words)

    conn = get_db()
    conn.execute(
        'INSERT OR IGNORE INTO projects (group_id, name) VALUES (?, ?)',
        (chat.id, chat.title)
    )
    cursor = conn.execute(
        'INSERT INTO tasks (group_id, title, assigned_username, deadline, created_by) '
        'VALUES (?, ?, ?, ?, ?)',
        (chat.id, title, assigned, deadline, user.username)
    )
    task_id = cursor.lastrowid
    conn.commit()
    conn.close()

    msg = f"✅ *Задача #{task_id} создана*\n\n📌 {title}"
    if assigned:
        msg += f"\n👤 @{assigned}"
    if deadline:
        msg += f"\n📅 {deadline}"

    await update.message.reply_text(msg, parse_mode='Markdown')


# ─── /задачи ──────────────────────────────────────────────────────────────────

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Команда работает только в группах клиентов")
        return

    conn = get_db()
    tasks = conn.execute(
        'SELECT * FROM tasks WHERE group_id = ? AND status = "active" ORDER BY created_at',
        (chat.id,)
    ).fetchall()
    conn.close()

    if not tasks:
        await update.message.reply_text("🎉 Активных задач нет!")
        return

    msg = f"📋 *Задачи — {chat.title}*\n\n"
    for t in tasks:
        msg += f"*#{t['id']}* {t['title']}"
        if t['assigned_username']:
            msg += f" → @{t['assigned_username']}"
        if t['deadline']:
            msg += f" 📅 {t['deadline']}"
        msg += "\n"

    await update.message.reply_text(msg, parse_mode='Markdown')


# ─── /готово ──────────────────────────────────────────────────────────────────

async def done_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Укажи номер задачи: `/готово 5`", parse_mode='Markdown'
        )
        return

    task_id = context.args[0]
    conn = get_db()
    task = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()

    if task:
        conn.execute('UPDATE tasks SET status = "done" WHERE id = ?', (task_id,))
        conn.commit()
        await update.message.reply_text(
            f"✅ Задача #{task_id} выполнена!\n_{task['title']}_",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(f"❌ Задача #{task_id} не найдена")

    conn.close()


# ─── /статус ─────────────────────────────────────────────────────────────────

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    conn = get_db()
    active = conn.execute(
        'SELECT COUNT(*) as cnt FROM tasks WHERE group_id = ? AND status = "active"',
        (chat.id,)
    ).fetchone()['cnt']
    done = conn.execute(
        'SELECT COUNT(*) as cnt FROM tasks WHERE group_id = ? AND status = "done"',
        (chat.id,)
    ).fetchone()['cnt']
    deadline_tasks = conn.execute(
        'SELECT * FROM tasks WHERE group_id = ? AND status = "active" AND deadline IS NOT NULL',
        (chat.id,)
    ).fetchall()
    conn.close()

    msg = f"📊 *{chat.title}*\n\n"
    msg += f"🔄 Активных: {active}\n"
    msg += f"✅ Выполнено: {done}\n"

    if deadline_tasks:
        msg += f"\n⏰ *С дедлайном:*\n"
        for t in deadline_tasks:
            msg += f"  #{t['id']} {t['title']}"
            if t['assigned_username']:
                msg += f" @{t['assigned_username']}"
            msg += f" — {t['deadline']}\n"

    await update.message.reply_text(msg, parse_mode='Markdown')


# ─── /кпи ────────────────────────────────────────────────────────────────────

async def kpi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    conn = get_db()
    from datetime import datetime
    month = datetime.now().strftime('%Y-%m')

    # Считаем задачи по исполнителям в этой группе
    stats = conn.execute(
        '''SELECT assigned_username,
                  COUNT(*) as total,
                  SUM(CASE WHEN status = "done" THEN 1 ELSE 0 END) as done_cnt
           FROM tasks
           WHERE group_id = ? AND assigned_username IS NOT NULL
           GROUP BY assigned_username''',
        (chat.id,)
    ).fetchall()
    conn.close()

    if not stats:
        await update.message.reply_text("📈 Пока нет данных по KPI")
        return

    msg = f"📈 *KPI — {chat.title}*\n\n"
    for s in stats:
        total = s['total']
        done_cnt = s['done_cnt'] or 0
        pct = int(done_cnt / total * 100) if total > 0 else 0
        bar = "🟢" if pct >= 80 else "🟡" if pct >= 50 else "🔴"
        msg += f"{bar} @{s['assigned_username']}: {done_cnt}/{total} задач ({pct}%)\n"

    await update.message.reply_text(msg, parse_mode='Markdown')


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler(["задача",  "task"],   new_task))
    app.add_handler(CommandHandler(["задачи",  "tasks"],  list_tasks))
    app.add_handler(CommandHandler(["готово",  "done"],   done_task))
    app.add_handler(CommandHandler(["статус",  "status"], status))
    app.add_handler(CommandHandler(["кпи",     "kpi"],    kpi))

    logger.info("✅ Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
