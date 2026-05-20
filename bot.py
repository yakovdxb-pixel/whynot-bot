import logging
import sqlite3
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (Application, CommandHandler, ContextTypes,
                           MessageHandler, filters, ConversationHandler)
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('BOT_TOKEN')
DB_PATH = 'agency.db'

TASK_TITLE, TASK_USER, TASK_DEADLINE = range(3)


# ─── База данных ──────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS projects (
            group_id   INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id          INTEGER NOT NULL,
            title             TEXT NOT NULL,
            assigned_username TEXT,
            deadline          TEXT,
            status            TEXT DEFAULT 'active',
            created_by        TEXT,
            created_at        TEXT DEFAULT (datetime('now'))
        );
    ''')
    conn.commit()
    conn.close()


# ─── Клавиатура ───────────────────────────────────────────────

def main_keyboard():
    buttons = [
        [KeyboardButton("📝 Новая задача"), KeyboardButton("📋 Задачи")],
        [KeyboardButton("📊 Статус"),        KeyboardButton("📈 KPI")],
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


# ─── /start ───────────────────────────────────────────────────

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
            text = (
                f"✅ *{chat.title}* подключена!\n\n"
                f"Используй кнопки внизу экрана 👇"
            )
        else:
            text = f"👋 Группа *{chat.title}* уже подключена!"
        conn.close()
        await update.message.reply_text(
            text, parse_mode='Markdown', reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text(
            "👋 Привет! Добавь меня в группу клиента и напиши /start"
        )


# ─── Создание задачи (разговор) ───────────────────────────────

async def new_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Добавь меня в группу клиента!")
        return ConversationHandler.END
    await update.message.reply_text(
        "📝 *Новая задача*\n\nОпиши задачу:",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardRemove()
    )
    return TASK_TITLE

async def task_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['task_title'] = update.message.text
    await update.message.reply_text(
        "👤 Кому назначить?\nНапиши @username или нажми /skip"
    )
    return TASK_USER

async def task_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    context.user_data['task_user'] = text.lstrip('@') if text.startswith('@') else text
    await update.message.reply_text(
        "📅 Дедлайн? (например: 25.05)\nИли нажми /skip"
    )
    return TASK_DEADLINE

async def task_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['task_deadline'] = update.message.text
    return await save_task(update, context)

async def task_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('_conv_state', TASK_USER)
    if state == TASK_USER:
        context.user_data['task_user'] = None
        await update.message.reply_text("📅 Дедлайн? (например: 25.05)\nИли /skip")
        context.user_data['_conv_state'] = TASK_DEADLINE
        return TASK_DEADLINE
    else:
        context.user_data['task_deadline'] = None
        return await save_task(update, context)

async def save_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    title    = context.user_data.get('task_title', '—')
    assigned = context.user_data.get('task_user')
    deadline = context.user_data.get('task_deadline')

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

    context.user_data.clear()
    await update.message.reply_text(
        msg, parse_mode='Markdown', reply_markup=main_keyboard()
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Отменено", reply_markup=main_keyboard()
    )
    return ConversationHandler.END


# ─── Список задач ─────────────────────────────────────────────

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Добавь меня в группу клиента!")
        return

    conn = get_db()
    tasks = conn.execute(
        'SELECT * FROM tasks WHERE group_id = ? AND status = "active" ORDER BY created_at',
        (chat.id,)
    ).fetchall()
    conn.close()

    if not tasks:
        await update.message.reply_text(
            "🎉 Активных задач нет!", reply_markup=main_keyboard()
        )
        return

    msg = f"📋 *Задачи — {chat.title}*\n\n"
    for t in tasks:
        msg += f"*#{t['id']}* {t['title']}"
        if t['assigned_username']:
            msg += f" → @{t['assigned_username']}"
        if t['deadline']:
            msg += f" 📅{t['deadline']}"
        msg += "\n"

    msg += "\nЧтобы закрыть задачу: `/done 5`"
    await update.message.reply_text(
        msg, parse_mode='Markdown', reply_markup=main_keyboard()
    )


# ─── /done ────────────────────────────────────────────────────

async def done_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Укажи номер: `/done 5`", parse_mode='Markdown'
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
            parse_mode='Markdown', reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text(f"❌ Задача #{task_id} не найдена")
    conn.close()


# ─── Статус ───────────────────────────────────────────────────

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Добавь меня в группу клиента!")
        return

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

    await update.message.reply_text(
        msg, parse_mode='Markdown', reply_markup=main_keyboard()
    )


# ─── KPI ──────────────────────────────────────────────────────

async def kpi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Добавь меня в группу клиента!")
        return

    conn = get_db()
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
        await update.message.reply_text(
            "📈 Пока нет данных по KPI", reply_markup=main_keyboard()
        )
        return

    msg = f"📈 *KPI — {chat.title}*\n\n"
    for s in stats:
        total    = s['total']
        done_cnt = s['done_cnt'] or 0
        pct      = int(done_cnt / total * 100) if total > 0 else 0
        bar      = "🟢" if pct >= 80 else "🟡" if pct >= 50 else "🔴"
        msg += f"{bar} @{s['assigned_username']}: {done_cnt}/{total} ({pct}%)\n"

    await update.message.reply_text(
        msg, parse_mode='Markdown', reply_markup=main_keyboard()
    )


# ─── Обработчик кнопок ────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📋 Задачи":
        await list_tasks(update, context)
    elif text == "📊 Статус":
        await status(update, context)
    elif text == "📈 KPI":
        await kpi(update, context)


# ─── Запуск ───────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("task", new_task_start),
            MessageHandler(filters.Regex("^📝 Новая задача$"), new_task_start),
        ],
        states={
            TASK_TITLE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, task_title)],
            TASK_USER:     [
                MessageHandler(filters.TEXT & ~filters.COMMAND, task_user),
                CommandHandler("skip", task_skip),
            ],
            TASK_DEADLINE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, task_deadline),
                CommandHandler("skip", task_skip),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("tasks",  list_tasks))
    app.add_handler(CommandHandler("done",   done_task))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("kpi",    kpi))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, button_handler
    ))

    logger.info("✅ Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
