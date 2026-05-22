import logging
import sqlite3
import os
import calendar as cal_lib
from datetime import datetime, date
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

TOKEN   = os.getenv('BOT_TOKEN')
DB_PATH = 'agency.db'

# ── Conversation states ─────────────────────────────────────────
# Task creation
T_TYPE, T_DESC, T_DATE, T_ASSIGNEE = range(4)
# Content plan
CP_DATE, CP_TYPE_S, CP_DESC_S = range(4, 7)

# ── Constants ───────────────────────────────────────────────────
TASK_TYPES = {
    'shoot':   ('🎬', 'Съемка'),
    'publish': ('📢', 'Публикация'),
    'design':  ('🎨', 'Дизайн'),
    'edit':    ('✂️', 'Монтаж'),
    'other':   ('📌', 'Другое'),
}

STATUSES = {
    'active':    '🔄 В работе',
    'submitted': '📨 Сдано на проверку',
    'revision':  '🔁 На доработку',
    'approved':  '✅ Принято',
    'published': '🚀 Опубликовано',
}

ROLES = {
    'director': '👑 Директор',
    'am':       '📋 АМ',
    'executor': '✂️ Исполнитель',
}

BRIEF_FIELDS = {
    'brand_info':      '🏷 Бренд',
    'target_audience': '👥 Целевая аудитория',
    'tone_of_voice':   '🗣 Tone of Voice',
    'main_offers':     '💎 Основные офферы',
    'visual_style':    '🎨 Визуал / Цвета',
    'links':           '🔗 Ссылки',
    'competitors':     '⚔️ Конкуренты',
    'visual_refs':     '🖼 Референсы',
}

MONTH_RU = ['', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
            'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']


# ── Database ────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS members (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id  INTEGER NOT NULL,
            username  TEXT    NOT NULL,
            full_name TEXT,
            role      TEXT    DEFAULT 'executor',
            added_at  TEXT    DEFAULT (datetime('now')),
            UNIQUE(group_id, username)
        );
        CREATE TABLE IF NOT EXISTS project_brief (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id         INTEGER NOT NULL,
            thread_id        INTEGER DEFAULT 0,
            brand_info       TEXT,
            target_audience  TEXT,
            tone_of_voice    TEXT,
            main_offers      TEXT,
            visual_style     TEXT,
            links            TEXT,
            competitors      TEXT,
            visual_refs      TEXT,
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
            task_date         TEXT,
            assigned_username TEXT,
            status            TEXT    DEFAULT 'active',
            created_by        TEXT,
            created_at        TEXT    DEFAULT (datetime('now')),
            done_at           TEXT
        );
        CREATE TABLE IF NOT EXISTS content_plan (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id     INTEGER NOT NULL,
            thread_id    INTEGER DEFAULT 0,
            plan_date    TEXT    NOT NULL,
            content_type TEXT,
            description  TEXT    NOT NULL,
            status       TEXT    DEFAULT 'planned',
            created_by   TEXT,
            created_at   TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS kpi_goals (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id  INTEGER NOT NULL,
            thread_id INTEGER DEFAULT 0,
            metric    TEXT    NOT NULL,
            value     TEXT    NOT NULL,
            set_by    TEXT,
            set_at    TEXT    DEFAULT (datetime('now')),
            UNIQUE(group_id, thread_id, metric)
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
        CREATE TABLE IF NOT EXISTS change_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id    INTEGER,
            thread_id   INTEGER DEFAULT 0,
            entity_type TEXT,
            entity_id   INTEGER,
            field_name  TEXT,
            old_value   TEXT,
            new_value   TEXT,
            changed_by  TEXT,
            changed_at  TEXT    DEFAULT (datetime('now'))
        );
    ''')
    conn.commit()
    conn.close()


# ── Helpers ─────────────────────────────────────────────────────

def get_thread(update: Update) -> int:
    msg = update.effective_message
    return (msg.message_thread_id or 0) if msg else 0

def get_uname(update: Update) -> str:
    u = update.effective_user
    return (u.username or str(u.id)) if u else 'unknown'

def log_change(group_id, thread_id, entity_type, entity_id,
               field, old_val, new_val, changed_by):
    conn = get_db()
    conn.execute(
        '''INSERT INTO change_log
           (group_id, thread_id, entity_type, entity_id, field_name,
            old_value, new_value, changed_by)
           VALUES (?,?,?,?,?,?,?,?)''',
        (group_id, thread_id, entity_type, entity_id, field,
         str(old_val) if old_val is not None else '—',
         str(new_val) if new_val is not None else '—',
         changed_by)
    )
    conn.commit()
    conn.close()

def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("➕ Задача"),    KeyboardButton("📋 Список")],
        [KeyboardButton("📅 Контент"),  KeyboardButton("💡 Идеи")],
        [KeyboardButton("📁 Проект"),   KeyboardButton("📈 KPI")],
    ], resize_keyboard=True)

def build_calendar(year: int, month: int, prefix: str) -> InlineKeyboardMarkup:
    pm = month - 1 if month > 1 else 12
    py = year if month > 1 else year - 1
    nm = month + 1 if month < 12 else 1
    ny = year if month < 12 else year + 1
    rows = []
    rows.append([
        InlineKeyboardButton("◀️", callback_data=f"{prefix}prev_{py}_{pm:02d}"),
        InlineKeyboardButton(f"{MONTH_RU[month]} {year}", callback_data=f"{prefix}noop"),
        InlineKeyboardButton("▶️", callback_data=f"{prefix}next_{ny}_{nm:02d}"),
    ])
    rows.append([InlineKeyboardButton(d, callback_data=f"{prefix}noop")
                 for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]])
    for week in cal_lib.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton("·", callback_data=f"{prefix}noop"))
            else:
                row.append(InlineKeyboardButton(
                    str(day),
                    callback_data=f"{prefix}day_{year}_{month:02d}_{day:02d}"
                ))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

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
        'SELECT username, full_name, role FROM members WHERE group_id=? ORDER BY full_name',
        (group_id,)
    ).fetchall()
    conn.close()
    if not members:
        return None
    role_emoji = {'director': '👑', 'am': '📋', 'executor': '✂️'}
    buttons, row = [], []
    for m in members:
        em    = role_emoji.get(m['role'], '')
        label = f"{em} {m['full_name'] or m['username']}"
        row.append(InlineKeyboardButton(label, callback_data=f"asgn_{m['username']}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⏭ Без исполнителя", callback_data="asgn_skip")])
    return InlineKeyboardMarkup(buttons)

def task_action_kb(task_id: int, status: str):
    if status == 'active':
        rows = [[InlineKeyboardButton("📨 Сдать на проверку",
                                      callback_data=f"st_{task_id}_submitted")]]
    elif status == 'submitted':
        rows = [[
            InlineKeyboardButton("✅ Принять",        callback_data=f"st_{task_id}_approved"),
            InlineKeyboardButton("🔁 На доработку",  callback_data=f"st_{task_id}_revision"),
        ]]
    elif status == 'revision':
        rows = [[InlineKeyboardButton("📨 Сдать снова",
                                      callback_data=f"st_{task_id}_submitted")]]
    elif status == 'approved':
        rows = [[InlineKeyboardButton("🚀 Опубликовано",
                                      callback_data=f"st_{task_id}_published")]]
    else:
        return None
    return InlineKeyboardMarkup(rows)


# ── /start & /join ───────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ['group', 'supergroup']:
        await update.message.reply_text(
            f"✅ *{chat.title}*\n\n"
            "Зарегистрируйся в команде: /join\n"
            "Используй кнопки внизу 👇",
            parse_mode='Markdown',
            reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text(
            "👋 Добавь меня в группу и напиши /start\n\n"
            "/my — мои задачи\n"
            "/join — войти в команду"
        )

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах")
        return
    username  = user.username or str(user.id)
    full_name = user.full_name or username
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👑 Директор",    callback_data=f"role_director_{username}"),
        InlineKeyboardButton("📋 АМ",          callback_data=f"role_am_{username}"),
        InlineKeyboardButton("✂️ Исполнитель", callback_data=f"role_executor_{username}"),
    ]])
    await update.message.reply_text(
        f"👋 *{full_name}*, выбери роль:",
        parse_mode='Markdown', reply_markup=kb
    )

async def role_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # role_ROLE_username
    parts    = q.data.split("_", 2)
    role     = parts[1]
    username = parts[2]
    user     = q.from_user
    full_name = user.full_name or username
    chat = q.message.chat
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO members (group_id, username, full_name, role) VALUES (?,?,?,?)',
        (chat.id, username, full_name, role)
    )
    conn.commit()
    conn.close()
    await q.edit_message_text(f"✅ @{username} — {ROLES.get(role, role)}")


# ── /my ─────────────────────────────────────────────────────────

async def my_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = get_uname(update)
    conn = get_db()
    rows = conn.execute(
        '''SELECT * FROM tasks WHERE assigned_username=?
           AND status NOT IN ('approved','published')
           ORDER BY task_date NULLS LAST, created_at''',
        (username,)
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("🎉 Нет активных задач!")
        return
    msg = f"📋 *Мои задачи — @{username}*\n\n"
    for t in rows:
        emoji  = TASK_TYPES.get(t['task_type'], ('📌',))[0]
        status = STATUSES.get(t['status'], t['status'])
        msg   += f"{emoji} *#{t['id']}* {t['description']}\n"
        msg   += f"   {status}"
        if t['task_date']: msg += f" · 📅 {t['task_date']}"
        msg   += "\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')


# ── Task creation ────────────────────────────────────────────────

async def new_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!")
        return ConversationHandler.END
    await update.message.reply_text(
        "➕ *Новая задача*\n\nВыбери тип:",
        parse_mode='Markdown',
        reply_markup=task_type_keyboard()
    )
    return T_TYPE

async def t_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ttype = q.data.replace("tt_", "")
    context.user_data['t_type'] = ttype
    context.user_data['t_gid']  = q.message.chat.id
    context.user_data['t_tid']  = q.message.message_thread_id or 0
    emoji, name = TASK_TYPES[ttype]
    await q.edit_message_text(f"{emoji} *{name}*\n\nОпиши задачу:", parse_mode='Markdown')
    return T_DESC

async def t_desc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_desc'] = update.message.text
    ttype = context.user_data.get('t_type', 'other')
    label = "📅 Дата съёмки:" if ttype == 'shoot' else "📅 Выбери дедлайн:"
    today = date.today()
    await update.message.reply_text(
        label, reply_markup=build_calendar(today.year, today.month, prefix="tc")
    )
    return T_DATE

async def t_cal_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    raw   = q.data.replace("tcprev_", "").replace("tcnext_", "")
    y, m  = raw.split("_"); year, month = int(y), int(m)
    await q.edit_message_reply_markup(
        reply_markup=build_calendar(year, month, prefix="tc")
    )
    return T_DATE

async def t_cal_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return T_DATE

async def t_date_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, y, m, d = q.data.split("_")
    date_str = f"{d}.{m}.{y}"
    context.user_data['t_date'] = date_str
    await q.edit_message_text(f"📅 {date_str}")
    gid = context.user_data.get('t_gid')
    kb  = assignee_keyboard(gid)
    if kb:
        await q.message.reply_text("👤 Кому назначить?", reply_markup=kb)
    else:
        await q.message.reply_text(
            "👤 Кому назначить? Напиши @username\n"
            "_(Зарегистрируй команду через /join)_",
            parse_mode='Markdown'
        )
    return T_ASSIGNEE

async def t_assignee_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "asgn_skip":
        context.user_data['t_assignee'] = None
        await q.edit_message_text("👤 Без исполнителя")
    else:
        username = q.data.replace("asgn_", "")
        context.user_data['t_assignee'] = username
        await q.edit_message_text(f"👤 @{username}")
    return await _save_task(update, context, via_cb=True)

async def t_assignee_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_assignee'] = update.message.text.strip().lstrip('@')
    return await _save_task(update, context, via_cb=False)

async def _save_task(update, context, via_cb=False):
    gid      = context.user_data.get('t_gid', 0)
    tid      = context.user_data.get('t_tid', 0)
    ttype    = context.user_data.get('t_type', 'other')
    desc     = context.user_data.get('t_desc', '—')
    task_date = context.user_data.get('t_date')
    assignee = context.user_data.get('t_assignee')
    user     = update.effective_user
    by       = user.username or str(user.id) if user else 'bot'

    conn = get_db()
    cur  = conn.execute(
        '''INSERT INTO tasks
           (group_id, thread_id, task_type, description, task_date, assigned_username, created_by)
           VALUES (?,?,?,?,?,?,?)''',
        (gid, tid, ttype, desc, task_date, assignee, by)
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()

    log_change(gid, tid, 'task', task_id, 'создана', None,
               f"{TASK_TYPES.get(ttype,('',''))[1]}: {desc}", by)

    emoji, tname = TASK_TYPES.get(ttype, ('📌', 'Задача'))
    msg = f"✅ *Задача #{task_id} создана*\n\n{emoji} {tname}: {desc}\n"
    if assignee:   msg += f"👤 @{assignee}\n"
    if task_date:  msg += f"📅 {task_date}\n"

    context.user_data.clear()
    send = update.callback_query.message if via_cb else update.message
    await send.reply_text(msg, parse_mode='Markdown', reply_markup=main_keyboard())
    kb = task_action_kb(task_id, 'active')
    if kb:
        await send.reply_text("Управление задачей:", reply_markup=kb)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено", reply_markup=main_keyboard())
    return ConversationHandler.END


# ── Status change ────────────────────────────────────────────────

async def status_change_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q  = update.callback_query
    await q.answer()
    _, task_id_s, new_status = q.data.split("_", 2)
    task_id = int(task_id_s)
    by      = q.from_user.username or str(q.from_user.id)

    conn = get_db()
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not task:
        conn.close(); return
    old_status = task['status']
    done_at    = datetime.now().isoformat() if new_status in ('approved', 'published') else None
    conn.execute('UPDATE tasks SET status=?, done_at=? WHERE id=?',
                 (new_status, done_at, task_id))
    conn.commit()
    conn.close()

    log_change(task['group_id'], task['thread_id'], 'task', task_id, 'статус',
               STATUSES.get(old_status, old_status),
               STATUSES.get(new_status, new_status), by)

    emoji  = TASK_TYPES.get(task['task_type'], ('📌',))[0]
    status = STATUSES.get(new_status, new_status)
    text   = f"{emoji} *#{task_id} {task['description']}*\n{status}"
    if task['assigned_username']: text += f"\n👤 @{task['assigned_username']}"
    if task['task_date']:         text += f"\n📅 {task['task_date']}"
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
           WHERE group_id=? AND thread_id=? AND status NOT IN ('approved','published')
           ORDER BY
             CASE status WHEN 'submitted' THEN 1 WHEN 'revision' THEN 2
                         WHEN 'active' THEN 3 ELSE 4 END,
             task_date NULLS LAST''',
        (chat.id, tid)
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("🎉 Активных задач нет!", reply_markup=main_keyboard())
        return
    msg = "📋 *Задачи*\n\n"
    for t in rows:
        emoji  = TASK_TYPES.get(t['task_type'], ('📌',))[0]
        status = STATUSES.get(t['status'], t['status'])
        msg   += f"{emoji} *#{t['id']}* {t['description']}\n"
        msg   += f"   {status}"
        if t['assigned_username']: msg += f" · @{t['assigned_username']}"
        if t['task_date']:         msg += f" · 📅 {t['task_date']}"
        msg   += "\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_keyboard())


# ── Content plan ─────────────────────────────────────────────────

async def content_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!")
        return
    tid = get_thread(update)
    conn = get_db()
    rows = conn.execute(
        '''SELECT * FROM content_plan WHERE group_id=? AND thread_id=?
           ORDER BY plan_date, id LIMIT 30''',
        (chat.id, tid)
    ).fetchall()
    conn.close()
    DOT = {'planned': '⚪', 'in_progress': '🟡', 'done': '🟢', 'published': '✅'}
    if not rows:
        msg = "📅 *Контент-план*\n\nПока пусто."
    else:
        msg = "📅 *Контент-план*\n\n"
        for r in rows:
            dot  = DOT.get(r['status'], '⚪')
            msg += f"{dot} *{r['plan_date']}* {r['content_type']} — {r['description']}\n"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Добавить запись", callback_data="cp_add")
    ]])
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=kb)

async def cp_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data['cp_gid'] = q.message.chat.id
    context.user_data['cp_tid'] = q.message.message_thread_id or 0
    today = date.today()
    await q.message.reply_text(
        "📅 *Новая запись*\n\nВыбери дату публикации:",
        parse_mode='Markdown',
        reply_markup=build_calendar(today.year, today.month, prefix="cp")
    )
    return CP_DATE

async def cp_cal_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    raw  = q.data.replace("cpprev_", "").replace("cpnext_", "")
    y, m = raw.split("_"); year, month = int(y), int(m)
    await q.edit_message_reply_markup(
        reply_markup=build_calendar(year, month, prefix="cp")
    )
    return CP_DATE

async def cp_cal_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return CP_DATE

async def cp_date_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, y, m, d = q.data.split("_")
    date_str = f"{d}.{m}.{y}"
    context.user_data['cp_date'] = date_str
    await q.edit_message_text(f"📅 Дата: {date_str}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Пост",       callback_data="cpt_post"),
         InlineKeyboardButton("📱 Сторис",     callback_data="cpt_story")],
        [InlineKeyboardButton("🎬 Рилс",       callback_data="cpt_reels"),
         InlineKeyboardButton("🎯 Актуальное", callback_data="cpt_highlight")],
    ])
    await q.message.reply_text("Тип контента:", reply_markup=kb)
    return CP_TYPE_S

async def cp_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    TYPES = {'cpt_post': '📸 Пост', 'cpt_story': '📱 Сторис',
             'cpt_reels': '🎬 Рилс', 'cpt_highlight': '🎯 Актуальное'}
    ct = TYPES.get(q.data, q.data)
    context.user_data['cp_type'] = ct
    await q.edit_message_text(f"Тип: {ct}")
    await q.message.reply_text("✏️ Что должно быть в публикации?")
    return CP_DESC_S

async def cp_desc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc      = update.message.text
    gid       = context.user_data.get('cp_gid', update.effective_chat.id)
    tid       = context.user_data.get('cp_tid', 0)
    plan_date = context.user_data.get('cp_date', '—')
    ct        = context.user_data.get('cp_type', '—')
    by        = get_uname(update)

    conn = get_db()
    cur  = conn.execute(
        '''INSERT INTO content_plan
           (group_id, thread_id, plan_date, content_type, description, created_by)
           VALUES (?,?,?,?,?,?)''',
        (gid, tid, plan_date, ct, desc, by)
    )
    cp_id = cur.lastrowid
    conn.commit()
    conn.close()

    log_change(gid, tid, 'content_plan', cp_id, 'создана', None,
               f"{ct} {plan_date}: {desc}", by)

    for k in ('cp_gid', 'cp_tid', 'cp_date', 'cp_type'):
        context.user_data.pop(k, None)

    await update.message.reply_text(
        f"✅ *Добавлено в контент-план*\n\n{ct} — {plan_date}\n_{desc}_",
        parse_mode='Markdown', reply_markup=main_keyboard()
    )
    return ConversationHandler.END


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
        msg = "💡 *Идеи*\n\nПусто.\nДобавить: `/idea текст`"
    else:
        msg = "💡 *Идеи*\n\n"
        for i in rows:
            msg += f"{P.get(i['priority'],'🟡')} *#{i['id']}* {i['text']}\n"
        msg += "\nДобавить: `/idea текст` · Срочно: `/idea !текст`"
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_keyboard())

async def add_idea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        return
    if not context.args:
        await update.message.reply_text("Напиши: `/idea текст`", parse_mode='Markdown')
        return
    text     = ' '.join(context.args)
    priority = 'normal'
    if text.startswith('!'):   priority = 'high'; text = text[1:].strip()
    elif text.startswith('-'): priority = 'low';  text = text[1:].strip()
    tid = get_thread(update)
    by  = get_uname(update)
    conn = get_db()
    conn.execute('INSERT INTO ideas (group_id,thread_id,text,priority,created_by) VALUES (?,?,?,?,?)',
                 (chat.id, tid, text, priority, by))
    conn.commit()
    conn.close()
    P = {'high': '🔴', 'normal': '🟡', 'low': '⚪'}
    await update.message.reply_text(
        f"💡 {P.get(priority,'🟡')} Сохранено\n_{text}_", parse_mode='Markdown'
    )


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
        if brief['updated_by']:
            when = brief['updated_at'][:16].replace('T', ' ')
            msg += f"_Обновил: @{brief['updated_by']} · {when}_\n\n"
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
        msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons)
    )

async def brief_field_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    field = q.data.replace("br_", "")
    label = BRIEF_FIELDS.get(field, field)
    context.user_data['brief_field'] = field
    context.user_data['brief_gid']   = q.message.chat.id
    context.user_data['brief_tid']   = q.message.message_thread_id or 0
    await q.message.reply_text(
        f"✏️ *{label}*\n\nНапиши текст (/cancel — отмена):",
        parse_mode='Markdown', reply_markup=ReplyKeyboardRemove()
    )

async def brief_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get('brief_field')
    gid   = context.user_data.get('brief_gid')
    tid   = context.user_data.get('brief_tid', 0)
    value = update.message.text
    by    = get_uname(update)

    conn = get_db()
    existing = conn.execute(
        'SELECT * FROM project_brief WHERE group_id=? AND thread_id=?', (gid, tid)
    ).fetchone()
    old_val = existing[field] if existing else None
    if existing:
        conn.execute(
            f'UPDATE project_brief SET {field}=?, updated_by=?, updated_at=datetime("now")'
            f' WHERE group_id=? AND thread_id=?',
            (value, by, gid, tid)
        )
    else:
        conn.execute(
            f'INSERT INTO project_brief (group_id, thread_id, {field}, updated_by)'
            f' VALUES (?,?,?,?)',
            (gid, tid, value, by)
        )
    conn.commit()
    conn.close()

    log_change(gid, tid, 'brief', 0, BRIEF_FIELDS.get(field, field), old_val, value, by)

    for k in ('brief_field', 'brief_gid', 'brief_tid'):
        context.user_data.pop(k, None)
    label = BRIEF_FIELDS.get(field, field)
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
    goals = conn.execute(
        'SELECT * FROM kpi_goals WHERE group_id=? AND thread_id=? ORDER BY metric',
        (chat.id, tid)
    ).fetchall()
    stats = conn.execute(
        '''SELECT assigned_username,
                  COUNT(*) as total,
                  SUM(CASE WHEN status IN ('approved','published') THEN 1 ELSE 0 END) as done_cnt,
                  SUM(CASE WHEN status = 'submitted'              THEN 1 ELSE 0 END) as sub_cnt,
                  SUM(CASE WHEN status IN ('active','revision')   THEN 1 ELSE 0 END) as active_cnt
           FROM tasks WHERE group_id=? AND thread_id=? AND assigned_username IS NOT NULL
           GROUP BY assigned_username ORDER BY done_cnt DESC''',
        (chat.id, tid)
    ).fetchall()
    conn.close()

    msg = "📈 *KPI*\n\n"
    if goals:
        msg += "🎯 *Цели клиента:*\n"
        for g in goals:
            msg += f"  • {g['metric']}: {g['value']}\n"
        msg += f"\n_Установил: @{goals[0]['set_by']}_\n\n"
    else:
        msg += "🎯 *Цели не установлены*\n_Директор: `/goal Постов 12`_\n\n"

    if stats:
        msg += "👥 *Команда:*\n\n"
        for s in stats:
            total  = s['total']
            done   = s['done_cnt']   or 0
            sub    = s['sub_cnt']    or 0
            active = s['active_cnt'] or 0
            pct    = int(done / total * 100) if total else 0
            bar    = "🟢" if pct >= 80 else "🟡" if pct >= 50 else "🔴"
            msg   += f"{bar} *@{s['assigned_username']}*\n"
            msg   += f"   ✅ {done} · 📨 {sub} · 🔄 {active} · Всего: {total} ({pct}%)\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_keyboard())

async def set_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!")
        return
    by = get_uname(update)
    conn = get_db()
    member = conn.execute(
        'SELECT role FROM members WHERE group_id=? AND username=?', (chat.id, by)
    ).fetchone()
    if not member or member['role'] != 'director':
        conn.close()
        await update.message.reply_text("⛔ Только директор может устанавливать цели KPI")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Формат: `/goal Постов 12`", parse_mode='Markdown'
        )
        conn.close()
        return
    metric = context.args[0]
    value  = ' '.join(context.args[1:])
    tid    = get_thread(update)
    old    = conn.execute(
        'SELECT value FROM kpi_goals WHERE group_id=? AND thread_id=? AND metric=?',
        (chat.id, tid, metric)
    ).fetchone()
    old_val = old['value'] if old else None
    conn.execute(
        '''INSERT INTO kpi_goals (group_id, thread_id, metric, value, set_by)
           VALUES (?,?,?,?,?)
           ON CONFLICT(group_id, thread_id, metric)
           DO UPDATE SET value=excluded.value, set_by=excluded.set_by, set_at=datetime('now')''',
        (chat.id, tid, metric, value, by)
    )
    conn.commit()
    conn.close()
    log_change(chat.id, tid, 'kpi_goal', 0, metric, old_val, value, by)
    await update.message.reply_text(
        f"✅ Цель: *{metric}* → {value}", parse_mode='Markdown'
    )


# ── History ───────────────────────────────────────────────────────

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!")
        return
    tid = get_thread(update)
    conn = get_db()
    rows = conn.execute(
        '''SELECT * FROM change_log WHERE group_id=? AND thread_id=?
           ORDER BY changed_at DESC LIMIT 20''',
        (chat.id, tid)
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📜 История пуста", reply_markup=main_keyboard())
        return
    ETYPE = {
        'task':         '📌 Задача',
        'content_plan': '📅 Контент',
        'brief':        '📁 Бриф',
        'kpi_goal':     '🎯 KPI',
    }
    msg = "📜 *История изменений*\n\n"
    for r in rows:
        entity = ETYPE.get(r['entity_type'], r['entity_type'])
        when   = r['changed_at'][:16].replace('T', ' ')
        who    = r['changed_by'] or '?'
        if r['field_name'] == 'создана':
            msg += f"➕ {entity}: {r['new_value']}\n"
        else:
            msg += f"✏️ {entity} — {r['field_name']}\n"
            msg += f"   _{r['old_value']}_ → *{r['new_value']}*\n"
        msg += f"   👤 @{who} · {when}\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_keyboard())


# ── Cancel (global) & text handler ───────────────────────────────

async def cancel_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for k in ('brief_field', 'brief_gid', 'brief_tid'):
        context.user_data.pop(k, None)
    await update.message.reply_text("❌ Отменено", reply_markup=main_keyboard())

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('brief_field'):
        await brief_value_received(update, context)
        return
    text = update.message.text
    if   text == "📋 Список":  await list_tasks(update, context)
    elif text == "📅 Контент": await content_plan(update, context)
    elif text == "💡 Идеи":    await ideas_list(update, context)
    elif text == "📁 Проект":  await project_brief(update, context)
    elif text == "📈 KPI":     await kpi(update, context)


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
            T_TYPE: [
                CallbackQueryHandler(t_type_chosen, pattern="^tt_"),
            ],
            T_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, t_desc_received),
            ],
            T_DATE: [
                CallbackQueryHandler(t_date_chosen, pattern="^tcday_"),
                CallbackQueryHandler(t_cal_nav,     pattern="^tc(prev|next)_"),
                CallbackQueryHandler(t_cal_noop,    pattern="^tcnoop$"),
            ],
            T_ASSIGNEE: [
                CallbackQueryHandler(t_assignee_cb,   pattern="^asgn_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, t_assignee_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    cp_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cp_add_start, pattern="^cp_add$"),
        ],
        states={
            CP_DATE: [
                CallbackQueryHandler(cp_date_chosen, pattern="^cpday_"),
                CallbackQueryHandler(cp_cal_nav,     pattern="^cp(prev|next)_"),
                CallbackQueryHandler(cp_cal_noop,    pattern="^cpnoop$"),
            ],
            CP_TYPE_S: [
                CallbackQueryHandler(cp_type_chosen, pattern="^cpt_"),
            ],
            CP_DESC_S: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cp_desc_received),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("join",    join))
    app.add_handler(CommandHandler("my",      my_tasks))
    app.add_handler(CommandHandler("idea",    add_idea))
    app.add_handler(CommandHandler("goal",    set_goal))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("cancel",  cancel_global))
    app.add_handler(task_conv)
    app.add_handler(cp_conv)
    app.add_handler(CallbackQueryHandler(role_chosen,      pattern="^role_"))
    app.add_handler(CallbackQueryHandler(status_change_cb, pattern=r"^st_\d+_"))
    app.add_handler(CallbackQueryHandler(brief_field_chosen, pattern="^br_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("✅ WhyNot бот v3 запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
