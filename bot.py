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
T_TYPE, T_DESC, T_REFS, T_DATE, T_ASSIGNEE = range(5)
CP_TYPE_S, CP_DESC_S, CP_REFS_S, CP_DATE = range(5, 9)
IDEA_TEXT, IDEA_PRIORITY = range(9, 11)
KPI_METRIC, KPI_VALUE = range(11, 13)

# ── Constants ───────────────────────────────────────────────────
TASK_TYPES = {
    'shoot':   ('🎬', 'Съемка'),
    'publish': ('📢', 'Публикация'),
    'design':  ('🎨', 'Дизайн'),
    'edit':    ('✂️', 'Монтаж'),
    'other':   ('📌', 'Другое'),
}
STATUSES = {
    'active':      '⏳ Ожидает',
    'in_progress': '▶️ В процессе',
    'submitted':   '📨 На проверке',
    'revision':    '🔁 На доработку',
    'approved':    '✅ Принято',
    'published':   '🚀 Опубликовано',
}
STATUS_DOT = {
    'active':      '⚪',
    'in_progress': '🔵',
    'submitted':   '🟡',
    'revision':    '🔴',
    'approved':    '🟢',
    'published':   '✅',
}
ROLES = {'director': '👑 Директор', 'am': '📋 АМ', 'executor': '✂️ Исполнитель'}
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
CP_TYPE_MAP = {
    'cpt_post':      '📸 Пост',
    'cpt_story':     '📱 Сторис',
    'cpt_reels':     '🎬 Рилс',
    'cpt_highlight': '🎯 Актуальное',
}
KPI_PRESETS = ['Постов', 'Сторис', 'Рилс', 'Охваты', 'Подписчики', 'ER %']
KPI_VALUES  = ['4', '8', '10', '12', '15', '20', '30', '50']
MONTH_RU    = ['', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
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
            brand_info       TEXT, target_audience TEXT, tone_of_voice TEXT,
            main_offers      TEXT, visual_style     TEXT, links         TEXT,
            competitors      TEXT, visual_refs      TEXT,
            updated_by TEXT, updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(group_id, thread_id)
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id          INTEGER NOT NULL,
            thread_id         INTEGER DEFAULT 0,
            task_type         TEXT    DEFAULT 'other',
            description       TEXT    NOT NULL,
            refs              TEXT,
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
            content_type TEXT,
            description  TEXT    NOT NULL,
            refs         TEXT,
            plan_date    TEXT,
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
            entity_type TEXT, entity_id INTEGER,
            field_name  TEXT, old_value TEXT, new_value TEXT,
            changed_by  TEXT,
            changed_at  TEXT    DEFAULT (datetime('now'))
        );
    ''')
    conn.commit()
    for sql in [
        'ALTER TABLE content_plan ADD COLUMN refs TEXT',
        'ALTER TABLE tasks ADD COLUMN refs TEXT',
    ]:
        try: conn.execute(sql); conn.commit()
        except: pass
    conn.close()


# ── Helpers ─────────────────────────────────────────────────────

def get_thread(update: Update) -> int:
    msg = update.effective_message
    return (msg.message_thread_id or 0) if msg else 0

def get_uname(update: Update) -> str:
    u = update.effective_user
    return (u.username or str(u.id)) if u else 'unknown'

def log_change(gid, tid, etype, eid, field, old, new, by):
    conn = get_db()
    conn.execute(
        '''INSERT INTO change_log
           (group_id,thread_id,entity_type,entity_id,field_name,old_value,new_value,changed_by)
           VALUES (?,?,?,?,?,?,?,?)''',
        (gid, tid, etype, eid, field,
         str(old) if old is not None else '—',
         str(new) if new is not None else '—', by)
    )
    conn.commit(); conn.close()

def is_manager(conn, group_id: int, username: str) -> bool:
    m = conn.execute(
        'SELECT role FROM members WHERE group_id=? AND username=?', (group_id, username)
    ).fetchone()
    return bool(m and m['role'] in ('director', 'am'))

def is_url(text: str) -> bool:
    return bool(text and (text.startswith('http://') or text.startswith('https://')))

def format_refs(refs: str) -> str:
    if not refs:
        return ''
    if refs.startswith('photo:'):
        return ''
    if is_url(refs):
        return f'\n🔗 [Референс]({refs})'
    return f'\n🔗 {refs}'

def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("➕ Задача"),   KeyboardButton("💡 Идеи")],
        [KeyboardButton("📅 Контент"), KeyboardButton("📈 KPI")],
        [KeyboardButton("📋 Список"),  KeyboardButton("📁 Проект")],
    ], resize_keyboard=True)

def build_calendar(year: int, month: int, prefix: str,
                   show_skip: bool = False) -> InlineKeyboardMarkup:
    pm = month - 1 if month > 1 else 12
    py = year if month > 1 else year - 1
    nm = month + 1 if month < 12 else 1
    ny = year if month < 12 else year + 1
    rows = [[
        InlineKeyboardButton("◀️", callback_data=f"{prefix}prev_{py}_{pm:02d}"),
        InlineKeyboardButton(f"{MONTH_RU[month]} {year}", callback_data=f"{prefix}noop"),
        InlineKeyboardButton("▶️", callback_data=f"{prefix}next_{ny}_{nm:02d}"),
    ], [
        InlineKeyboardButton(d, callback_data=f"{prefix}noop")
        for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    ]]
    for week in cal_lib.monthcalendar(year, month):
        rows.append([
            InlineKeyboardButton(str(d) if d else "·",
                                 callback_data=f"{prefix}day_{year}_{month:02d}_{d:02d}"
                                 if d else f"{prefix}noop")
            for d in week
        ])
    if show_skip:
        rows.append([
            InlineKeyboardButton("⏭ Без даты", callback_data=f"{prefix}skip"),
            InlineKeyboardButton("❌ Отмена",   callback_data="conv_cancel"),
        ])
    return InlineKeyboardMarkup(rows)

def multi_assignee_keyboard(group_id: int, selected: set) -> InlineKeyboardMarkup | None:
    conn = get_db()
    members = conn.execute(
        'SELECT username, full_name, role FROM members WHERE group_id=? ORDER BY full_name',
        (group_id,)
    ).fetchall()
    conn.close()
    if not members:
        return None
    re_map = {'director': '👑', 'am': '📋', 'executor': '✂️'}
    buttons = []
    for m in members:
        check = "✅" if m['username'] in selected else "☐"
        label = f"{check} {re_map.get(m['role'],'')} {m['full_name'] or m['username']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"mtoggle_{m['username']}")])
    count = len(selected)
    confirm = f"✅ Назначить ({count})" if count else "✅ Без исполнителя"
    buttons.append([InlineKeyboardButton(confirm, callback_data="mconfirm")])
    return InlineKeyboardMarkup(buttons)

def task_type_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Съемка",     callback_data="tt_shoot"),
         InlineKeyboardButton("📢 Публикация", callback_data="tt_publish")],
        [InlineKeyboardButton("🎨 Дизайн",     callback_data="tt_design"),
         InlineKeyboardButton("✂️ Монтаж",     callback_data="tt_edit")],
        [InlineKeyboardButton("📌 Другое",     callback_data="tt_other")],
    ])

def cp_type_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Пост",       callback_data="cpt_post"),
         InlineKeyboardButton("📱 Сторис",     callback_data="cpt_story")],
        [InlineKeyboardButton("🎬 Рилс",       callback_data="cpt_reels"),
         InlineKeyboardButton("🎯 Актуальное", callback_data="cpt_highlight")],
    ])

def task_action_kb(task_id: int, status: str):
    """Кнопки управления для отдельной карточки задачи."""
    if status == 'active':
        rows = [[
            InlineKeyboardButton("▶️ Начать",       callback_data=f"st_{task_id}_in_progress"),
            InlineKeyboardButton("📨 На одобрение", callback_data=f"st_{task_id}_submitted"),
        ]]
    elif status == 'in_progress':
        rows = [[InlineKeyboardButton("📨 Отправить на одобрение",
                                      callback_data=f"st_{task_id}_submitted")]]
    elif status == 'submitted':
        rows = [[
            InlineKeyboardButton("✅ Принять",       callback_data=f"st_{task_id}_approved"),
            InlineKeyboardButton("🔁 На доработку", callback_data=f"st_{task_id}_revision"),
        ]]
    elif status == 'revision':
        rows = [[
            InlineKeyboardButton("▶️ Возобновить", callback_data=f"st_{task_id}_in_progress"),
            InlineKeyboardButton("📨 Сдать снова",  callback_data=f"st_{task_id}_submitted"),
        ]]
    elif status == 'approved':
        rows = [[InlineKeyboardButton("🚀 Опубликовано",
                                      callback_data=f"st_{task_id}_published")]]
    else:
        return None
    return InlineKeyboardMarkup(rows)

def _do_status_update(task_id: int, new_status: str, by: str):
    """Обновляет статус задачи в БД и пишет в лог."""
    conn = get_db()
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not task:
        conn.close(); return
    old_status = task['status']
    done_at = datetime.now().isoformat() if new_status in ('approved', 'published') else None
    conn.execute('UPDATE tasks SET status=?,done_at=? WHERE id=?', (new_status, done_at, task_id))
    conn.commit(); conn.close()
    log_change(task['group_id'], task['thread_id'], 'task', task_id, 'статус',
               STATUSES.get(old_status), STATUSES.get(new_status, new_status), by)


# ── /start & /join ───────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ['group', 'supergroup']:
        await update.message.reply_text(
            f"✅ *{chat.title}*\n\nЗарегистрируйся: /join\nИспользуй кнопки 👇",
            parse_mode='Markdown', reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text(
            "👋 Добавь меня в группу и напиши /start\n/my — мои задачи"
        )

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat; user = update.effective_user
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах"); return
    username = user.username or str(user.id)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👑 Директор",    callback_data=f"role_director_{username}"),
        InlineKeyboardButton("📋 АМ",          callback_data=f"role_am_{username}"),
        InlineKeyboardButton("✂️ Исполнитель", callback_data=f"role_executor_{username}"),
    ]])
    await update.message.reply_text(
        f"👋 *{user.full_name or username}*, выбери роль:",
        parse_mode='Markdown', reply_markup=kb
    )

async def role_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, role, username = q.data.split("_", 2)
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO members (group_id,username,full_name,role) VALUES (?,?,?,?)',
        (q.message.chat.id, username, q.from_user.full_name or username, role)
    )
    conn.commit(); conn.close()
    await q.edit_message_text(f"✅ @{username} — {ROLES.get(role, role)}")


# ── /my ─────────────────────────────────────────────────────────

async def my_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = get_uname(update)
    conn = get_db()
    rows = conn.execute(
        '''SELECT * FROM tasks WHERE assigned_username LIKE ?
           AND status NOT IN ('approved','published')
           ORDER BY task_date NULLS LAST, created_at''',
        (f'%{username}%',)
    ).fetchall()
    conn.close()
    rows = [t for t in rows if username in (t['assigned_username'] or '').split(',')]
    if not rows:
        await update.message.reply_text("🎉 Нет активных задач!"); return
    msg = f"📋 *Мои задачи — @{username}*\n\n"
    for t in rows:
        emoji = TASK_TYPES.get(t['task_type'], ('📌',))[0]
        dot   = STATUS_DOT.get(t['status'], '⚪')
        msg  += f"{dot} {emoji} *#{t['id']}* {t['description']}\n"
        msg  += f"   {STATUSES.get(t['status'], t['status'])}"
        if t['task_date']: msg += f" · 📅 {t['task_date']}"
        msg  += "\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')


# ── Task creation ────────────────────────────────────────────────

async def new_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!"); return ConversationHandler.END
    context.user_data.clear()
    context.user_data['t_gid'] = update.effective_chat.id
    context.user_data['t_tid'] = get_thread(update)
    await update.message.reply_text(
        "➕ *Новая задача*\n\nВыбери тип:",
        parse_mode='Markdown', reply_markup=task_type_keyboard()
    )
    return T_TYPE

async def t_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ttype = q.data.replace("tt_", "")
    context.user_data['t_type'] = ttype
    emoji, name = TASK_TYPES[ttype]
    await q.edit_message_text(
        f"{emoji} *{name}*\n\nОпиши задачу:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="conv_cancel")
        ]])
    )
    return T_DESC

async def t_desc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_desc'] = update.message.text
    gid = context.user_data['t_gid']
    tid = context.user_data['t_tid']
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Без референса", callback_data="t_refs_skip"),
        InlineKeyboardButton("❌ Отмена",        callback_data="conv_cancel"),
    ]])
    await context.bot.send_message(
        chat_id=gid,
        message_thread_id=tid if tid else None,
        text="🖼 *Референс?*\n\nПришли фото или вставь ссылку:",
        parse_mode='Markdown',
        reply_markup=kb
    )
    return T_REFS

async def t_refs_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_refs'] = update.message.text
    return await _t_ask_date(update, context)

async def t_refs_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = update.message.photo[-1].file_id
    context.user_data['t_refs'] = f"photo:{file_id}"
    gid = context.user_data['t_gid']
    tid = context.user_data['t_tid']
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="🖼 Фото сохранено"
    )
    return await _t_ask_date(update, context)

async def t_refs_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пропустить референс — через /skip (legacy) или inline-кнопку."""
    context.user_data['t_refs'] = None
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("🖼 Без референса")
    return await _t_ask_date(update, context)

async def _t_ask_date(update, context):
    gid = context.user_data['t_gid']
    tid = context.user_data['t_tid']
    today = date.today()
    label = "📅 Дата съёмки:" if context.user_data.get('t_type') == 'shoot' else "📅 Дедлайн:"
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text=label,
        reply_markup=build_calendar(today.year, today.month, prefix="tc", show_skip=True)
    )
    return T_DATE

async def t_date_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Без даты — inline-кнопка в календаре."""
    q = update.callback_query; await q.answer()
    context.user_data['t_date'] = None
    await q.edit_message_text("📅 Без даты")
    gid = context.user_data['t_gid']
    tid = context.user_data['t_tid']
    context.user_data['m_selected'] = set()
    kb = multi_assignee_keyboard(gid, set())
    if kb:
        await context.bot.send_message(
            chat_id=gid, message_thread_id=tid if tid else None,
            text="👤 Выбери исполнителей:", reply_markup=kb
        )
    else:
        await context.bot.send_message(
            chat_id=gid, message_thread_id=tid if tid else None,
            text="👤 Напиши @username или выбери кнопкой:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Без исполнителя", callback_data="mconfirm"),
                InlineKeyboardButton("❌ Отмена",          callback_data="conv_cancel"),
            ]])
        )
    return T_ASSIGNEE

async def t_cal_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    raw = q.data.replace("tcprev_", "").replace("tcnext_", "")
    y, m = raw.split("_")
    await q.edit_message_reply_markup(reply_markup=build_calendar(int(y), int(m), prefix="tc"))
    return T_DATE

async def t_cal_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(); return T_DATE

async def t_date_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, y, m, d = q.data.split("_")
    context.user_data['t_date'] = f"{d}.{m}.{y}"
    await q.edit_message_text(f"📅 {d}.{m}.{y}")
    gid = context.user_data['t_gid']
    tid = context.user_data['t_tid']
    context.user_data['m_selected'] = set()
    kb = multi_assignee_keyboard(gid, set())
    if kb:
        await context.bot.send_message(
            chat_id=gid, message_thread_id=tid if tid else None,
            text="👤 Выбери исполнителей:", reply_markup=kb
        )
    else:
        await context.bot.send_message(
            chat_id=gid, message_thread_id=tid if tid else None,
            text="👤 Напиши @username _(или /skip)_", parse_mode='Markdown'
        )
    return T_ASSIGNEE

async def t_toggle_assignee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    username = q.data.replace("mtoggle_", "")
    selected = context.user_data.setdefault('m_selected', set())
    if username in selected:
        selected.discard(username)
    else:
        selected.add(username)
    gid = context.user_data.get('t_gid')
    await q.edit_message_reply_markup(reply_markup=multi_assignee_keyboard(gid, selected))
    return T_ASSIGNEE

async def t_confirm_assignees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    selected = context.user_data.get('m_selected', set())
    context.user_data['t_assignee'] = ','.join(sorted(selected)) if selected else None
    names = ' '.join(f"@{u}" for u in sorted(selected)) if selected else "Без исполнителя"
    await q.edit_message_text(f"👤 {names}")
    return await _save_task(update, context)

async def t_assignee_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_assignee'] = update.message.text.strip().lstrip('@')
    return await _save_task(update, context)

async def _save_task(update, context):
    gid       = context.user_data.get('t_gid', 0)
    tid       = context.user_data.get('t_tid', 0)
    ttype     = context.user_data.get('t_type', 'other')
    desc      = context.user_data.get('t_desc', '—')
    refs      = context.user_data.get('t_refs')
    task_date = context.user_data.get('t_date')
    assignee  = context.user_data.get('t_assignee')
    by        = get_uname(update)
    conn = get_db()
    cur = conn.execute(
        '''INSERT INTO tasks
           (group_id,thread_id,task_type,description,refs,task_date,assigned_username,created_by)
           VALUES (?,?,?,?,?,?,?,?)''',
        (gid, tid, ttype, desc, refs, task_date, assignee, by)
    )
    task_id = cur.lastrowid; conn.commit(); conn.close()
    log_change(gid, tid, 'task', task_id, 'создана', None,
               f"{TASK_TYPES.get(ttype,('',''))[1]}: {desc}", by)
    emoji, tname = TASK_TYPES.get(ttype, ('📌', 'Задача'))
    msg = f"✅ *Задача #{task_id} создана*\n\n{emoji} {tname}: {desc}\n"
    if assignee:
        names = ' '.join(f"@{u}" for u in assignee.split(','))
        msg += f"👤 {names}\n"
    if task_date: msg += f"📅 {task_date}\n"
    if refs and not refs.startswith("photo:"):
        msg += format_refs(refs)
    context.user_data.clear()
    await context.bot.send_message(
        chat_id=gid,
        message_thread_id=tid if tid else None,
        text=msg,
        parse_mode='Markdown',
        reply_markup=task_action_kb(task_id, 'active'),
        disable_web_page_preview=True
    )
    if refs and refs.startswith("photo:"):
        await context.bot.send_photo(
            chat_id=gid,
            message_thread_id=tid if tid else None,
            photo=refs.replace("photo:", ""),
            caption="🖼 Референс к задаче"
        )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено", reply_markup=main_keyboard())
    return ConversationHandler.END

async def cancel_conv_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена через inline-кнопку ❌ Отмена — работает из любого шага."""
    q = update.callback_query; await q.answer("Отменено")
    await q.edit_message_text("❌ Отменено")
    context.user_data.clear()
    await context.bot.send_message(
        chat_id=q.message.chat_id,
        message_thread_id=q.message.message_thread_id or None,
        text="Главное меню 👇",
        reply_markup=main_keyboard()
    )
    return ConversationHandler.END


# ── Status change (standalone task cards) ────────────────────────

async def status_change_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.split("_", 2)
    task_id = int(parts[1]); new_status = parts[2]
    by = q.from_user.username or str(q.from_user.id)
    _do_status_update(task_id, new_status, by)
    conn = get_db()
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    conn.close()
    if not task: return
    emoji = TASK_TYPES.get(task['task_type'], ('📌',))[0]
    dot   = STATUS_DOT.get(new_status, '⚪')
    text  = f"{dot} {emoji} *#{task_id} {task['description']}*\n{STATUSES.get(new_status, new_status)}"
    if task['assigned_username']:
        names = ' '.join(f"@{u}" for u in task['assigned_username'].split(','))
        text += f"\n👤 {names}"
    if task['task_date']: text += f"\n📅 {task['task_date']}"
    await q.edit_message_text(text, parse_mode='Markdown',
                              reply_markup=task_action_kb(task_id, new_status))


# ── Task list ────────────────────────────────────────────────────

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!"); return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📥 Мне назначены", callback_data="tlist_mine"),
        InlineKeyboardButton("📤 Я назначил",    callback_data="tlist_given"),
    ]])
    await update.message.reply_text("📋 *Задачи*\n\nЧто показать?",
                                    parse_mode='Markdown', reply_markup=kb)

def _build_mine_view(chat_id: int, thread_id: int, username: str):
    conn = get_db()
    active = conn.execute(
        '''SELECT * FROM tasks WHERE group_id=? AND thread_id=?
           AND assigned_username LIKE ? AND status NOT IN ('approved','published')
           ORDER BY CASE status WHEN 'revision' THEN 1 WHEN 'submitted' THEN 2
                                WHEN 'in_progress' THEN 3 WHEN 'active' THEN 4 ELSE 5 END,
                    task_date NULLS LAST LIMIT 20''',
        (chat_id, thread_id, f'%{username}%')
    ).fetchall()
    done = conn.execute(
        '''SELECT * FROM tasks WHERE group_id=? AND thread_id=?
           AND assigned_username LIKE ? AND status IN ('approved','published')
           ORDER BY done_at DESC LIMIT 5''',
        (chat_id, thread_id, f'%{username}%')
    ).fetchall()
    conn.close()
    active = [t for t in active if username in (t['assigned_username'] or '').split(',')]
    done   = [t for t in done   if username in (t['assigned_username'] or '').split(',')]

    msg = "📥 *Мне назначены*\n\n"
    buttons = []

    if not active and not done:
        msg += "🎉 Задач нет!"
    else:
        for t in active:
            emoji  = TASK_TYPES.get(t['task_type'], ('📌',))[0]
            dot    = STATUS_DOT.get(t['status'], '⚪')
            date_s = f" · 📅 {t['task_date']}" if t['task_date'] else ""
            msg += f"{dot} {emoji} *#{t['id']}* {t['description'][:50]}{date_s}\n"
            msg += f"   {STATUSES.get(t['status'], t['status'])}\n\n"
            row = []
            if t['status'] == 'active':
                row = [
                    InlineKeyboardButton("▶️ Начать",       callback_data=f"lmst_{t['id']}_in_progress"),
                    InlineKeyboardButton("📨 На одобрение", callback_data=f"lmst_{t['id']}_submitted"),
                ]
            elif t['status'] == 'in_progress':
                row = [InlineKeyboardButton("📨 Отправить на одобрение",
                                            callback_data=f"lmst_{t['id']}_submitted")]
            elif t['status'] == 'revision':
                row = [
                    InlineKeyboardButton("▶️ Возобновить", callback_data=f"lmst_{t['id']}_in_progress"),
                    InlineKeyboardButton("📨 Сдать снова",  callback_data=f"lmst_{t['id']}_submitted"),
                ]
            elif t['status'] == 'approved':
                row = [InlineKeyboardButton("🚀 Опубликовать",
                                            callback_data=f"lmst_{t['id']}_published")]
            # submitted — ждём ответа, без кнопок
            if row:
                buttons.append(row)

        if done:
            msg += "─────────────────\n✅ *Выполненные:*\n"
            for t in done:
                emoji = TASK_TYPES.get(t['task_type'], ('📌',))[0]
                msg += f"✅ {emoji} #{t['id']} {t['description'][:45]}\n"
            msg += "\n"

    buttons.append([InlineKeyboardButton("📤 Я назначил →", callback_data="tlist_given")])
    return msg, InlineKeyboardMarkup(buttons)

def _build_given_view(chat_id: int, thread_id: int, username: str):
    conn = get_db()
    active = conn.execute(
        '''SELECT * FROM tasks WHERE group_id=? AND thread_id=? AND created_by=?
           AND status NOT IN ('approved','published')
           ORDER BY CASE status WHEN 'submitted' THEN 1 WHEN 'revision' THEN 2
                                WHEN 'in_progress' THEN 3 WHEN 'active' THEN 4 ELSE 5 END,
                    task_date NULLS LAST LIMIT 20''',
        (chat_id, thread_id, username)
    ).fetchall()
    done = conn.execute(
        '''SELECT * FROM tasks WHERE group_id=? AND thread_id=? AND created_by=?
           AND status IN ('approved','published')
           ORDER BY done_at DESC LIMIT 5''',
        (chat_id, thread_id, username)
    ).fetchall()
    conn.close()

    msg = "📤 *Я назначил*\n\n"
    buttons = []

    if not active and not done:
        msg += "Нет назначенных задач."
    else:
        for t in active:
            emoji    = TASK_TYPES.get(t['task_type'], ('📌',))[0]
            dot      = STATUS_DOT.get(t['status'], '⚪')
            assignee = ' '.join(f"@{u}" for u in (t['assigned_username'] or '').split(',') if u)
            date_s   = f" · 📅 {t['task_date']}" if t['task_date'] else ""
            msg += f"{dot} {emoji} *#{t['id']}* {t['description'][:45]}\n"
            msg += f"   {assignee}{date_s} · {STATUSES.get(t['status'], t['status'])}\n\n"
            row = []
            if t['status'] == 'submitted':
                row = [
                    InlineKeyboardButton(f"✅ Принять #{t['id']}",   callback_data=f"lgst_{t['id']}_approved"),
                    InlineKeyboardButton(f"🔁 Доработка #{t['id']}", callback_data=f"lgst_{t['id']}_revision"),
                ]
            elif t['status'] == 'approved':
                row = [InlineKeyboardButton(f"🚀 Опубл. #{t['id']}",
                                            callback_data=f"lgst_{t['id']}_published")]
            if row:
                buttons.append(row)

        if done:
            msg += "─────────────────\n✅ *Выполненные:*\n"
            for t in done:
                emoji = TASK_TYPES.get(t['task_type'], ('📌',))[0]
                msg += f"✅ {emoji} #{t['id']} {t['description'][:45]}\n"
            msg += "\n"

    buttons.append([InlineKeyboardButton("← 📥 Мне назначены", callback_data="tlist_mine")])
    return msg, InlineKeyboardMarkup(buttons)

async def tlist_mine_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    username = q.from_user.username or str(q.from_user.id)
    tid = q.message.message_thread_id or 0
    msg, kb = _build_mine_view(q.message.chat.id, tid, username)
    await q.edit_message_text(msg, parse_mode='Markdown', reply_markup=kb)

async def tlist_given_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    username = q.from_user.username or str(q.from_user.id)
    tid = q.message.message_thread_id or 0
    msg, kb = _build_given_view(q.message.chat.id, tid, username)
    await q.edit_message_text(msg, parse_mode='Markdown', reply_markup=kb)

async def lmine_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Смена статуса из списка 'Мне назначены' — обновляет список."""
    q = update.callback_query; await q.answer()
    parts = q.data.split("_", 2)
    task_id = int(parts[1]); new_status = parts[2]
    by = q.from_user.username or str(q.from_user.id)
    _do_status_update(task_id, new_status, by)
    username = q.from_user.username or str(q.from_user.id)
    tid = q.message.message_thread_id or 0
    msg, kb = _build_mine_view(q.message.chat.id, tid, username)
    await q.edit_message_text(msg, parse_mode='Markdown', reply_markup=kb)

async def lgiven_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Смена статуса из списка 'Я назначил' — обновляет список."""
    q = update.callback_query; await q.answer()
    parts = q.data.split("_", 2)
    task_id = int(parts[1]); new_status = parts[2]
    by = q.from_user.username or str(q.from_user.id)
    _do_status_update(task_id, new_status, by)
    username = q.from_user.username or str(q.from_user.id)
    tid = q.message.message_thread_id or 0
    msg, kb = _build_given_view(q.message.chat.id, tid, username)
    await q.edit_message_text(msg, parse_mode='Markdown', reply_markup=kb)


# ── Content plan ─────────────────────────────────────────────────

async def content_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!"); return
    tid = get_thread(update); username = get_uname(update)
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM content_plan WHERE group_id=? AND thread_id=? ORDER BY plan_date, id LIMIT 30',
        (chat.id, tid)
    ).fetchall()
    mgr = is_manager(conn, chat.id, username); conn.close()
    DOT = {'planned': '⚪', 'in_progress': '🟡', 'done': '🟢', 'published': '✅'}
    if not rows:
        msg = "📅 *Контент-план*\n\nПока пусто."
    else:
        msg = "📅 *Контент-план*\n\n"
        for r in rows:
            dot = DOT.get(r['status'], '⚪')
            ref_icon = (" 🖼" if r['refs'] and r['refs'].startswith("photo:") else
                        " 🔗" if r['refs'] else "")
            msg += f"{dot} *#{r['id']}* {r['content_type']} — {r['plan_date'] or '—'}\n"
            msg += f"   {r['description']}{ref_icon}\n\n"
    bottom = []
    if mgr:
        bottom = [InlineKeyboardButton("➕ Добавить", callback_data="cp_add"),
                  InlineKeyboardButton("⚙️ Управление", callback_data="cp_manage_0")]
    else:
        msg += "_Редактирование: АМ и Директор_"
    kb = InlineKeyboardMarkup([bottom]) if bottom else None
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=kb)

async def cp_manage_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    offset = int(q.data.replace("cp_manage_", ""))
    chat   = q.message.chat; username = q.from_user.username or str(q.from_user.id)
    conn   = get_db()
    if not is_manager(conn, chat.id, username):
        conn.close(); await q.answer("⛔ Только АМ и Директор", show_alert=True); return
    tid = q.message.message_thread_id or 0
    rows  = conn.execute(
        'SELECT * FROM content_plan WHERE group_id=? AND thread_id=? ORDER BY plan_date, id LIMIT 5 OFFSET ?',
        (chat.id, tid, offset)
    ).fetchall()
    total = conn.execute(
        'SELECT COUNT(*) FROM content_plan WHERE group_id=? AND thread_id=?', (chat.id, tid)
    ).fetchone()[0]
    conn.close()
    if not rows:
        await q.edit_message_text("Записей нет"); return
    DOT = {'planned': '⚪', 'in_progress': '🟡', 'done': '🟢', 'published': '✅'}
    msg = f"⚙️ *Управление* ({offset+1}–{min(offset+5, total)} из {total})\n\n"
    buttons = []
    for r in rows:
        dot   = DOT.get(r['status'], '⚪')
        label = f"{dot} #{r['id']} {r['content_type']} {r['plan_date'] or ''}"
        buttons.append([InlineKeyboardButton(label, callback_data="cp_noop")])
        buttons.append([
            InlineKeyboardButton("✏️ Изменить", callback_data=f"cpEdit_{r['id']}"),
            InlineKeyboardButton("📌 В задачи", callback_data=f"cpTask_{r['id']}"),
            InlineKeyboardButton("🗑 Удалить",  callback_data=f"cpDel_{r['id']}"),
        ])
    nav = []
    if offset > 0:          nav.append(InlineKeyboardButton("◀️", callback_data=f"cp_manage_{offset-5}"))
    if offset + 5 < total:  nav.append(InlineKeyboardButton("▶️", callback_data=f"cp_manage_{offset+5}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton("✖️ Закрыть", callback_data="cp_manage_close")])
    await q.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))

async def cp_manage_close_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("Нажми 📅 Контент чтобы открыть план")

async def cp_noop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ── Content plan creation conversation ───────────────────────────

async def cp_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data['cp_gid'] = q.message.chat.id
    context.user_data['cp_tid'] = q.message.message_thread_id or 0
    await context.bot.send_message(
        chat_id=q.message.chat.id,
        message_thread_id=q.message.message_thread_id or None,
        text="📅 *Новая запись*\n\nТип контента:",
        parse_mode='Markdown', reply_markup=cp_type_keyboard()
    )
    return CP_TYPE_S

async def cp_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ct = CP_TYPE_MAP.get(q.data, q.data)
    context.user_data['cp_type'] = ct
    await q.edit_message_text(f"Тип: {ct}")
    gid = context.user_data.get('cp_gid', q.message.chat.id)
    tid = context.user_data.get('cp_tid', 0)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="✏️ Что должно быть в публикации?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="conv_cancel")
        ]])
    )
    return CP_DESC_S

async def cp_desc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['cp_desc'] = update.message.text
    gid = context.user_data.get('cp_gid', update.effective_chat.id)
    tid = context.user_data.get('cp_tid', 0)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Без референса", callback_data="cp_refs_skip"),
        InlineKeyboardButton("❌ Отмена",        callback_data="conv_cancel"),
    ]])
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="🖼 *Референс?*\n\nПришли фото или вставь ссылку:",
        parse_mode='Markdown',
        reply_markup=kb
    )
    return CP_REFS_S

async def cp_refs_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['cp_refs'] = update.message.text
    return await _cp_ask_date(update, context)

async def cp_refs_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = update.message.photo[-1].file_id
    context.user_data['cp_refs'] = f"photo:{file_id}"
    gid = context.user_data.get('cp_gid', update.effective_chat.id)
    tid = context.user_data.get('cp_tid', 0)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="🖼 Фото сохранено"
    )
    return await _cp_ask_date(update, context)

async def cp_refs_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пропустить референс — через /skip или inline-кнопку."""
    context.user_data['cp_refs'] = None
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("🖼 Без референса")
    return await _cp_ask_date(update, context)

async def _cp_ask_date(update, context):
    gid = context.user_data.get('cp_gid', update.effective_chat.id)
    tid = context.user_data.get('cp_tid', 0)
    today = date.today()
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="📅 Дата публикации:",
        reply_markup=build_calendar(today.year, today.month, prefix="cp", show_skip=True)
    )
    return CP_DATE

async def cp_date_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Без даты — inline-кнопка в календаре."""
    q = update.callback_query; await q.answer()
    context.user_data['cp_date'] = None
    await q.edit_message_text("📅 Без даты")
    return await _save_cp(update, context)

async def cp_cal_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    raw = q.data.replace("cpprev_", "").replace("cpnext_", "")
    y, m = raw.split("_")
    await q.edit_message_reply_markup(reply_markup=build_calendar(int(y), int(m), prefix="cp"))
    return CP_DATE

async def cp_cal_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(); return CP_DATE

async def cp_date_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, y, m, d = q.data.split("_")
    date_str = f"{d}.{m}.{y}"
    context.user_data['cp_date'] = date_str
    await q.edit_message_text(f"📅 {date_str}")
    return await _save_cp(update, context)

async def _save_cp(update, context):
    gid  = context.user_data.get('cp_gid', 0); tid  = context.user_data.get('cp_tid', 0)
    ct   = context.user_data.get('cp_type', '—'); desc = context.user_data.get('cp_desc', '—')
    refs = context.user_data.get('cp_refs'); plan_date = context.user_data.get('cp_date')
    by   = get_uname(update)
    conn = get_db()
    cur  = conn.execute(
        'INSERT INTO content_plan (group_id,thread_id,content_type,description,refs,plan_date,created_by) VALUES (?,?,?,?,?,?,?)',
        (gid, tid, ct, desc, refs, plan_date, by)
    )
    cp_id = cur.lastrowid; conn.commit(); conn.close()
    log_change(gid, tid, 'content_plan', cp_id, 'создана', None, f"{ct} {plan_date}: {desc}", by)
    for k in ('cp_gid','cp_tid','cp_type','cp_desc','cp_refs','cp_date'):
        context.user_data.pop(k, None)
    msg = f"✅ *Добавлено в контент-план*\n\n{ct}"
    if plan_date: msg += f" — {plan_date}"
    msg += f"\n_{desc}_"
    if refs and not refs.startswith("photo:"):
        msg += format_refs(refs)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text=msg, parse_mode='Markdown', disable_web_page_preview=False
    )
    if refs and refs.startswith("photo:"):
        await context.bot.send_photo(
            chat_id=gid, message_thread_id=tid if tid else None,
            photo=refs.replace("photo:", ""), caption="🖼 Референс"
        )
    return ConversationHandler.END


# ── Content plan actions ─────────────────────────────────────────

async def cp_edit_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cp_id = int(q.data.replace("cpEdit_", ""))
    conn  = get_db(); r = conn.execute('SELECT * FROM content_plan WHERE id=?', (cp_id,)).fetchone(); conn.close()
    if not r: return
    msg = f"✏️ *#{cp_id}* {r['content_type']} — {r['plan_date'] or '—'}\n_{r['description']}_\n\nЧто изменить?"
    kb  = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Описание", callback_data=f"cpEf_{cp_id}_desc"),
         InlineKeyboardButton("🖼 Референс", callback_data=f"cpEf_{cp_id}_refs")],
        [InlineKeyboardButton("📅 Дату",    callback_data=f"cpEf_{cp_id}_date"),
         InlineKeyboardButton("🏷 Тип",     callback_data=f"cpEf_{cp_id}_type")],
    ])
    if r['refs'] and r['refs'].startswith("photo:"):
        await q.message.reply_photo(r['refs'].replace("photo:", ""), caption="🖼 Текущий референс")
    elif r['refs'] and is_url(r['refs']):
        await q.message.reply_text(f"🔗 Текущий референс: {r['refs']}")
    await q.message.reply_text(msg, parse_mode='Markdown', reply_markup=kb)

async def cp_edit_field_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.replace("cpEf_", "").split("_"); cp_id, field = int(parts[0]), parts[1]
    context.user_data.update({'cp_edit_id': cp_id, 'cp_edit_field': field,
                              'cp_edit_gid': q.message.chat.id,
                              'cp_edit_tid': q.message.message_thread_id or 0})
    if field == 'type':
        await q.message.reply_text("Новый тип:", reply_markup=cp_type_keyboard())
    elif field == 'refs':
        await q.message.reply_text("🖼 Пришли фото, вставь ссылку или напиши текст:")
    else:
        hints = {'desc': 'Новое описание:', 'date': 'Новая дата (ДД.ММ.ГГГГ):'}
        await q.message.reply_text(hints.get(field, 'Новое значение:'))

async def cp_edit_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('cp_edit_id'): return
    q = update.callback_query; await q.answer()
    ct = CP_TYPE_MAP.get(q.data, q.data); cp_id = context.user_data['cp_edit_id']
    gid = context.user_data.get('cp_edit_gid', q.message.chat.id)
    tid = context.user_data.get('cp_edit_tid', 0); by = q.from_user.username or str(q.from_user.id)
    conn = get_db()
    old = conn.execute('SELECT content_type FROM content_plan WHERE id=?', (cp_id,)).fetchone()
    conn.execute('UPDATE content_plan SET content_type=? WHERE id=?', (ct, cp_id))
    conn.commit(); conn.close()
    log_change(gid, tid, 'content_plan', cp_id, 'тип', old['content_type'] if old else '—', ct, by)
    for k in ('cp_edit_id','cp_edit_field','cp_edit_gid','cp_edit_tid'): context.user_data.pop(k, None)
    await q.edit_message_text(f"✅ Тип: {ct}")

async def cp_task_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cp_id = int(q.data.replace("cpTask_", ""))
    conn  = get_db(); r = conn.execute('SELECT * FROM content_plan WHERE id=?', (cp_id,)).fetchone()
    if not r: conn.close(); return
    members = conn.execute(
        'SELECT username, full_name, role FROM members WHERE group_id=? ORDER BY full_name',
        (r['group_id'],)
    ).fetchall(); conn.close()
    context.user_data['cptask_id'] = cp_id
    context.user_data['cptask_selected'] = set()
    re_map = {'director': '👑', 'am': '📋', 'executor': '✂️'}
    buttons = []
    for m in members:
        label = f"☐ {re_map.get(m['role'],'')} {m['full_name'] or m['username']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"cptoggle_{m['username']}")])
    buttons.append([InlineKeyboardButton("✅ Назначить", callback_data="cpconfirm")])
    buttons.append([InlineKeyboardButton("📌 Без исполнителя", callback_data="cpconfirm_none")])
    await q.message.reply_text(
        f"👤 Кому назначить?\n_{r['description']}_",
        parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons)
    )

async def cptask_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    username = q.data.replace("cptoggle_", "")
    selected = context.user_data.setdefault('cptask_selected', set())
    cp_id    = context.user_data.get('cptask_id')
    if username in selected: selected.discard(username)
    else: selected.add(username)
    conn = get_db()
    r = conn.execute('SELECT * FROM content_plan WHERE id=?', (cp_id,)).fetchone()
    members = conn.execute(
        'SELECT username, full_name, role FROM members WHERE group_id=? ORDER BY full_name',
        (r['group_id'],)
    ).fetchall(); conn.close()
    re_map = {'director': '👑', 'am': '📋', 'executor': '✂️'}
    buttons = []
    for m in members:
        check = "✅" if m['username'] in selected else "☐"
        label = f"{check} {re_map.get(m['role'],'')} {m['full_name'] or m['username']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"cptoggle_{m['username']}")])
    count   = len(selected)
    confirm = f"✅ Назначить ({count})" if count else "✅ Назначить"
    buttons.append([InlineKeyboardButton(confirm, callback_data="cpconfirm")])
    buttons.append([InlineKeyboardButton("📌 Без исполнителя", callback_data="cpconfirm_none")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))

async def cptask_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cp_id     = context.user_data.get('cptask_id')
    selected  = context.user_data.get('cptask_selected', set())
    none_mode = q.data == "cpconfirm_none"
    assignee  = None if none_mode else (','.join(sorted(selected)) if selected else None)
    by = q.from_user.username or str(q.from_user.id)
    conn = get_db(); r = conn.execute('SELECT * FROM content_plan WHERE id=?', (cp_id,)).fetchone()
    if not r: conn.close(); return
    cur = conn.execute(
        'INSERT INTO tasks (group_id,thread_id,task_type,description,task_date,assigned_username,created_by) VALUES (?,?,?,?,?,?,?)',
        (r['group_id'], r['thread_id'], 'publish', r['description'], r['plan_date'], assignee, by)
    )
    task_id = cur.lastrowid
    conn.execute('UPDATE content_plan SET status="in_progress" WHERE id=?', (cp_id,))
    conn.commit(); conn.close()
    log_change(r['group_id'], r['thread_id'], 'task', task_id, 'из контент-плана', None, r['description'], by)
    msg = f"✅ Задача *#{task_id}* создана!\n📢 _{r['description']}_"
    if assignee:
        names = ' '.join(f"@{u}" for u in assignee.split(','))
        msg += f"\n👤 {names}"
    if r['plan_date']: msg += f"\n📅 {r['plan_date']}"
    for k in ('cptask_id', 'cptask_selected'): context.user_data.pop(k, None)
    await q.edit_message_text(msg, parse_mode='Markdown',
                              reply_markup=task_action_kb(task_id, 'active'))

async def cp_del_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cp_id = int(q.data.replace("cpDel_", ""))
    conn  = get_db(); r = conn.execute('SELECT description FROM content_plan WHERE id=?', (cp_id,)).fetchone(); conn.close()
    desc  = r['description'][:30] if r else ''
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"cpDelOk_{cp_id}"),
        InlineKeyboardButton("❌ Отмена",      callback_data="cpDelCancel"),
    ]])
    await q.message.reply_text(f"Удалить запись #{cp_id}?\n_{desc}_",
                               parse_mode='Markdown', reply_markup=kb)

async def cp_del_ok_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cp_id = int(q.data.replace("cpDelOk_", "")); by = q.from_user.username or str(q.from_user.id)
    conn  = get_db(); r = conn.execute('SELECT * FROM content_plan WHERE id=?', (cp_id,)).fetchone()
    if r:
        conn.execute('DELETE FROM content_plan WHERE id=?', (cp_id,))
        conn.commit()
        log_change(r['group_id'], r['thread_id'], 'content_plan', cp_id, 'удалена',
                   f"{r['content_type']} {r['plan_date']}", None, by)
    conn.close(); await q.edit_message_text(f"🗑 Запись #{cp_id} удалена")

async def cp_del_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); await q.edit_message_text("❌ Отменено")


# ── Ideas ─────────────────────────────────────────────────────────

async def ideas_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!"); return
    tid = get_thread(update)
    conn = get_db()
    rows = conn.execute(
        '''SELECT * FROM ideas WHERE group_id=? AND thread_id=? AND converted=0
           ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                    created_at DESC LIMIT 10''', (chat.id, tid)
    ).fetchall(); conn.close()
    P   = {'high': '🔴', 'normal': '🟡', 'low': '⚪'}
    msg = "💡 *Идеи*\n\n"
    msg += "".join(f"{P.get(i['priority'],'🟡')} *#{i['id']}* {i['text']}\n" for i in rows) if rows else "Пока пусто.\n"
    kb  = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Добавить идею", callback_data="idea_add")]])
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=kb)

async def idea_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data['idea_gid'] = q.message.chat.id
    context.user_data['idea_tid'] = q.message.message_thread_id or 0
    await context.bot.send_message(
        chat_id=q.message.chat.id,
        message_thread_id=q.message.message_thread_id or None,
        text="💡 Напиши идею:"
    )
    return IDEA_TEXT

async def idea_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['idea_text'] = update.message.text
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔴 Высокий",  callback_data="iprio_high"),
        InlineKeyboardButton("🟡 Обычный",  callback_data="iprio_normal"),
        InlineKeyboardButton("⚪ Низкий",   callback_data="iprio_low"),
    ]])
    gid = context.user_data.get('idea_gid', update.effective_chat.id)
    tid = context.user_data.get('idea_tid', 0)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="Приоритет?", reply_markup=kb
    )
    return IDEA_PRIORITY

async def idea_priority_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    priority = q.data.replace("iprio_", "")
    text = context.user_data.get('idea_text', '')
    gid  = context.user_data.get('idea_gid', q.message.chat.id)
    tid  = context.user_data.get('idea_tid', 0)
    by   = q.from_user.username or str(q.from_user.id)
    conn = get_db()
    conn.execute('INSERT INTO ideas (group_id,thread_id,text,priority,created_by) VALUES (?,?,?,?,?)',
                 (gid, tid, text, priority, by)); conn.commit(); conn.close()
    P = {'high': '🔴', 'normal': '🟡', 'low': '⚪'}
    for k in ('idea_gid','idea_tid','idea_text'): context.user_data.pop(k, None)
    await q.edit_message_text(f"💡 {P.get(priority,'🟡')} Идея сохранена!\n_{text}_",
                              parse_mode='Markdown')
    return ConversationHandler.END


# ── Project brief ─────────────────────────────────────────────────

async def project_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!"); return
    tid = get_thread(update)
    conn = get_db(); brief = conn.execute(
        'SELECT * FROM project_brief WHERE group_id=? AND thread_id=?', (chat.id, tid)
    ).fetchone(); conn.close()
    msg = "📁 *О проекте*\n\n"
    if brief:
        for field, label in BRIEF_FIELDS.items():
            if brief[field]: msg += f"*{label}:*\n{brief[field]}\n\n"
        if brief['updated_by']:
            msg += f"_Обновил: @{brief['updated_by']} · {brief['updated_at'][:16]}_\n\n"
    else:
        msg += "Ещё ничего не заполнено.\n\n"
    msg += "Выбери раздел:"
    buttons, row = [], []
    for field, label in BRIEF_FIELDS.items():
        row.append(InlineKeyboardButton(label, callback_data=f"br_{field}"))
        if len(row) == 2: buttons.append(row); row = []
    if row: buttons.append(row)
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))

async def brief_field_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    field = q.data.replace("br_", "")
    context.user_data.update({'brief_field': field, 'brief_gid': q.message.chat.id,
                              'brief_tid': q.message.message_thread_id or 0})
    await q.message.reply_text(
        f"✏️ *{BRIEF_FIELDS.get(field, field)}*\n\nНапиши текст (/cancel — отмена):",
        parse_mode='Markdown', reply_markup=ReplyKeyboardRemove()
    )

async def brief_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get('brief_field'); gid = context.user_data.get('brief_gid')
    tid   = context.user_data.get('brief_tid', 0); value = update.message.text; by = get_uname(update)
    conn  = get_db()
    existing = conn.execute('SELECT * FROM project_brief WHERE group_id=? AND thread_id=?', (gid, tid)).fetchone()
    old_val  = existing[field] if existing else None
    if existing:
        conn.execute(f'UPDATE project_brief SET {field}=?,updated_by=?,updated_at=datetime("now") WHERE group_id=? AND thread_id=?', (value, by, gid, tid))
    else:
        conn.execute(f'INSERT INTO project_brief (group_id,thread_id,{field},updated_by) VALUES (?,?,?,?)', (gid, tid, value, by))
    conn.commit(); conn.close()
    log_change(gid, tid, 'brief', 0, BRIEF_FIELDS.get(field, field), old_val, value, by)
    for k in ('brief_field','brief_gid','brief_tid'): context.user_data.pop(k, None)
    await update.message.reply_text(f"✅ *{BRIEF_FIELDS.get(field, field)}* обновлено!",
                                    parse_mode='Markdown', reply_markup=main_keyboard())


# ── KPI ───────────────────────────────────────────────────────────

async def kpi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!"); return
    tid = get_thread(update); username = get_uname(update)
    conn = get_db()
    goals = conn.execute(
        'SELECT * FROM kpi_goals WHERE group_id=? AND thread_id=? ORDER BY metric',
        (chat.id, tid)
    ).fetchall()
    stats = conn.execute(
        '''SELECT assigned_username, COUNT(*) as total,
                  SUM(CASE WHEN status IN ('approved','published') THEN 1 ELSE 0 END) as done_cnt,
                  SUM(CASE WHEN status='submitted' THEN 1 ELSE 0 END) as sub_cnt,
                  SUM(CASE WHEN status IN ('active','in_progress','revision') THEN 1 ELSE 0 END) as act_cnt
           FROM tasks WHERE group_id=? AND thread_id=? AND assigned_username IS NOT NULL
           GROUP BY assigned_username ORDER BY done_cnt DESC''', (chat.id, tid)
    ).fetchall()
    mgr = is_manager(conn, chat.id, username); conn.close()
    msg = "📈 *KPI*\n\n"
    if goals:
        msg += "🎯 *Цели клиента:*\n"
        for g in goals: msg += f"  • {g['metric']}: {g['value']}\n"
        msg += f"\n_Установил: @{goals[0]['set_by']}_\n\n"
    else:
        msg += "🎯 *Цели не установлены*\n\n"
    if stats:
        msg += "👥 *Команда:*\n\n"
        for s in stats:
            total = s['total']; done = s['done_cnt'] or 0
            pct   = int(done / total * 100) if total else 0
            bar   = "🟢" if pct >= 80 else "🟡" if pct >= 50 else "🔴"
            msg  += f"{bar} *@{s['assigned_username']}*\n"
            msg  += f"   ✅ {done} · 📨 {s['sub_cnt'] or 0} · 🔄 {s['act_cnt'] or 0} ({pct}%)\n\n"
    buttons = []
    if mgr:
        buttons.append([InlineKeyboardButton("🎯 Установить цели", callback_data="kpi_set_start")])
    await update.message.reply_text(msg, parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

async def kpi_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    username = q.from_user.username or str(q.from_user.id)
    conn = get_db(); mgr = is_manager(conn, q.message.chat.id, username); conn.close()
    if not mgr:
        await q.answer("⛔ Только АМ и Директор", show_alert=True); return
    context.user_data['kpi_gid'] = q.message.chat.id
    context.user_data['kpi_tid'] = q.message.message_thread_id or 0
    buttons = []
    row = []
    for preset in KPI_PRESETS:
        row.append(InlineKeyboardButton(preset, callback_data=f"kpim_{preset}"))
        if len(row) == 3: buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton("✍️ Своё название", callback_data="kpim_custom")])
    await context.bot.send_message(
        chat_id=q.message.chat.id,
        message_thread_id=q.message.message_thread_id or None,
        text="🎯 *Установить цель*\n\nВыбери метрику:",
        parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons)
    )
    return KPI_METRIC

async def kpi_metric_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "kpim_custom":
        await q.edit_message_text("Напиши название метрики:")
        return KPI_METRIC
    metric = q.data.replace("kpim_", "")
    context.user_data['kpi_metric'] = metric
    await q.edit_message_text(f"🎯 Метрика: *{metric}*", parse_mode='Markdown')
    buttons = [
        [InlineKeyboardButton(v, callback_data=f"kpiv_{v}") for v in KPI_VALUES[:4]],
        [InlineKeyboardButton(v, callback_data=f"kpiv_{v}") for v in KPI_VALUES[4:]],
        [InlineKeyboardButton("✍️ Другое число", callback_data="kpiv_custom")],
    ]
    gid = context.user_data.get('kpi_gid', q.message.chat.id)
    tid = context.user_data.get('kpi_tid', 0)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="Значение:", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return KPI_VALUE

async def kpi_metric_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['kpi_metric'] = update.message.text
    buttons = [
        [InlineKeyboardButton(v, callback_data=f"kpiv_{v}") for v in KPI_VALUES[:4]],
        [InlineKeyboardButton(v, callback_data=f"kpiv_{v}") for v in KPI_VALUES[4:]],
        [InlineKeyboardButton("✍️ Другое число", callback_data="kpiv_custom")],
    ]
    gid = context.user_data.get('kpi_gid', update.effective_chat.id)
    tid = context.user_data.get('kpi_tid', 0)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text=f"🎯 *{update.message.text}*\n\nЗначение:",
        parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons)
    )
    return KPI_VALUE

async def kpi_value_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "kpiv_custom":
        await q.edit_message_text("Напиши значение:")
        return KPI_VALUE
    value = q.data.replace("kpiv_", "")
    await q.edit_message_text(f"Значение: *{value}*", parse_mode='Markdown')
    return await _save_kpi_goal(update, context, value)

async def kpi_value_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _save_kpi_goal(update, context, update.message.text)

async def _save_kpi_goal(update, context, value):
    gid    = context.user_data.get('kpi_gid', 0)
    tid    = context.user_data.get('kpi_tid', 0)
    metric = context.user_data.get('kpi_metric', '—')
    by     = get_uname(update)
    conn   = get_db()
    old    = conn.execute(
        'SELECT value FROM kpi_goals WHERE group_id=? AND thread_id=? AND metric=?',
        (gid, tid, metric)
    ).fetchone()
    conn.execute(
        '''INSERT INTO kpi_goals (group_id,thread_id,metric,value,set_by) VALUES (?,?,?,?,?)
           ON CONFLICT(group_id,thread_id,metric) DO UPDATE SET value=excluded.value,
           set_by=excluded.set_by, set_at=datetime('now')''',
        (gid, tid, metric, value, by)
    )
    conn.commit(); conn.close()
    log_change(gid, tid, 'kpi_goal', 0, metric, old['value'] if old else None, value, by)
    for k in ('kpi_gid','kpi_tid','kpi_metric'): context.user_data.pop(k, None)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text=f"✅ Цель: *{metric}* → {value}", parse_mode='Markdown'
    )
    return ConversationHandler.END


# ── History ───────────────────────────────────────────────────────

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Только в группах!"); return
    tid = get_thread(update)
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM change_log WHERE group_id=? AND thread_id=? ORDER BY changed_at DESC LIMIT 20',
        (chat.id, tid)
    ).fetchall(); conn.close()
    if not rows:
        await update.message.reply_text("📜 История пуста", reply_markup=main_keyboard()); return
    ETYPE = {'task': '📌', 'content_plan': '📅', 'brief': '📁', 'kpi_goal': '🎯'}
    msg = "📜 *История изменений*\n\n"
    for r in rows:
        icon = ETYPE.get(r['entity_type'], '•')
        when = r['changed_at'][:16].replace('T', ' '); who = r['changed_by'] or '?'
        if 'создана' in (r['field_name'] or ''):
            msg += f"➕ {icon} {r['new_value']}\n"
        else:
            msg += f"✏️ {icon} #{r['entity_id']} — {r['field_name']}\n"
            msg += f"   _{r['old_value']}_ → *{r['new_value']}*\n"
        msg += f"   👤 @{who} · {when}\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_keyboard())


# ── General handlers ─────────────────────────────────────────────

async def cancel_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for k in list(context.user_data.keys()): context.user_data.pop(k, None)
    await update.message.reply_text("❌ Отменено", reply_markup=main_keyboard())

async def cp_edit_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cp_id     = context.user_data.get('cp_edit_id'); field = context.user_data.get('cp_edit_field')
    gid       = context.user_data.get('cp_edit_gid', update.effective_chat.id)
    tid       = context.user_data.get('cp_edit_tid', 0); value = update.message.text; by = get_uname(update)
    field_map = {'desc': 'description', 'refs': 'refs', 'date': 'plan_date'}
    db_field  = field_map.get(field, field)
    conn = get_db()
    old  = conn.execute(f'SELECT {db_field} FROM content_plan WHERE id=?', (cp_id,)).fetchone()
    conn.execute(f'UPDATE content_plan SET {db_field}=? WHERE id=?', (value, cp_id))
    conn.commit(); conn.close()
    label_map = {'desc': '📝 Описание', 'refs': '🖼 Референс', 'date': '📅 Дата'}
    log_change(gid, tid, 'content_plan', cp_id, label_map.get(field, field),
               old[db_field] if old else '—', value, by)
    for k in ('cp_edit_id','cp_edit_field','cp_edit_gid','cp_edit_tid'): context.user_data.pop(k, None)
    await update.message.reply_text("✅ Обновлено!", reply_markup=main_keyboard())

async def cp_edit_refs_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('cp_edit_id'): return
    cp_id = context.user_data['cp_edit_id']
    gid   = context.user_data.get('cp_edit_gid', update.effective_chat.id)
    tid   = context.user_data.get('cp_edit_tid', 0); by = get_uname(update)
    value = f"photo:{update.message.photo[-1].file_id}"
    conn  = get_db()
    conn.execute('UPDATE content_plan SET refs=? WHERE id=?', (value, cp_id))
    conn.commit(); conn.close()
    log_change(gid, tid, 'content_plan', cp_id, '🖼 Референс', '—', 'фото', by)
    for k in ('cp_edit_id','cp_edit_field','cp_edit_gid','cp_edit_tid'): context.user_data.pop(k, None)
    await update.message.reply_text("✅ Фото-референс обновлён!", reply_markup=main_keyboard())

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('brief_field'):
        await brief_value_received(update, context); return
    if context.user_data.get('cp_edit_id') and context.user_data.get('cp_edit_field') != 'type':
        await cp_edit_value_received(update, context); return
    text = update.message.text
    if   text == "📋 Список":  await list_tasks(update, context)
    elif text == "📅 Контент": await content_plan(update, context)
    elif text == "💡 Идеи":    await ideas_list(update, context)
    elif text == "📁 Проект":  await project_brief(update, context)
    elif text == "📈 KPI":     await kpi(update, context)

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('cp_edit_id') and context.user_data.get('cp_edit_field') == 'refs':
        await cp_edit_refs_photo(update, context)


# ── main ─────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    _cancel_fallbacks = [
        CommandHandler("cancel", cancel),
        CallbackQueryHandler(cancel_conv_cb, pattern="^conv_cancel$"),
    ]

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
            T_REFS: [
                MessageHandler(filters.PHOTO, t_refs_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, t_refs_text),
                CommandHandler("skip", t_refs_skip),
                CallbackQueryHandler(t_refs_skip, pattern="^t_refs_skip$"),
            ],
            T_DATE: [
                CallbackQueryHandler(t_date_chosen, pattern="^tcday_"),
                CallbackQueryHandler(t_cal_nav,     pattern="^tc(prev|next)_"),
                CallbackQueryHandler(t_cal_noop,    pattern="^tcnoop$"),
                CallbackQueryHandler(t_date_skip,   pattern="^tcskip$"),
            ],
            T_ASSIGNEE: [
                CallbackQueryHandler(t_toggle_assignee,   pattern="^mtoggle_"),
                CallbackQueryHandler(t_confirm_assignees, pattern="^mconfirm$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, t_assignee_text),
            ],
        },
        fallbacks=_cancel_fallbacks,
        per_message=False, allow_reentry=True,
    )

    cp_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cp_add_start, pattern="^cp_add$")],
        states={
            CP_TYPE_S: [
                CallbackQueryHandler(cp_type_chosen, pattern="^cpt_"),
            ],
            CP_DESC_S: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cp_desc_received),
            ],
            CP_REFS_S: [
                MessageHandler(filters.PHOTO, cp_refs_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, cp_refs_received),
                CommandHandler("skip", cp_refs_skip),
                CallbackQueryHandler(cp_refs_skip, pattern="^cp_refs_skip$"),
            ],
            CP_DATE: [
                CallbackQueryHandler(cp_date_chosen, pattern="^cpday_"),
                CallbackQueryHandler(cp_cal_nav,     pattern="^cp(prev|next)_"),
                CallbackQueryHandler(cp_cal_noop,    pattern="^cpnoop$"),
                CallbackQueryHandler(cp_date_skip,   pattern="^cpskip$"),
            ],
        },
        fallbacks=_cancel_fallbacks,
        per_message=False, allow_reentry=True,
    )

    idea_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(idea_add_start, pattern="^idea_add$")],
        states={
            IDEA_TEXT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, idea_text_received)],
            IDEA_PRIORITY: [CallbackQueryHandler(idea_priority_chosen, pattern="^iprio_")],
        },
        fallbacks=_cancel_fallbacks,
        per_message=False, allow_reentry=True,
    )

    kpi_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(kpi_set_start, pattern="^kpi_set_start$")],
        states={
            KPI_METRIC: [
                CallbackQueryHandler(kpi_metric_chosen, pattern="^kpim_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, kpi_metric_text),
            ],
            KPI_VALUE: [
                CallbackQueryHandler(kpi_value_chosen, pattern="^kpiv_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, kpi_value_text),
            ],
        },
        fallbacks=_cancel_fallbacks,
        per_message=False, allow_reentry=True,
    )

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("join",    join))
    app.add_handler(CommandHandler("my",      my_tasks))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("cancel",  cancel_global))
    app.add_handler(task_conv)
    app.add_handler(cp_conv)
    app.add_handler(idea_conv)
    app.add_handler(kpi_conv)
    app.add_handler(CallbackQueryHandler(cancel_conv_cb,     pattern="^conv_cancel$"))
    app.add_handler(CallbackQueryHandler(role_chosen,        pattern="^role_"))
    app.add_handler(CallbackQueryHandler(status_change_cb,   pattern=r"^st_\d+_"))
    app.add_handler(CallbackQueryHandler(tlist_mine_cb,      pattern="^tlist_mine$"))
    app.add_handler(CallbackQueryHandler(tlist_given_cb,     pattern="^tlist_given$"))
    app.add_handler(CallbackQueryHandler(lmine_action_cb,    pattern=r"^lmst_\d+_"))
    app.add_handler(CallbackQueryHandler(lgiven_action_cb,   pattern=r"^lgst_\d+_"))
    app.add_handler(CallbackQueryHandler(brief_field_chosen, pattern="^br_"))
    app.add_handler(CallbackQueryHandler(cp_manage_cb,       pattern=r"^cp_manage_\d"))
    app.add_handler(CallbackQueryHandler(cp_manage_close_cb, pattern="^cp_manage_close$"))
    app.add_handler(CallbackQueryHandler(cp_noop_cb,         pattern="^cp_noop$"))
    app.add_handler(CallbackQueryHandler(cp_edit_cb,         pattern="^cpEdit_"))
    app.add_handler(CallbackQueryHandler(cp_edit_field_cb,   pattern="^cpEf_"))
    app.add_handler(CallbackQueryHandler(cp_edit_type_cb,    pattern="^cpt_"))
    app.add_handler(CallbackQueryHandler(cp_task_cb,         pattern="^cpTask_"))
    app.add_handler(CallbackQueryHandler(cptask_toggle_cb,   pattern="^cptoggle_"))
    app.add_handler(CallbackQueryHandler(cptask_confirm_cb,  pattern="^cpconfirm"))
    app.add_handler(CallbackQueryHandler(cp_del_cb,          pattern="^cpDel_"))
    app.add_handler(CallbackQueryHandler(cp_del_ok_cb,       pattern="^cpDelOk_"))
    app.add_handler(CallbackQueryHandler(cp_del_cancel_cb,   pattern="^cpDelCancel$"))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("✅ WhyNot бот v7 запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
