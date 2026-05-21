import logging
import sqlite3
import os
from datetime import datetime
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters, ConversationHandler,
    CallbackQueryHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('BOT_TOKEN')
DB_PATH = 'agency.db'

# ── Conversation states ─────────────────────────────────────────
TASK_TYPE, TASK_DESC, TASK_DEADLINE, TASK_ASSIGNEE = range(4)

# ── Task types ──────────────────────────────────────────────────
TASK_TYPES = {
    'shoot':   ('🎬', 'Съемка'),
    'publish': ('📢', 'Публикация'),
    'design':  ('🎨', 'Дизайн'),
    'edit':    ('✂️', 'Монтаж'),
    'other':   ('📌', 'Другое'),
}

STATUS_LABELS = {
    'active':    '🔄 В работе',
    'review':    '👀 Согласование',
    'done':      '✅ Готово',
    'published': '🚀 Опубликовано',
}

BRIEF_FIELDS = {
    'client_name':     '📛 Название клиента',
    'brand_info':      '🏷 Бренд',
    'target_audience': '👥 Целевая аудитория',
    'tone_of_voice':   '🗣 Tone of Voice',
    'main_offers':     '💎 Основные офферы',
    'forbidden_words': '🚫 Запрещённые слова',
    'visual_style':    '🎨 Визуал / Цвета',
    'links':           '🔗 Ссылки',
    'goals':           '🎯 Цели / KPI',
    'competitors':     '⚔️ Конкуренты',
}


# ── Database ────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS members (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id   INTEGER NOT NULL,
            username   TEXT    NOT NULL,
            full_name  TEXT,
            added_at   TEXT    DEFAULT (datetime('now')),
            UNIQUE(group_id, username)
        );
        CREATE TABLE IF NOT EXISTS project_brief (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id         INTEGER NOT NULL,
            thread_id        INTEGER DEFAULT 0,
            client_name      TEXT,
            brand_info       TEXT,
            target_audience  TEXT,
            tone_of_voice    TEXT,
            main_offers      TEXT,
            forbidden_words  TEXT,
            visual_style     TEXT,
            links            TEXT,
            goals            TEXT,
            competitors      TEXT,
            updated_by       TEXT,
            updated_at       TEXT DEFAULT (datetime('now')),
            UNIQUE(group_id, thread_id)
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id          INTEGER NOT NULL,
            thread_id         INTEGER DEFAULT 0,
            task_type         TEXT    DEFAULT 'other',
            description       TEXT    NOT NULL,
            deadline          TEXT,
            assigned_username TEXT,
            status            TEXT    DEFAULT 'active',
            created_by        TEXT,
            created_at        TEXT    DEFAULT (datetime('now')),
            done_at           TEXT
        );
        CREATE TABLE IF NOT EXISTS ideas (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id   INTEGER NOT NULL,
            thread_id  INTEGER DEFAULT 0,
            text       TEXT    NOT NULL,
            priority   TEXT    DEFAULT 'normal',
            created_by TEXT,
            created_at TEXT    DEFAULT (datetime('now')),
            converted  INTEGER DEFAULT 0
        );
    ''')
    conn.commit()
    conn.close()


# ── Helpers ─────────────────────────────────────────────────────

def get_thread(update: Update) -> int:
    msg = update.effective_message
    return (msg.message_thread_id or 0) if msg else 0

def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("➕ Задача"),   KeyboardButton("📋 Список")],
        [KeyboardButton("📅 Контент"), KeyboardButton("💡 Идеи")],
        [KeyboardButton("📁 Проект"),  KeyboardButton("📈 KPI")],
    ], resize_keyboard=True)

def task_type_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Съемка",     callback_data="tt_shoot"),
         InlineKeyboardButton("📢 Публикация", callback_data="tt_publish")],
        [InlineKeyboardButton("🎨 Дизайн",     callback_data="tt_design"),
         InlineKeyboardButton("✂️ Монтаж",     callback_data="tt_edit")],
        [InlineKeyboardButton("📌 Другое",     callback_data="tt_other")],
    ])

def assignee_keyboard(group_id: int):
    conn = get_db()
    members = conn.execute(
        'SELECT username, full_name FROM members WHERE group_id = ? ORDER BY full_name',
        (group_id,)
    ).fetchall()
    conn.close()
    if not members:
        return None
    buttons, row = [], []
    for m in members:
        label = m['full_name'] or f"@{m['username']}"
        row.append(InlineKeyboardButton(label, callback_data=f"asgn_{m['username']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⏭ Без исполнителя", callback_data="asgn_skip")])
    return InlineKeyboardMarkup(buttons)

def task_action_kb(task_id: int, status: str):
    s = status
    if s == 'active':
        rows = [[
            InlineKeyboardButton("👀 На согласование", callback_data=f"st_{task_id}_review"),
            InlineKeyboardButton("✅ Готово",           callback_data=f"st_{task_id}_done"),
        ]]
    elif s == 'review':
        rows = [[
            InlineKeyboardButton("🔄 Вернуть в работу", callback_data=f"st_{task_id}_active"),
            InlineKeyboardButton("🚀 Опубликовано",     callback_data=f"st_{task_id}_published"),
        ]]
    elif s == 'done':
        rows = [[InlineKeyboardButton("🚀 Опубликовано", callback_data=f"st_{task_id}_published")]]
    else:
        return None
    return InlineKeyboardMarkup(rows)


# ── /start ──────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ['group', 'supergroup']:
        await update.message.reply_text(
            f"✅ *{chat.title}* подключена!\n\n"
            "Зарегистрируйся в команде: /join\n"
            "Используй кнопки внизу 👇",
            parse_mode='Markdown',
            reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text(
            "👋 Привет!\n\n"
            "Добавь меня в группу клиента и напиши /start\n\n"
            "Команды:\n"
            "/my — мои задачи\n"
            "/join — войти в команду группы"
        )


# ── /join ───────────────────────────────────────────────────────

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Команда работает только в группах")
        return
    username  = user.username or str(user.id)
    full_name = user.full_name or username
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO members (group_id, username, full_name) VALUES (?,?,?)',
        (chat.id, username, full_name)
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"✅ *{full_name}* теперь в команде!\n"
        "Тебя можно назначать исполнителем задач.",
        parse_mode='Markdown'
    )


# ── /my — личные задачи ─────────────────────────────────────────

async def my_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or str(user.id)
    conn = get_db()
    rows = conn.execute(
        '''SELECT t.*, b.client_name
           FROM tasks t
           LEFT JOIN project_brief b ON t.group_id = b.group_id AND t.thread_id = b.thread_id
           WHERE t.assigned_username = ? AND t.status NOT IN ('done','published')
           ORDER BY t.deadline NULLS LAST, t.created_at''',
        (username,)
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("🎉 У тебя нет активных задач!")
        return
    msg = f"📋 *Мои задачи — @{username}*\n\n"
    for t in rows:
        emoji  = TASK_TYPES.get(t['task_type'], ('📌',))[0]
        status = STATUS_LABELS.get(t['status'], t['status'])
        client = t['client_name'] or '—'
        msg += f"{emoji} *#{t['id']}* {t['description']}\n"
        msg += f"   {status} · {client}"
        if t['deadline']:
            msg += f" · 📅 {t['deadline']}"
        msg += "\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')


# ── Task creation ────────────────────────────────────────────────

async def new_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Добавь меня в группу!")
        return ConversationHandler.END
    await update.message.reply_text(
        "➕ *Новая задача*\n\nВыбери тип:",
        parse_mode='Markdown',
        reply_markup=task_type_keyboard()
    )
    return TASK_TYPE

async def task_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ttype = q.data.replace("tt_", "")
    context.user_data['t_type']   = ttype
    context.user_data['t_gid']    = q.message.chat.id
    context.user_data['t_tid']    = q.message.message_thread_id or 0
    emoji, name = TASK_TYPES[ttype]
    await q.edit_message_text(f"{emoji} *{name}*\n\nОпиши задачу:", parse_mode='Markdown')
    return TASK_DESC

async def task_desc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_desc'] = update.message.text
    await update.message.reply_text(
        "📅 Дедлайн? Например: *25.05* или *25.05 18:00*",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⏭ Без дедлайна", callback_data="dl_skip")
        ]])
    )
    return TASK_DEADLINE

async def task_deadline_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_deadline'] = update.message.text
    return await ask_assignee(update, context)

async def task_deadline_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("📅 Без дедлайна")
    context.user_data['t_deadline'] = None
    return await ask_assignee_cb(q, context)

async def ask_assignee(update, context):
    gid = context.user_data.get('t_gid', update.effective_chat.id)
    kb  = assignee_keyboard(gid)
    if kb:
        await update.message.reply_text("👤 Кому назначить?", reply_markup=kb)
    else:
        await update.message.reply_text(
            "👤 Кому назначить? Напиши @username\n"
            "_(Сначала зарегистрируй команду через /join)_",
            parse_mode='Markdown'
        )
    return TASK_ASSIGNEE

async def ask_assignee_cb(query, context):
    gid = context.user_data.get('t_gid', query.message.chat.id)
    kb  = assignee_keyboard(gid)
    if kb:
        await query.message.reply_text("👤 Кому назначить?", reply_markup=kb)
    else:
        await query.message.reply_text(
            "👤 Кому назначить? Напиши @username\n"
            "_(Зарегистрируй команду: /join)_",
            parse_mode='Markdown'
        )
    return TASK_ASSIGNEE

async def task_assignee_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "asgn_skip":
        context.user_data['t_assignee'] = None
        await q.edit_message_text("👤 Без исполнителя")
    else:
        username = q.data.replace("asgn_", "")
        context.user_data['t_assignee'] = username
        await q.edit_message_text(f"👤 @{username}")
    return await do_save_task(update, context, via_cb=True)

async def task_assignee_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data['t_assignee'] = text.lstrip('@') if text.startswith('@') else text
    return await do_save_task(update, context, via_cb=False)

async def do_save_task(update, context, via_cb=False):
    gid      = context.user_data.get('t_gid', 0)
    tid      = context.user_data.get('t_tid', 0)
    ttype    = context.user_data.get('t_type', 'other')
    desc     = context.user_data.get('t_desc', '—')
    deadline = context.user_data.get('t_deadline')
    assignee = context.user_data.get('t_assignee')
    user     = update.effective_user
    created_by = user.username or str(user.id) if user else 'bot'

    conn = get_db()
    cur  = conn.execute(
        '''INSERT INTO tasks (group_id, thread_id, task_type, description, deadline, assigned_username, created_by)
           VALUES (?,?,?,?,?,?,?)''',
        (gid, tid, ttype, desc, deadline, assignee, created_by)
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()

    emoji, tname = TASK_TYPES.get(ttype, ('📌', 'Задача'))
    msg = f"✅ *Задача #{task_id} создана*\n\n{emoji} {tname}: {desc}\n"
    if assignee:  msg += f"👤 @{assignee}\n"
    if deadline:  msg += f"📅 {deadline}\n"

    action_kb = task_action_kb(task_id, 'active')
    context.user_data.clear()

    send = (update.callback_query.message if via_cb else update.message)
    await send.reply_text(msg, parse_mode='Markdown', reply_markup=main_keyboard())
    if action_kb:
        await send.reply_text("Управление:", reply_markup=action_kb)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено", reply_markup=main_keyboard())
    return ConversationHandler.END


# ── Status change ────────────────────────────────────────────────

async def status_change_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, task_id_s, new_status = q.data.split("_", 2)
    task_id = int(task_id_s)
    done_at = datetime.now().isoformat() if new_status in ('done', 'published') else None
    conn = get_db()
    conn.execute('UPDATE tasks SET status=?, done_at=? WHERE id=?', (new_status, done_at, task_id))
    conn.commit()
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    conn.close()
    emoji  = TASK_TYPES.get(task['task_type'], ('📌',))[0]
    status = STATUS_LABELS.get(new_status, new_status)
    text   = f"{emoji} *#{task_id} {task['description']}*\n{status}"
    if task['assigned_username']: text += f"\n👤 @{task['assigned_username']}"
    if task['deadline']:          text += f"\n📅 {task['deadline']}"
    await q.edit_message_text(text, parse_mode='Markdown',
                              reply_markup=task_action_kb(task_id, new_status))


# ── Task list ────────────────────────────────────────────────────

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!")
        return
    tid = get_thread(update)
    conn = get_db()
    rows = conn.execute(
        '''SELECT * FROM tasks
           WHERE group_id=? AND thread_id=? AND status NOT IN ('done','published')
           ORDER BY
             CASE status WHEN 'review' THEN 1 WHEN 'active' THEN 2 ELSE 3 END,
             deadline NULLS LAST''',
        (chat.id, tid)
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("🎉 Активных задач нет!", reply_markup=main_keyboard())
        return
    msg = "📋 *Активные задачи*\n\n"
    for t in rows:
        emoji  = TASK_TYPES.get(t['task_type'], ('📌',))[0]
        status = STATUS_LABELS.get(t['status'], t['status'])
        msg += f"{emoji} *#{t['id']}* {t['description']}\n"
        msg += f"   {status}"
        if t['assigned_username']: msg += f" · @{t['assigned_username']}"
        if t['deadline']:          msg += f" · 📅 {t['deadline']}"
        msg += "\n\n"
    msg += "_/done 5 — закрыть задачу_"
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_keyboard())


# ── /done ────────────────────────────────────────────────────────

async def done_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажи номер: `/done 5`", parse_mode='Markdown')
        return
    tid = context.args[0]
    conn = get_db()
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    if task:
        conn.execute('UPDATE tasks SET status="done", done_at=? WHERE id=?',
                     (datetime.now().isoformat(), tid))
        conn.commit()
        await update.message.reply_text(
            f"✅ Задача #{tid} выполнена!\n_{task['description']}_",
            parse_mode='Markdown', reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text(f"❌ Задача #{tid} не найдена")
    conn.close()


# ── Content calendar ─────────────────────────────────────────────

async def content_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!")
        return
    tid = get_thread(update)
    conn = get_db()
    rows = conn.execute(
        '''SELECT * FROM tasks WHERE group_id=? AND thread_id=?
           ORDER BY
             CASE status WHEN 'active' THEN 1 WHEN 'review' THEN 2
                         WHEN 'done' THEN 3 WHEN 'published' THEN 4 ELSE 5 END,
             deadline NULLS LAST
           LIMIT 20''',
        (chat.id, tid)
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text(
            "📅 Контент-план пуст.\nСоздай задачу → ➕ Задача",
            reply_markup=main_keyboard()
        )
        return
    ST = {'active': '🟡', 'review': '🔵', 'done': '🟢', 'published': '✅', 'idea': '⚪'}
    msg = "📅 *Контент-план*\n\n"
    for t in rows:
        emoji  = TASK_TYPES.get(t['task_type'], ('📌',))[0]
        dot    = ST.get(t['status'], '⚪')
        msg   += f"{dot} {emoji} {t['description']}"
        if t['deadline']:          msg += f" — *{t['deadline']}*"
        if t['assigned_username']: msg += f" @{t['assigned_username']}"
        msg   += "\n"
    msg += "\n⚪ Идея · 🟡 В работе · 🔵 Согласование · 🟢 Готово · ✅ Опубликовано"
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_keyboard())


# ── Ideas ─────────────────────────────────────────────────────────

async def ideas_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!")
        return
    tid = get_thread(update)
    conn = get_db()
    rows = conn.execute(
        '''SELECT * FROM ideas WHERE group_id=? AND thread_id=? AND converted=0
           ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                    created_at DESC LIMIT 10''',
        (chat.id, tid)
    ).fetchall()
    conn.close()
    P = {'high': '🔴', 'normal': '🟡', 'low': '⚪'}
    if not rows:
        msg = "💡 *Идеи*\n\nПока пусто.\n\nДобавить: `/idea текст`"
    else:
        msg = "💡 *Идеи*\n\n"
        for i in rows:
            p    = P.get(i['priority'], '🟡')
            msg += f"{p} *#{i['id']}* {i['text']}\n"
        msg += "\nДобавить: `/idea текст`\nВысокий приоритет: `/idea !текст`"
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_keyboard())

async def add_idea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!")
        return
    if not context.args:
        await update.message.reply_text("Напиши: `/idea текст идеи`", parse_mode='Markdown')
        return
    text     = ' '.join(context.args)
    priority = 'normal'
    if text.startswith('!'):
        priority = 'high';   text = text[1:].strip()
    elif text.startswith('-'):
        priority = 'low';    text = text[1:].strip()
    tid        = get_thread(update)
    user       = update.effective_user
    created_by = user.username or str(user.id)
    conn = get_db()
    conn.execute('INSERT INTO ideas (group_id,thread_id,text,priority,created_by) VALUES (?,?,?,?,?)',
                 (chat.id, tid, text, priority, created_by))
    conn.commit()
    conn.close()
    emoji = {'high': '🔴', 'normal': '🟡', 'low': '⚪'}.get(priority, '🟡')
    await update.message.reply_text(f"💡 Идея сохранена {emoji}\n_{text}_", parse_mode='Markdown')


# ── Project brief ─────────────────────────────────────────────────

async def project_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!")
        return
    tid = get_thread(update)
    conn = get_db()
    brief = conn.execute(
        'SELECT * FROM project_brief WHERE group_id=? AND thread_id=?', (chat.id, tid)
    ).fetchone()
    conn.close()
    msg = "📁 *О проекте*\n\n"
    if brief:
        for field, label in BRIEF_FIELDS.items():
            val = brief[field]
            if val:
                msg += f"*{label}:*\n{val}\n\n"
    else:
        msg += "Ещё ничего не заполнено.\n\n"
    msg += "Выбери раздел для редактирования:"
    buttons, row = [], []
    for field, label in BRIEF_FIELDS.items():
        row.append(InlineKeyboardButton(label, callback_data=f"br_{field}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    await update.message.reply_text(
        msg, parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def brief_field_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    field = q.data.replace("br_", "")
    label = BRIEF_FIELDS.get(field, field)
    context.user_data['brief_field']  = field
    context.user_data['brief_gid']    = q.message.chat.id
    context.user_data['brief_tid']    = q.message.message_thread_id or 0
    await q.message.reply_text(
        f"✏️ *{label}*\n\nНапиши текст (/cancel — отмена):",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardRemove()
    )

async def brief_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get('brief_field')
    gid   = context.user_data.get('brief_gid')
    tid   = context.user_data.get('brief_tid', 0)
    value = update.message.text
    user  = update.effective_user
    updated_by = user.username or str(user.id)
    conn = get_db()
    existing = conn.execute(
        'SELECT id FROM project_brief WHERE group_id=? AND thread_id=?', (gid, tid)
    ).fetchone()
    if existing:
        conn.execute(
            f'UPDATE project_brief SET {field}=?, updated_by=?, updated_at=datetime("now") '
            f'WHERE group_id=? AND thread_id=?',
            (value, updated_by, gid, tid)
        )
    else:
        conn.execute(
            f'INSERT INTO project_brief (group_id, thread_id, {field}, updated_by) VALUES (?,?,?,?)',
            (gid, tid, value, updated_by)
        )
    conn.commit()
    conn.close()
    label = BRIEF_FIELDS.get(field, field)
    context.user_data.pop('brief_field', None)
    context.user_data.pop('brief_gid', None)
    context.user_data.pop('brief_tid', None)
    await update.message.reply_text(
        f"✅ *{label}* обновлено!", parse_mode='Markdown', reply_markup=main_keyboard()
    )


# ── KPI ───────────────────────────────────────────────────────────

async def kpi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!")
        return
    tid = get_thread(update)
    conn = get_db()
    stats = conn.execute(
        '''SELECT assigned_username,
                  COUNT(*) as total,
                  SUM(CASE WHEN status IN ('done','published') THEN 1 ELSE 0 END) as done_cnt,
                  SUM(CASE WHEN status = 'review'             THEN 1 ELSE 0 END) as review_cnt,
                  SUM(CASE WHEN status = 'active'             THEN 1 ELSE 0 END) as active_cnt
           FROM tasks
           WHERE group_id=? AND thread_id=? AND assigned_username IS NOT NULL
           GROUP BY assigned_username
           ORDER BY done_cnt DESC''',
        (chat.id, tid)
    ).fetchall()
    conn.close()
    if not stats:
        await update.message.reply_text(
            "📈 Пока нет данных.\n\nСоздай задачи и назначь исполнителей.",
            reply_markup=main_keyboard()
        )
        return
    msg = "📈 *KPI команды*\n\n"
    for s in stats:
        total  = s['total']
        done   = s['done_cnt']   or 0
        review = s['review_cnt'] or 0
        active = s['active_cnt'] or 0
        pct    = int(done / total * 100) if total else 0
        bar    = "🟢" if pct >= 80 else "🟡" if pct >= 50 else "🔴"
        msg   += f"{bar} *@{s['assigned_username']}*\n"
        msg   += f"   ✅ {done} · 👀 {review} · 🔄 {active} · Всего: {total} ({pct}%)\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_keyboard())


# ── /cancel (global) ─────────────────────────────────────────────

async def cancel_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('brief_field'):
        context.user_data.pop('brief_field', None)
        context.user_data.pop('brief_gid', None)
        context.user_data.pop('brief_tid', None)
        await update.message.reply_text("❌ Отменено", reply_markup=main_keyboard())
    else:
        await update.message.reply_text("❌ Нечего отменять", reply_markup=main_keyboard())


# ── General text handler ─────────────────────────────────────────

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Brief editing state?
    if context.user_data.get('brief_field'):
        await brief_value_received(update, context)
        return
    # Main menu buttons
    text = update.message.text
    if   text == "📋 Список":   await list_tasks(update, context)
    elif text == "📅 Контент":  await content_plan(update, context)
    elif text == "💡 Идеи":     await ideas_list(update, context)
    elif text == "📁 Проект":   await project_brief(update, context)
    elif text == "📈 KPI":      await kpi(update, context)


# ── main ─────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    task_conv = ConversationHandler(
        entry_points=[
            CommandHandler("task", new_task_start),
            MessageHandler(filters.Regex("^➕ Задача$"), new_task_start),
        ],
        states={
            TASK_TYPE: [
                CallbackQueryHandler(task_type_chosen, pattern="^tt_"),
            ],
            TASK_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, task_desc_received),
            ],
            TASK_DEADLINE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, task_deadline_text),
                CallbackQueryHandler(task_deadline_skip, pattern="^dl_skip$"),
            ],
            TASK_ASSIGNEE: [
                CallbackQueryHandler(task_assignee_chosen, pattern="^asgn_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, task_assignee_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("join",   join))
    app.add_handler(CommandHandler("my",     my_tasks))
    app.add_handler(CommandHandler("done",   done_task))
    app.add_handler(CommandHandler("idea",   add_idea))
    app.add_handler(CommandHandler("cancel", cancel_brief))
    app.add_handler(task_conv)
    app.add_handler(CallbackQueryHandler(status_change_cb,   pattern=r"^st_\d+_"))
    app.add_handler(CallbackQueryHandler(brief_field_chosen, pattern="^br_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("✅ WhyNot бот v2 запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
