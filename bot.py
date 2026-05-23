import logging
import sqlite3
import os
import calendar as cal_lib
from datetime import datetime, date, timedelta
import re
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
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
TASK_TYPE_BTN_MAP = {
    '🎬 Съемка': 'shoot', '📢 Публикация': 'publish',
    '🎨 Дизайн': 'design', '✂️ Монтаж': 'edit',
    '📌 Другое': 'other',
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
    'main_offers':     '💎 Офферы',
    'visual_style':    '🎨 Визуал',
    'links':           '🔗 Ссылки',
    'competitors':     '⚔️ Конкуренты',
    'visual_refs':     '🖼 Референсы',
}
CP_TYPE_BTN_MAP = {
    '📸 Пост': '📸 Пост', '📱 Сторис': '📱 Сторис',
    '🎬 Рилс': '🎬 Рилс', '🎯 Актуальное': '🎯 Актуальное',
}
KPI_PRESETS = ['Постов', 'Сторис', 'Рилс', 'Охваты', 'Подписчики', 'ER %']
KPI_VALUES  = ['4', '8', '10', '12', '15', '20', '30', '50']
MONTH_RU    = ['', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
               'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']

# ── Button texts ─────────────────────────────────────────────────
BTN_TASK    = "➕ Задача"
BTN_IDEAS   = "💡 Идеи"
BTN_CONTENT = "📅 Контент"
BTN_KPI     = "📈 KPI"
BTN_LIST    = "📋 Список"
BTN_PROJECT = "📁 Проект"
BTN_CANCEL     = "❌ Отмена"
BTN_SKIP       = "⏭ Без референса"
BTN_NO_DATE    = "⏭ Без даты"
BTN_CP_ADD     = "➕ Добавить"
BTN_CP_MANAGE  = "⚙️ Управление"
BTN_LIST_MINE  = "📥 Мне назначено"
BTN_LIST_GIVEN = "📤 Я назначил"
BTN_IDEAS_ADD  = "💡 Новая идея"

MAIN_BTNS = {BTN_TASK, BTN_IDEAS, BTN_CONTENT, BTN_KPI, BTN_LIST, BTN_PROJECT}


# ── Database ────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL, username TEXT NOT NULL,
            full_name TEXT, role TEXT DEFAULT 'executor',
            added_at TEXT DEFAULT (datetime('now')),
            UNIQUE(group_id, username)
        );
        CREATE TABLE IF NOT EXISTS project_brief (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL, thread_id INTEGER DEFAULT 0,
            brand_info TEXT, target_audience TEXT, tone_of_voice TEXT,
            main_offers TEXT, visual_style TEXT, links TEXT,
            competitors TEXT, visual_refs TEXT,
            updated_by TEXT, updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(group_id, thread_id)
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL, thread_id INTEGER DEFAULT 0,
            task_type TEXT DEFAULT 'other', description TEXT NOT NULL,
            refs TEXT, task_date TEXT, assigned_username TEXT,
            status TEXT DEFAULT 'active', created_by TEXT,
            created_at TEXT DEFAULT (datetime('now')), done_at TEXT
        );
        CREATE TABLE IF NOT EXISTS content_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL, thread_id INTEGER DEFAULT 0,
            content_type TEXT, description TEXT NOT NULL, refs TEXT,
            plan_date TEXT, status TEXT DEFAULT 'planned',
            created_by TEXT, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS kpi_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL, thread_id INTEGER DEFAULT 0,
            metric TEXT NOT NULL, value TEXT NOT NULL, set_by TEXT,
            set_at TEXT DEFAULT (datetime('now')),
            UNIQUE(group_id, thread_id, metric)
        );
        CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL, thread_id INTEGER DEFAULT 0,
            text TEXT NOT NULL, priority TEXT DEFAULT 'normal',
            created_by TEXT, created_at TEXT DEFAULT (datetime('now')),
            converted INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER, thread_id INTEGER DEFAULT 0,
            entity_type TEXT, entity_id INTEGER,
            field_name TEXT, old_value TEXT, new_value TEXT,
            changed_by TEXT, changed_at TEXT DEFAULT (datetime('now'))
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
        'INSERT INTO change_log (group_id,thread_id,entity_type,entity_id,field_name,old_value,new_value,changed_by) VALUES (?,?,?,?,?,?,?,?)',
        (gid, tid, etype, eid, field,
         str(old) if old is not None else '—',
         str(new) if new is not None else '—', by)
    )
    conn.commit(); conn.close()

def is_manager(conn, group_id: int, username: str) -> bool:
    m = conn.execute('SELECT role FROM members WHERE group_id=? AND username=?',
                     (group_id, username)).fetchone()
    return bool(m and m['role'] in ('director', 'am'))

def is_url(text: str) -> bool:
    return bool(text and (text.startswith('http://') or text.startswith('https://')))

def format_refs(refs: str) -> str:
    if not refs: return ''
    if refs.startswith('photo:'): return ''
    if is_url(refs): return f'\n🔗 [Референс]({refs})'
    return f'\n🔗 {refs}'

def _do_status_update(task_id: int, new_status: str, by: str):
    conn = get_db()
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not task: conn.close(); return
    old_status = task['status']
    done_at = datetime.now().isoformat() if new_status in ('approved', 'published') else None
    conn.execute('UPDATE tasks SET status=?,done_at=? WHERE id=?', (new_status, done_at, task_id))
    conn.commit(); conn.close()
    log_change(task['group_id'], task['thread_id'], 'task', task_id, 'статус',
               STATUSES.get(old_status), STATUSES.get(new_status, new_status), by)


# ── Reply keyboards (bottom of screen) ──────────────────────────

def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [BTN_TASK,    BTN_IDEAS],
        [BTN_CONTENT, BTN_KPI],
        [BTN_LIST,    BTN_PROJECT],
    ], resize_keyboard=True)

def task_type_kb_reply() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ['🎬 Съемка',   '📢 Публикация'],
        ['🎨 Дизайн',   '✂️ Монтаж'],
        ['📌 Другое'],
        [BTN_CANCEL],
    ], resize_keyboard=True, one_time_keyboard=True)

def cp_type_kb_reply() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ['📸 Пост',   '📱 Сторис'],
        ['🎬 Рилс',   '🎯 Актуальное'],
        [BTN_CANCEL],
    ], resize_keyboard=True, one_time_keyboard=True)

def cancel_kb_reply() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True, one_time_keyboard=True)

def refs_kb_reply() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_SKIP, BTN_CANCEL]], resize_keyboard=True, one_time_keyboard=True
    )

def month_kb() -> ReplyKeyboardMarkup:
    today = date.today()
    months = []
    for i in range(6):
        d = (today.replace(day=1) + timedelta(days=32 * i)).replace(day=1)
        months.append(f"{MONTH_RU[d.month]} {d.year}")
    return ReplyKeyboardMarkup([
        months[:3], months[3:],
        [BTN_NO_DATE, BTN_CANCEL],
    ], resize_keyboard=True, one_time_keyboard=True)

def day_kb(year: int, month: int) -> ReplyKeyboardMarkup:
    max_d = cal_lib.monthrange(year, month)[1]
    days  = [str(d) for d in range(1, max_d + 1)]
    rows  = [days[i:i+7] for i in range(0, len(days), 7)]
    rows.append(["◀️ Месяц", BTN_NO_DATE, BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

def content_action_kb(is_mgr: bool = True) -> ReplyKeyboardMarkup:
    if is_mgr:
        return ReplyKeyboardMarkup(
            [[BTN_CP_ADD, BTN_CP_MANAGE], [BTN_CANCEL]],
            resize_keyboard=True, one_time_keyboard=True
        )
    return main_kb()

def list_type_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_LIST_MINE, BTN_LIST_GIVEN], [BTN_CANCEL]],
        resize_keyboard=True, one_time_keyboard=True
    )

def ideas_action_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_IDEAS_ADD, BTN_CANCEL]],
        resize_keyboard=True, one_time_keyboard=True
    )


# ── Inline keyboards (in-chat message buttons) ──────────────────

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

def multi_assignee_keyboard(group_id: int, selected: set):
    conn = get_db()
    members = conn.execute(
        'SELECT username, full_name, role FROM members WHERE group_id=? ORDER BY full_name',
        (group_id,)
    ).fetchall()
    conn.close()
    if not members: return None
    re_map = {'director': '👑', 'am': '📋', 'executor': '✂️'}
    buttons = []
    for m in members:
        check = "✅" if m['username'] in selected else "☐"
        label = f"{check} {re_map.get(m['role'],'')} {m['full_name'] or m['username']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"mtoggle_{m['username']}")])
    count   = len(selected)
    confirm = f"✅ Назначить ({count})" if count else "✅ Без исполнителя"
    buttons.append([InlineKeyboardButton(confirm, callback_data="mconfirm")])
    return InlineKeyboardMarkup(buttons)

def cancel_kb_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="conv_cancel")]])

def task_action_kb(task_id: int, status: str):
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


# ── send_menu ────────────────────────────────────────────────────

async def send_menu(chat_id: int, tid: int, context, text: str = "Выбери действие:"):
    await context.bot.send_message(
        chat_id=chat_id, message_thread_id=tid if tid else None,
        text=text, reply_markup=main_kb()
    )


# ── /start & /join ───────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ['group', 'supergroup']:
        tid = get_thread(update)
        await context.bot.send_message(
            chat_id=chat.id, message_thread_id=tid if tid else None,
            text=f"✅ *{chat.title}*\n\nЗарегистрируйся: /join\nИспользуй кнопки ниже 👇",
            parse_mode='Markdown', reply_markup=main_kb()
        )
    else:
        await update.message.reply_text("👋 Добавь меня в группу и напиши /start")

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


# ── /my & /history ────────────────────────────────────────────────

async def my_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = get_uname(update)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE assigned_username LIKE ? AND status NOT IN ('approved','published') ORDER BY task_date NULLS LAST, created_at",
        (f'%{username}%',)
    ).fetchall()
    conn.close()
    rows = [t for t in rows if username in (t['assigned_username'] or '').split(',')]
    if not rows:
        await update.message.reply_text("🎉 Нет активных задач!"); return
    msg = f"📋 *Мои задачи — @{username}*\n\n"
    for t in rows:
        dot  = STATUS_DOT.get(t['status'], '⚪')
        e    = TASK_TYPES.get(t['task_type'], ('📌',))[0]
        msg += f"{dot} {e} *#{t['id']}* {t['description']}\n   {STATUSES.get(t['status'], t['status'])}"
        if t['task_date']: msg += f" · 📅 {t['task_date']}"
        msg += "\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

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
        await update.message.reply_text("📜 История пуста"); return
    ETYPE = {'task': '📌', 'content_plan': '📅', 'brief': '📁', 'kpi_goal': '🎯'}
    msg = "📜 *История изменений*\n\n"
    for r in rows:
        icon = ETYPE.get(r['entity_type'], '•')
        when = r['changed_at'][:16].replace('T', ' ')
        if 'создана' in (r['field_name'] or ''):
            msg += f"➕ {icon} {r['new_value']}\n"
        else:
            msg += f"✏️ {icon} #{r['entity_id']} — {r['field_name']}\n   _{r['old_value']}_ → *{r['new_value']}*\n"
        msg += f"   👤 @{r['changed_by'] or '?'} · {when}\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')


# ── Task creation conversation ────────────────────────────────────

async def task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.effective_chat.id
    tid = get_thread(update)
    context.user_data.clear()
    context.user_data['t_gid'] = gid
    context.user_data['t_tid'] = tid
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="➕ *Новая задача*\n\nВыбери тип:",
        parse_mode='Markdown', reply_markup=task_type_kb_reply()
    )
    return T_TYPE

async def t_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text  = update.message.text
    ttype = TASK_TYPE_BTN_MAP.get(text, 'other')
    context.user_data['t_type'] = ttype
    emoji, name = TASK_TYPES.get(ttype, ('📌', 'Задача'))
    gid = context.user_data['t_gid']; tid = context.user_data['t_tid']
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text=f"{emoji} *{name}*\n\nОпиши задачу:",
        parse_mode='Markdown', reply_markup=cancel_kb_reply()
    )
    return T_DESC

async def t_desc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_desc'] = update.message.text
    gid = context.user_data['t_gid']; tid = context.user_data['t_tid']
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="🖼 *Референс?*\n\nПришли фото, ссылку или пропусти:",
        parse_mode='Markdown', reply_markup=refs_kb_reply()
    )
    return T_REFS

async def t_refs_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_refs'] = update.message.text
    return await _t_ask_date(update, context)

async def t_refs_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_refs'] = f"photo:{update.message.photo[-1].file_id}"
    gid = context.user_data['t_gid']; tid = context.user_data['t_tid']
    await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                   text="🖼 Фото сохранено")
    return await _t_ask_date(update, context)

async def t_refs_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_refs'] = None
    if update.callback_query: await update.callback_query.answer()
    return await _t_ask_date(update, context)

async def _t_ask_date(update, context):
    gid = context.user_data['t_gid']; tid = context.user_data['t_tid']
    label = "📅 Дата съёмки:" if context.user_data.get('t_type') == 'shoot' else "📅 Дедлайн:"
    context.user_data.pop('t_date_ym', None)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text=f"{label}\n\nВыбери месяц:", reply_markup=month_kb()
    )
    return T_DATE

async def t_date_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    gid  = context.user_data.get('t_gid', update.effective_chat.id)
    tid  = context.user_data.get('t_tid', 0)
    if text == "◀️ Месяц":
        context.user_data.pop('t_date_ym', None)
        await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                       text="📅 Выбери месяц:", reply_markup=month_kb())
        return T_DATE
    if 't_date_ym' not in context.user_data:
        parts = text.strip().split()
        if len(parts) == 2:
            try:
                m_idx = MONTH_RU.index(parts[0]); year = int(parts[1])
                if 1 <= m_idx <= 12:
                    context.user_data['t_date_ym'] = (year, m_idx)
                    await context.bot.send_message(
                        chat_id=gid, message_thread_id=tid if tid else None,
                        text=f"📅 {MONTH_RU[m_idx]} {year} — выбери число:",
                        reply_markup=day_kb(year, m_idx))
                    return T_DATE
            except (ValueError, IndexError):
                pass
        await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                       text="Выбери месяц:", reply_markup=month_kb())
        return T_DATE
    else:
        y, m = context.user_data['t_date_ym']
        try:
            d = int(text)
            if 1 <= d <= cal_lib.monthrange(y, m)[1]:
                context.user_data['t_date'] = f"{d:02d}.{m:02d}.{y}"
                context.user_data.pop('t_date_ym', None)
                return await _t_ask_assignee(update, context)
        except ValueError:
            pass
        await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                       text=f"Выбери число ({MONTH_RU[m]} {y}):",
                                       reply_markup=day_kb(y, m))
        return T_DATE

async def t_date_skip_kb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['t_date'] = None
    context.user_data.pop('t_date_ym', None)
    return await _t_ask_assignee(update, context)

async def _t_ask_assignee(update, context):
    gid = context.user_data['t_gid']; tid = context.user_data['t_tid']
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
            text="👤 Напиши @username:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Без исполнителя", callback_data="mconfirm"),
                InlineKeyboardButton("❌ Отмена",          callback_data="conv_cancel"),
            ]])
        )
    return T_ASSIGNEE

async def t_toggle_assignee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    username = q.data.replace("mtoggle_", "")
    selected = context.user_data.setdefault('m_selected', set())
    if username in selected: selected.discard(username)
    else: selected.add(username)
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
        'INSERT INTO tasks (group_id,thread_id,task_type,description,refs,task_date,assigned_username,created_by) VALUES (?,?,?,?,?,?,?,?)',
        (gid, tid, ttype, desc, refs, task_date, assignee, by)
    )
    task_id = cur.lastrowid; conn.commit(); conn.close()
    log_change(gid, tid, 'task', task_id, 'создана', None,
               f"{TASK_TYPES.get(ttype,('',''))[1]}: {desc}", by)
    emoji, tname = TASK_TYPES.get(ttype, ('📌', 'Задача'))
    msg = f"✅ *Задача #{task_id} создана*\n\n{emoji} {tname}: {desc}\n"
    if assignee:
        msg += f"👤 {' '.join(f'@{u}' for u in assignee.split(','))}\n"
    if task_date: msg += f"📅 {task_date}\n"
    if refs and not refs.startswith("photo:"): msg += format_refs(refs)
    context.user_data.clear()
    await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                   text=msg, parse_mode='Markdown',
                                   reply_markup=main_kb(),
                                   disable_web_page_preview=True)
    if refs and refs.startswith("photo:"):
        await context.bot.send_photo(chat_id=gid, message_thread_id=tid if tid else None,
                                     photo=refs.replace("photo:", ""), caption="🖼 Референс")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    gid = update.effective_chat.id; tid = get_thread(update)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="❌ Отменено", reply_markup=main_kb()
    )
    return ConversationHandler.END

async def cancel_conv_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Отменено")
    context.user_data.clear()
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    gid = q.message.chat.id; tid = q.message.message_thread_id or 0
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="❌ Отменено", reply_markup=main_kb()
    )
    return ConversationHandler.END


# ── Status change ────────────────────────────────────────────────

async def status_change_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.split("_", 2); task_id = int(parts[1]); new_status = parts[2]
    by = q.from_user.username or str(q.from_user.id)
    _do_status_update(task_id, new_status, by)
    conn = get_db(); task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone(); conn.close()
    if not task: return
    dot  = STATUS_DOT.get(new_status, '⚪')
    e    = TASK_TYPES.get(task['task_type'], ('📌',))[0]
    text = f"{dot} {e} *#{task_id} {task['description']}*\n{STATUSES.get(new_status, new_status)}"
    if task['assigned_username']:
        text += f"\n👤 {' '.join(f'@{u}' for u in task['assigned_username'].split(','))}"
    if task['task_date']: text += f"\n📅 {task['task_date']}"
    await q.edit_message_text(text, parse_mode='Markdown',
                              reply_markup=task_action_kb(task_id, new_status))


# ── Task list ────────────────────────────────────────────────────

def _build_mine_view(chat_id, tid, username):
    conn = get_db()
    active = conn.execute(
        "SELECT * FROM tasks WHERE group_id=? AND thread_id=? AND assigned_username LIKE ? AND status NOT IN ('approved','published') ORDER BY CASE status WHEN 'revision' THEN 1 WHEN 'submitted' THEN 2 WHEN 'in_progress' THEN 3 WHEN 'active' THEN 4 ELSE 5 END, task_date NULLS LAST LIMIT 20",
        (chat_id, tid, f'%{username}%')
    ).fetchall()
    done = conn.execute(
        "SELECT * FROM tasks WHERE group_id=? AND thread_id=? AND assigned_username LIKE ? AND status IN ('approved','published') ORDER BY done_at DESC LIMIT 5",
        (chat_id, tid, f'%{username}%')
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
            e = TASK_TYPES.get(t['task_type'], ('📌',))[0]
            dot = STATUS_DOT.get(t['status'], '⚪')
            ds = f" · 📅 {t['task_date']}" if t['task_date'] else ""
            msg += f"{dot} {e} *#{t['id']}* {t['description'][:50]}{ds}\n   {STATUSES.get(t['status'], t['status'])}\n\n"
            row = []
            if t['status'] == 'active':
                row = [InlineKeyboardButton("▶️ Начать", callback_data=f"lmst_{t['id']}_in_progress"),
                       InlineKeyboardButton("📨 На одобрение", callback_data=f"lmst_{t['id']}_submitted")]
            elif t['status'] == 'in_progress':
                row = [InlineKeyboardButton("📨 Отправить на одобрение",
                                            callback_data=f"lmst_{t['id']}_submitted")]
            elif t['status'] == 'revision':
                row = [InlineKeyboardButton("▶️ Возобновить", callback_data=f"lmst_{t['id']}_in_progress"),
                       InlineKeyboardButton("📨 Сдать снова",  callback_data=f"lmst_{t['id']}_submitted")]
            elif t['status'] == 'approved':
                row = [InlineKeyboardButton("🚀 Опубликовать", callback_data=f"lmst_{t['id']}_published")]
            if row: buttons.append(row)
        if done:
            msg += "─────\n✅ *Выполненные:*\n"
            for t in done:
                e = TASK_TYPES.get(t['task_type'], ('📌',))[0]
                msg += f"✅ {e} #{t['id']} {t['description'][:45]}\n"
            msg += "\n"
    buttons.append([InlineKeyboardButton("📤 Я назначил →", callback_data="tlist_given")])
    return msg, InlineKeyboardMarkup(buttons)

def _build_given_view(chat_id, tid, username):
    conn = get_db()
    active = conn.execute(
        "SELECT * FROM tasks WHERE group_id=? AND thread_id=? AND created_by=? AND status NOT IN ('approved','published') ORDER BY CASE status WHEN 'submitted' THEN 1 WHEN 'revision' THEN 2 WHEN 'in_progress' THEN 3 WHEN 'active' THEN 4 ELSE 5 END, task_date NULLS LAST LIMIT 20",
        (chat_id, tid, username)
    ).fetchall()
    done = conn.execute(
        "SELECT * FROM tasks WHERE group_id=? AND thread_id=? AND created_by=? AND status IN ('approved','published') ORDER BY done_at DESC LIMIT 5",
        (chat_id, tid, username)
    ).fetchall()
    conn.close()
    msg = "📤 *Я назначил*\n\n"
    buttons = []
    if not active and not done:
        msg += "Нет назначенных задач."
    else:
        for t in active:
            e    = TASK_TYPES.get(t['task_type'], ('📌',))[0]
            dot  = STATUS_DOT.get(t['status'], '⚪')
            asgn = ' '.join(f"@{u}" for u in (t['assigned_username'] or '').split(',') if u)
            ds   = f" · 📅 {t['task_date']}" if t['task_date'] else ""
            msg += f"{dot} {e} *#{t['id']}* {t['description'][:45]}\n   {asgn}{ds} · {STATUSES.get(t['status'], t['status'])}\n\n"
            row = []
            if t['status'] == 'submitted':
                row = [InlineKeyboardButton(f"✅ Принять #{t['id']}",   callback_data=f"lgst_{t['id']}_approved"),
                       InlineKeyboardButton(f"🔁 Доработка #{t['id']}", callback_data=f"lgst_{t['id']}_revision")]
            elif t['status'] == 'approved':
                row = [InlineKeyboardButton(f"🚀 Опубл. #{t['id']}", callback_data=f"lgst_{t['id']}_published")]
            if row: buttons.append(row)
        if done:
            msg += "─────\n✅ *Выполненные:*\n"
            for t in done:
                e = TASK_TYPES.get(t['task_type'], ('📌',))[0]
                msg += f"✅ {e} #{t['id']} {t['description'][:45]}\n"
            msg += "\n"
    buttons.append([InlineKeyboardButton("← 📥 Мне назначены", callback_data="tlist_mine")])
    return msg, InlineKeyboardMarkup(buttons)

async def handle_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.effective_chat.id; tid = get_thread(update)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="📋 *Список задач*\n\nЧьи показать?",
        parse_mode='Markdown', reply_markup=list_type_kb()
    )

async def handle_list_mine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.effective_chat.id; tid = get_thread(update)
    msg, kb = _build_mine_view(gid, tid, get_uname(update))
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text=msg, parse_mode='Markdown', reply_markup=kb
    )

async def handle_list_given(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.effective_chat.id; tid = get_thread(update)
    msg, kb = _build_given_view(gid, tid, get_uname(update))
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text=msg, parse_mode='Markdown', reply_markup=kb
    )

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
    q = update.callback_query; await q.answer()
    parts = q.data.split("_", 2)
    _do_status_update(int(parts[1]), parts[2], q.from_user.username or str(q.from_user.id))
    username = q.from_user.username or str(q.from_user.id)
    msg, kb = _build_mine_view(q.message.chat.id, q.message.message_thread_id or 0, username)
    await q.edit_message_text(msg, parse_mode='Markdown', reply_markup=kb)

async def lgiven_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.split("_", 2)
    _do_status_update(int(parts[1]), parts[2], q.from_user.username or str(q.from_user.id))
    username = q.from_user.username or str(q.from_user.id)
    msg, kb = _build_given_view(q.message.chat.id, q.message.message_thread_id or 0, username)
    await q.edit_message_text(msg, parse_mode='Markdown', reply_markup=kb)


# ── Ideas ─────────────────────────────────────────────────────────

async def _send_ideas(chat_id, tid, context):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM ideas WHERE group_id=? AND thread_id=? AND converted=0 ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, created_at DESC LIMIT 10",
        (chat_id, tid)
    ).fetchall(); conn.close()
    P   = {'high': '🔴', 'normal': '🟡', 'low': '⚪'}
    msg = "💡 *Идеи*\n\n"
    msg += "".join(f"{P.get(i['priority'],'🟡')} *#{i['id']}* {i['text']}\n" for i in rows) if rows else "Пока пусто.\n"
    await context.bot.send_message(
        chat_id=chat_id, message_thread_id=tid if tid else None,
        text=msg, parse_mode='Markdown',
        reply_markup=ideas_action_kb()
    )

async def handle_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_ideas(update.effective_chat.id, get_thread(update), context)

async def idea_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        q = update.callback_query; await q.answer()
        gid = q.message.chat.id; tid = q.message.message_thread_id or 0
    else:
        gid = update.effective_chat.id; tid = get_thread(update)
    context.user_data['idea_gid'] = gid
    context.user_data['idea_tid'] = tid
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="💡 Напиши идею:", reply_markup=cancel_kb_reply()
    )
    return IDEA_TEXT

async def idea_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['idea_text'] = update.message.text
    gid = context.user_data.get('idea_gid', update.effective_chat.id)
    tid = context.user_data.get('idea_tid', 0)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="Приоритет?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 Высокий",  callback_data="iprio_high"),
            InlineKeyboardButton("🟡 Обычный",  callback_data="iprio_normal"),
            InlineKeyboardButton("⚪ Низкий",   callback_data="iprio_low"),
        ]])
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
    await _send_ideas(gid, tid, context)
    return ConversationHandler.END


# ── Content plan helpers ─────────────────────────────────────────

def _build_cp_manage_markup(chat_id, tid, offset=0):
    """Returns (msg, InlineKeyboardMarkup) for content plan management view."""
    conn  = get_db()
    rows  = conn.execute(
        "SELECT * FROM content_plan WHERE group_id=? AND thread_id=? AND status NOT IN ('done','published') ORDER BY plan_date, id LIMIT 5 OFFSET ?",
        (chat_id, tid, offset)).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM content_plan WHERE group_id=? AND thread_id=? AND status NOT IN ('done','published')",
        (chat_id, tid)).fetchone()[0]
    conn.close()
    back = [InlineKeyboardButton("← Контент", callback_data="mm_content")]
    if not rows:
        return ("⚙️ *Управление*\n\nАктивных записей нет.",
                InlineKeyboardMarkup([back]))
    DOT     = {'planned': '⚪', 'in_progress': '🟡'}
    msg     = f"⚙️ *Управление* ({offset+1}–{min(offset+5, total)} из {total})\n\n"
    buttons = []
    for r in rows:
        buttons.append([InlineKeyboardButton(
            f"{DOT.get(r['status'],'⚪')} #{r['id']} {r['content_type']} {r['plan_date'] or ''}",
            callback_data="cp_noop")])
        buttons.append([
            InlineKeyboardButton("✏️ Изменить", callback_data=f"cpEdit_{r['id']}"),
            InlineKeyboardButton("📌 В задачи", callback_data=f"cpTask_{r['id']}"),
        ])
        buttons.append([
            InlineKeyboardButton("✅ Готово",   callback_data=f"cpSt_{r['id']}_done"),
            InlineKeyboardButton("🚀 Опубл.",  callback_data=f"cpSt_{r['id']}_published"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"cpDel_{r['id']}"),
        ])
    nav = []
    if offset > 0:         nav.append(InlineKeyboardButton("◀️", callback_data=f"cp_manage_{offset-5}"))
    if offset + 5 < total: nav.append(InlineKeyboardButton("▶️", callback_data=f"cp_manage_{offset+5}"))
    if nav: buttons.append(nav)
    buttons.append(back)
    return msg, InlineKeyboardMarkup(buttons)


# ── Content plan ──────────────────────────────────────────────────

async def _send_content(chat_id, tid, username, context):
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM content_plan WHERE group_id=? AND thread_id=? ORDER BY plan_date, id LIMIT 50',
        (chat_id, tid)
    ).fetchall()
    mgr = is_manager(conn, chat_id, username); conn.close()
    DOT = {'planned': '⚪', 'in_progress': '🟡', 'done': '🟢', 'published': '✅'}
    active_rows = [r for r in rows if r['status'] not in ('done', 'published')]
    done_rows   = [r for r in rows if r['status'] in ('done', 'published')]
    msg = "📅 *Контент-план*\n\n"
    if active_rows:
        for r in active_rows:
            dot = DOT.get(r['status'], '⚪')
            ri  = (" 🖼" if r['refs'] and r['refs'].startswith("photo:") else
                   " 🔗" if r['refs'] else "")
            msg += f"{dot} *#{r['id']}* {r['content_type']} — {r['plan_date'] or '—'}\n   {r['description']}{ri}\n\n"
    elif not done_rows:
        msg += "Пока пусто.\n"
    else:
        msg += "Активных записей нет.\n"
    if done_rows:
        msg += "─────\n✅ *Отработано:*\n\n"
        for r in done_rows[:8]:
            msg += f"✅ *#{r['id']}* {r['content_type']} — {r['plan_date'] or '—'}\n   {r['description']}\n\n"
        if len(done_rows) > 8:
            msg += f"_...и ещё {len(done_rows)-8} записей_\n"
    if not mgr:
        msg += "_Редактирование: АМ и Директор_"
    await context.bot.send_message(
        chat_id=chat_id, message_thread_id=tid if tid else None,
        text=msg, parse_mode='Markdown',
        reply_markup=content_action_kb(mgr)
    )

async def handle_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_content(update.effective_chat.id, get_thread(update), get_uname(update), context)

async def mm_content_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    username = q.from_user.username or str(q.from_user.id)
    await _send_content(q.message.chat.id, q.message.message_thread_id or 0, username, context)

async def cp_manage_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    offset   = int(q.data.replace("cp_manage_", ""))
    username = q.from_user.username or str(q.from_user.id)
    conn     = get_db()
    if not is_manager(conn, q.message.chat.id, username):
        conn.close(); await q.answer("⛔ Только АМ и Директор", show_alert=True); return
    conn.close()
    tid = q.message.message_thread_id or 0
    msg, kb = _build_cp_manage_markup(q.message.chat.id, tid, offset)
    await q.edit_message_text(msg, parse_mode='Markdown', reply_markup=kb)

async def cp_noop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ── Content plan creation conversation ───────────────────────────

async def cp_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        q = update.callback_query; await q.answer()
        gid = q.message.chat.id; tid = q.message.message_thread_id or 0
    else:
        gid = update.effective_chat.id; tid = get_thread(update)
    context.user_data['cp_gid'] = gid
    context.user_data['cp_tid'] = tid
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="📅 *Новая запись*\n\nТип контента:",
        parse_mode='Markdown', reply_markup=cp_type_kb_reply()
    )
    return CP_TYPE_S

async def handle_cp_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gid = update.effective_chat.id; tid = get_thread(update)
    username = get_uname(update)
    conn = get_db()
    if not is_manager(conn, gid, username):
        conn.close()
        await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                       text="⛔ Только АМ и Директор", reply_markup=main_kb())
        return
    conn.close()
    msg, kb = _build_cp_manage_markup(gid, tid, 0)
    await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                   text=msg, parse_mode='Markdown', reply_markup=kb)

async def cp_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    context.user_data['cp_type'] = text
    gid = context.user_data.get('cp_gid', update.effective_chat.id)
    tid = context.user_data.get('cp_tid', 0)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text=f"Тип: {text}\n\n✏️ Что должно быть в публикации?",
        reply_markup=cancel_kb_reply()
    )
    return CP_DESC_S

async def cp_desc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['cp_desc'] = update.message.text
    gid = context.user_data.get('cp_gid', update.effective_chat.id)
    tid = context.user_data.get('cp_tid', 0)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="🖼 *Референс?*\n\nПришли фото или ссылку:",
        parse_mode='Markdown', reply_markup=refs_kb_reply()
    )
    return CP_REFS_S

async def cp_refs_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['cp_refs'] = update.message.text
    return await _cp_ask_date(update, context)

async def cp_refs_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['cp_refs'] = f"photo:{update.message.photo[-1].file_id}"
    gid = context.user_data.get('cp_gid', update.effective_chat.id)
    tid = context.user_data.get('cp_tid', 0)
    await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                   text="🖼 Фото сохранено")
    return await _cp_ask_date(update, context)

async def cp_refs_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['cp_refs'] = None
    if update.callback_query: await update.callback_query.answer()
    return await _cp_ask_date(update, context)

async def _cp_ask_date(update, context):
    gid = context.user_data.get('cp_gid', update.effective_chat.id)
    tid = context.user_data.get('cp_tid', 0)
    context.user_data.pop('cp_date_ym', None)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="📅 Дата публикации:\n\nВыбери месяц:", reply_markup=month_kb()
    )
    return CP_DATE

async def cp_date_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    gid  = context.user_data.get('cp_gid', update.effective_chat.id)
    tid  = context.user_data.get('cp_tid', 0)
    if text == "◀️ Месяц":
        context.user_data.pop('cp_date_ym', None)
        await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                       text="📅 Выбери месяц:", reply_markup=month_kb())
        return CP_DATE
    if 'cp_date_ym' not in context.user_data:
        parts = text.strip().split()
        if len(parts) == 2:
            try:
                m_idx = MONTH_RU.index(parts[0]); year = int(parts[1])
                if 1 <= m_idx <= 12:
                    context.user_data['cp_date_ym'] = (year, m_idx)
                    await context.bot.send_message(
                        chat_id=gid, message_thread_id=tid if tid else None,
                        text=f"📅 {MONTH_RU[m_idx]} {year} — выбери число:",
                        reply_markup=day_kb(year, m_idx))
                    return CP_DATE
            except (ValueError, IndexError):
                pass
        await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                       text="Выбери месяц:", reply_markup=month_kb())
        return CP_DATE
    else:
        y, m = context.user_data['cp_date_ym']
        try:
            d = int(text)
            if 1 <= d <= cal_lib.monthrange(y, m)[1]:
                context.user_data['cp_date'] = f"{d:02d}.{m:02d}.{y}"
                context.user_data.pop('cp_date_ym', None)
                return await _save_cp(update, context)
        except ValueError:
            pass
        await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                       text=f"Выбери число ({MONTH_RU[m]} {y}):",
                                       reply_markup=day_kb(y, m))
        return CP_DATE

async def cp_date_skip_kb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['cp_date'] = None
    context.user_data.pop('cp_date_ym', None)
    return await _save_cp(update, context)

async def _save_cp(update, context):
    gid   = context.user_data.get('cp_gid', 0); tid  = context.user_data.get('cp_tid', 0)
    ct    = context.user_data.get('cp_type', '—'); desc = context.user_data.get('cp_desc', '—')
    refs  = context.user_data.get('cp_refs'); plan_date = context.user_data.get('cp_date')
    by    = get_uname(update)
    conn  = get_db()
    cur   = conn.execute(
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
    if refs and not refs.startswith("photo:"): msg += format_refs(refs)
    await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                   text=msg, parse_mode='Markdown')
    if refs and refs.startswith("photo:"):
        await context.bot.send_photo(chat_id=gid, message_thread_id=tid if tid else None,
                                     photo=refs.replace("photo:", ""), caption="🖼 Референс")
    await _send_content(gid, tid, by, context)
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
        [InlineKeyboardButton("← Управление", callback_data="cp_manage_0")],
    ])
    if r['refs'] and r['refs'].startswith("photo:"):
        await q.message.reply_photo(r['refs'].replace("photo:", ""), caption="🖼 Текущий референс")
    await q.edit_message_text(msg, parse_mode='Markdown', reply_markup=kb)

async def cp_edit_field_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.replace("cpEf_", "").split("_"); cp_id, field = int(parts[0]), parts[1]
    context.user_data.update({'cp_edit_id': cp_id, 'cp_edit_field': field,
                              'cp_edit_gid': q.message.chat.id,
                              'cp_edit_tid': q.message.message_thread_id or 0})
    if field == 'type':
        await q.edit_message_text("Новый тип:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📸 Пост",       callback_data="cpt_post"),
             InlineKeyboardButton("📱 Сторис",     callback_data="cpt_story")],
            [InlineKeyboardButton("🎬 Рилс",       callback_data="cpt_reels"),
             InlineKeyboardButton("🎯 Актуальное", callback_data="cpt_highlight")],
            [InlineKeyboardButton("← Отмена",      callback_data="conv_cancel")],
        ]))
    elif field == 'refs':
        await q.edit_message_text("🖼 Пришли фото или ссылку:", reply_markup=cancel_kb_inline())
    else:
        hints = {'desc': 'Новое описание:', 'date': 'Новая дата (ДД.ММ.ГГГГ):'}
        await q.edit_message_text(hints.get(field, 'Новое значение:'), reply_markup=cancel_kb_inline())

async def cp_edit_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('cp_edit_id'): return
    q = update.callback_query; await q.answer()
    CP_TYPE_MAP_LOCAL = {'cpt_post': '📸 Пост', 'cpt_story': '📱 Сторис',
                         'cpt_reels': '🎬 Рилс', 'cpt_highlight': '🎯 Актуальное'}
    ct    = CP_TYPE_MAP_LOCAL.get(q.data, q.data); cp_id = context.user_data['cp_edit_id']
    gid   = context.user_data.get('cp_edit_gid', q.message.chat.id)
    tid   = context.user_data.get('cp_edit_tid', 0)
    by    = q.from_user.username or str(q.from_user.id)
    conn  = get_db()
    old   = conn.execute('SELECT content_type FROM content_plan WHERE id=?', (cp_id,)).fetchone()
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
    members = conn.execute('SELECT username, full_name, role FROM members WHERE group_id=? ORDER BY full_name',
                           (r['group_id'],)).fetchall(); conn.close()
    context.user_data['cptask_id'] = cp_id
    context.user_data['cptask_selected'] = set()
    re_map  = {'director': '👑', 'am': '📋', 'executor': '✂️'}
    buttons = [[InlineKeyboardButton(f"☐ {re_map.get(m['role'],'')} {m['full_name'] or m['username']}",
                                     callback_data=f"cptoggle_{m['username']}")]
               for m in members]
    buttons.append([InlineKeyboardButton("✅ Назначить",       callback_data="cpconfirm")])
    buttons.append([InlineKeyboardButton("📌 Без исполнителя", callback_data="cpconfirm_none")])
    await q.edit_message_text(f"👤 Кому назначить?\n_{r['description']}_",
                              parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))

async def cptask_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    username = q.data.replace("cptoggle_", "")
    selected = context.user_data.setdefault('cptask_selected', set())
    cp_id    = context.user_data.get('cptask_id')
    if username in selected: selected.discard(username)
    else: selected.add(username)
    conn    = get_db()
    r       = conn.execute('SELECT * FROM content_plan WHERE id=?', (cp_id,)).fetchone()
    members = conn.execute('SELECT username, full_name, role FROM members WHERE group_id=? ORDER BY full_name',
                           (r['group_id'],)).fetchall(); conn.close()
    re_map  = {'director': '👑', 'am': '📋', 'executor': '✂️'}
    buttons = []
    for m in members:
        check = "✅" if m['username'] in selected else "☐"
        buttons.append([InlineKeyboardButton(f"{check} {re_map.get(m['role'],'')} {m['full_name'] or m['username']}",
                                             callback_data=f"cptoggle_{m['username']}")])
    count   = len(selected)
    buttons.append([InlineKeyboardButton(f"✅ Назначить ({count})" if count else "✅ Назначить",
                                         callback_data="cpconfirm")])
    buttons.append([InlineKeyboardButton("📌 Без исполнителя", callback_data="cpconfirm_none")])
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))

async def cptask_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cp_id    = context.user_data.get('cptask_id')
    selected = context.user_data.get('cptask_selected', set())
    assignee = None if q.data == "cpconfirm_none" else (','.join(sorted(selected)) if selected else None)
    by   = q.from_user.username or str(q.from_user.id)
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
    if assignee: msg += f"\n👤 {' '.join(f'@{u}' for u in assignee.split(','))}"
    if r['plan_date']: msg += f"\n📅 {r['plan_date']}"
    for k in ('cptask_id', 'cptask_selected'): context.user_data.pop(k, None)
    await q.edit_message_text(msg, parse_mode='Markdown',
                              reply_markup=task_action_kb(task_id, 'active'))

async def cp_del_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cp_id = int(q.data.replace("cpDel_", ""))
    conn  = get_db(); r = conn.execute('SELECT description FROM content_plan WHERE id=?', (cp_id,)).fetchone(); conn.close()
    await q.edit_message_text(
        f"Удалить запись #{cp_id}?\n_{(r['description'][:30] if r else '')}_",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"cpDelOk_{cp_id}"),
            InlineKeyboardButton("❌ Отмена",      callback_data="cp_manage_0"),
        ]])
    )

async def cp_del_ok_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    cp_id = int(q.data.replace("cpDelOk_", "")); by = q.from_user.username or str(q.from_user.id)
    conn  = get_db(); r = conn.execute('SELECT * FROM content_plan WHERE id=?', (cp_id,)).fetchone()
    if r:
        conn.execute('DELETE FROM content_plan WHERE id=?', (cp_id,))
        conn.commit()
        log_change(r['group_id'], r['thread_id'], 'content_plan', cp_id, 'удалена',
                   f"{r['content_type']} {r['plan_date']}", None, by)
    conn.close()
    await q.edit_message_text(f"🗑 Запись #{cp_id} удалена",
                              reply_markup=InlineKeyboardMarkup([[
                                  InlineKeyboardButton("← Управление", callback_data="cp_manage_0")
                              ]]))

async def cp_status_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts      = q.data.replace("cpSt_", "").split("_")
    cp_id      = int(parts[0]); new_status = parts[1]
    by         = q.from_user.username or str(q.from_user.id)
    conn = get_db()
    r    = conn.execute('SELECT * FROM content_plan WHERE id=?', (cp_id,)).fetchone()
    if r:
        conn.execute('UPDATE content_plan SET status=? WHERE id=?', (new_status, cp_id))
        conn.commit()
        log_change(r['group_id'], r['thread_id'], 'content_plan', cp_id, 'статус',
                   r['status'], new_status, by)
    conn.close()
    tid = q.message.message_thread_id or 0
    msg, kb = _build_cp_manage_markup(q.message.chat.id, tid, 0)
    await q.edit_message_text(msg, parse_mode='Markdown', reply_markup=kb)


# ── KPI ───────────────────────────────────────────────────────────

async def _send_kpi(chat_id, tid, username, context):
    conn = get_db()
    goals = conn.execute('SELECT * FROM kpi_goals WHERE group_id=? AND thread_id=? ORDER BY metric',
                         (chat_id, tid)).fetchall()
    stats = conn.execute(
        "SELECT assigned_username, COUNT(*) as total, SUM(CASE WHEN status IN ('approved','published') THEN 1 ELSE 0 END) as done_cnt, SUM(CASE WHEN status='submitted' THEN 1 ELSE 0 END) as sub_cnt, SUM(CASE WHEN status IN ('active','in_progress','revision') THEN 1 ELSE 0 END) as act_cnt FROM tasks WHERE group_id=? AND thread_id=? AND assigned_username IS NOT NULL GROUP BY assigned_username ORDER BY done_cnt DESC",
        (chat_id, tid)
    ).fetchall()
    mgr = is_manager(conn, chat_id, username); conn.close()
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
            msg  += f"{bar} *@{s['assigned_username']}*\n   ✅ {done} · 📨 {s['sub_cnt'] or 0} · 🔄 {s['act_cnt'] or 0} ({pct}%)\n\n"
    buttons = []
    if mgr:
        buttons.append([InlineKeyboardButton("🎯 Установить цели", callback_data="kpi_set_start")])
    await context.bot.send_message(
        chat_id=chat_id, message_thread_id=tid if tid else None,
        text=msg, parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None
    )

async def handle_kpi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_kpi(update.effective_chat.id, get_thread(update), get_uname(update), context)

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
    for p in KPI_PRESETS:
        row.append(InlineKeyboardButton(p, callback_data=f"kpim_{p}"))
        if len(row) == 3: buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton("✍️ Своё название", callback_data="kpim_custom")])
    buttons.append([InlineKeyboardButton("← Отмена", callback_data="conv_cancel")])
    await q.edit_message_text("🎯 *Установить цель*\n\nВыбери метрику:",
                              parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return KPI_METRIC

async def kpi_metric_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "kpim_custom":
        await q.edit_message_text("Напиши название метрики:", reply_markup=cancel_kb_inline())
        return KPI_METRIC
    metric = q.data.replace("kpim_", "")
    context.user_data['kpi_metric'] = metric
    buttons = [
        [InlineKeyboardButton(v, callback_data=f"kpiv_{v}") for v in KPI_VALUES[:4]],
        [InlineKeyboardButton(v, callback_data=f"kpiv_{v}") for v in KPI_VALUES[4:]],
        [InlineKeyboardButton("✍️ Другое число", callback_data="kpiv_custom")],
        [InlineKeyboardButton("← Отмена",        callback_data="conv_cancel")],
    ]
    await q.edit_message_text(f"🎯 *{metric}*\n\nЗначение:",
                              parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
    return KPI_VALUE

async def kpi_metric_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['kpi_metric'] = update.message.text
    gid = context.user_data.get('kpi_gid', update.effective_chat.id)
    tid = context.user_data.get('kpi_tid', 0)
    buttons = [
        [InlineKeyboardButton(v, callback_data=f"kpiv_{v}") for v in KPI_VALUES[:4]],
        [InlineKeyboardButton(v, callback_data=f"kpiv_{v}") for v in KPI_VALUES[4:]],
        [InlineKeyboardButton("✍️ Другое число", callback_data="kpiv_custom")],
        [InlineKeyboardButton("← Отмена",        callback_data="conv_cancel")],
    ]
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text=f"🎯 *{update.message.text}*\n\nЗначение:",
        parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons)
    )
    return KPI_VALUE

async def kpi_value_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "kpiv_custom":
        await q.edit_message_text("Напиши значение:", reply_markup=cancel_kb_inline())
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
    old    = conn.execute('SELECT value FROM kpi_goals WHERE group_id=? AND thread_id=? AND metric=?',
                          (gid, tid, metric)).fetchone()
    conn.execute(
        "INSERT INTO kpi_goals (group_id,thread_id,metric,value,set_by) VALUES (?,?,?,?,?) ON CONFLICT(group_id,thread_id,metric) DO UPDATE SET value=excluded.value, set_by=excluded.set_by, set_at=datetime('now')",
        (gid, tid, metric, value, by)
    )
    conn.commit(); conn.close()
    log_change(gid, tid, 'kpi_goal', 0, metric, old['value'] if old else None, value, by)
    for k in ('kpi_gid','kpi_tid','kpi_metric'): context.user_data.pop(k, None)
    await context.bot.send_message(chat_id=gid, message_thread_id=tid if tid else None,
                                   text=f"✅ Цель: *{metric}* → {value}", parse_mode='Markdown')
    await _send_kpi(gid, tid, by, context)
    return ConversationHandler.END


# ── Project brief ─────────────────────────────────────────────────

async def _send_project(chat_id, tid, context):
    conn  = get_db()
    brief = conn.execute('SELECT * FROM project_brief WHERE group_id=? AND thread_id=?',
                         (chat_id, tid)).fetchone(); conn.close()
    msg = "📁 *О проекте*\n\n"
    if brief:
        for field, label in BRIEF_FIELDS.items():
            if brief[field]: msg += f"*{label}:*\n{brief[field]}\n\n"
        if brief['updated_by']:
            msg += f"_Обновил: @{brief['updated_by']} · {brief['updated_at'][:16]}_\n\n"
    else:
        msg += "Ещё ничего не заполнено.\n\n"
    msg += "Выбери раздел для редактирования:"
    buttons = []
    row = []
    for field, label in BRIEF_FIELDS.items():
        row.append(InlineKeyboardButton(label, callback_data=f"br_{field}"))
        if len(row) == 2: buttons.append(row); row = []
    if row: buttons.append(row)
    await context.bot.send_message(
        chat_id=chat_id, message_thread_id=tid if tid else None,
        text=msg, parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_project(update.effective_chat.id, get_thread(update), context)

async def brief_field_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    field = q.data.replace("br_", "")
    context.user_data.update({'brief_field': field, 'brief_gid': q.message.chat.id,
                              'brief_tid': q.message.message_thread_id or 0})
    await q.edit_message_text(
        f"✏️ *{BRIEF_FIELDS.get(field, field)}*\n\nНапиши текст:",
        parse_mode='Markdown', reply_markup=cancel_kb_inline()
    )

async def brief_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get('brief_field'); gid = context.user_data.get('brief_gid')
    tid   = context.user_data.get('brief_tid', 0); value = update.message.text; by = get_uname(update)
    conn  = get_db()
    ex    = conn.execute('SELECT * FROM project_brief WHERE group_id=? AND thread_id=?', (gid, tid)).fetchone()
    old_val = ex[field] if ex else None
    if ex:
        conn.execute(f'UPDATE project_brief SET {field}=?,updated_by=?,updated_at=datetime("now") WHERE group_id=? AND thread_id=?', (value, by, gid, tid))
    else:
        conn.execute(f'INSERT INTO project_brief (group_id,thread_id,{field},updated_by) VALUES (?,?,?,?)', (gid, tid, value, by))
    conn.commit(); conn.close()
    log_change(gid, tid, 'brief', 0, BRIEF_FIELDS.get(field, field), old_val, value, by)
    for k in ('brief_field','brief_gid','brief_tid'): context.user_data.pop(k, None)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text=f"✅ *{BRIEF_FIELDS.get(field, field)}* обновлено!",
        parse_mode='Markdown'
    )
    await _send_project(gid, tid, context)


# ── General text / photo handlers ────────────────────────────────

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
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="✅ Обновлено!", reply_markup=main_kb()
    )

async def cp_edit_refs_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('cp_edit_id'): return
    cp_id = context.user_data['cp_edit_id']
    gid   = context.user_data.get('cp_edit_gid', update.effective_chat.id)
    tid   = context.user_data.get('cp_edit_tid', 0); by = get_uname(update)
    value = f"photo:{update.message.photo[-1].file_id}"
    conn  = get_db(); conn.execute('UPDATE content_plan SET refs=? WHERE id=?', (value, cp_id))
    conn.commit(); conn.close()
    log_change(gid, tid, 'content_plan', cp_id, '🖼 Референс', '—', 'фото', by)
    for k in ('cp_edit_id','cp_edit_field','cp_edit_gid','cp_edit_tid'): context.user_data.pop(k, None)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="✅ Фото-референс обновлён!", reply_markup=main_kb()
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('brief_field'):
        await brief_value_received(update, context); return
    if context.user_data.get('cp_edit_id') and context.user_data.get('cp_edit_field') != 'type':
        await cp_edit_value_received(update, context); return

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('cp_edit_id') and context.user_data.get('cp_edit_field') == 'refs':
        await cp_edit_refs_photo(update, context)

async def cancel_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    gid = update.effective_chat.id; tid = get_thread(update)
    await context.bot.send_message(
        chat_id=gid, message_thread_id=tid if tid else None,
        text="❌ Отменено", reply_markup=main_kb()
    )


# ── main ─────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    _cancel_fb = [
        CommandHandler("cancel", cancel),
        MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"), cancel),
        CallbackQueryHandler(cancel_conv_cb, pattern="^conv_cancel$"),
    ]

    # Task creation: entry via "➕ Задача" keyboard button
    task_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{BTN_TASK}$"), task_start)],
        states={
            T_TYPE: [MessageHandler(
                filters.Regex("^(🎬 Съемка|📢 Публикация|🎨 Дизайн|✂️ Монтаж|📌 Другое)$"),
                t_type_chosen
            )],
            T_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_desc_received)],
            T_REFS: [
                MessageHandler(filters.PHOTO, t_refs_photo),
                MessageHandler(filters.Regex(f"^{BTN_SKIP}$"), t_refs_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, t_refs_text),
            ],
            T_DATE: [
                MessageHandler(filters.Regex(f"^{BTN_NO_DATE}$"), t_date_skip_kb),
                MessageHandler(filters.TEXT & ~filters.COMMAND, t_date_text),
            ],
            T_ASSIGNEE: [
                CallbackQueryHandler(t_toggle_assignee,   pattern="^mtoggle_"),
                CallbackQueryHandler(t_confirm_assignees, pattern="^mconfirm$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, t_assignee_text),
            ],
        },
        fallbacks=_cancel_fb,
        per_message=False, allow_reentry=True,
    )

    # Content plan creation: entry via inline "➕ Добавить" button
    cp_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cp_add_start, pattern="^cp_add$"),
            MessageHandler(filters.Regex(f"^{BTN_CP_ADD}$"), cp_add_start),
        ],
        states={
            CP_TYPE_S: [MessageHandler(
                filters.Regex("^(📸 Пост|📱 Сторис|🎬 Рилс|🎯 Актуальное)$"),
                cp_type_chosen
            )],
            CP_DESC_S: [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_desc_received)],
            CP_REFS_S: [
                MessageHandler(filters.PHOTO, cp_refs_photo),
                MessageHandler(filters.Regex(f"^{BTN_SKIP}$"), cp_refs_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, cp_refs_received),
            ],
            CP_DATE: [
                MessageHandler(filters.Regex(f"^{BTN_NO_DATE}$"), cp_date_skip_kb),
                MessageHandler(filters.TEXT & ~filters.COMMAND, cp_date_text),
            ],
        },
        fallbacks=_cancel_fb,
        per_message=False, allow_reentry=True,
    )

    idea_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(idea_add_start, pattern="^idea_add$"),
            MessageHandler(filters.Regex(f"^{BTN_IDEAS_ADD}$"), idea_add_start),
        ],
        states={
            IDEA_TEXT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, idea_text_received)],
            IDEA_PRIORITY: [CallbackQueryHandler(idea_priority_chosen, pattern="^iprio_")],
        },
        fallbacks=_cancel_fb,
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
        fallbacks=_cancel_fb,
        per_message=False, allow_reentry=True,
    )

    # Commands
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("join",    join))
    app.add_handler(CommandHandler("my",      my_tasks))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("cancel",  cancel_global))

    # Conversations (must be before generic MessageHandlers)
    app.add_handler(task_conv)
    app.add_handler(cp_conv)
    app.add_handler(idea_conv)
    app.add_handler(kpi_conv)

    # Main keyboard section handlers
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_LIST}$"),    handle_list))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_IDEAS}$"),   handle_ideas))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_CONTENT}$"),    handle_content))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_KPI}$"),        handle_kpi))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_PROJECT}$"),    handle_project))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"),     cancel_global))
    # Sub-section keyboard handlers
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_LIST_MINE}$"),  handle_list_mine))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_LIST_GIVEN}$"), handle_list_given))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_CP_MANAGE}$"),  handle_cp_manage))

    # Inline callbacks
    app.add_handler(CallbackQueryHandler(cancel_conv_cb,  pattern="^conv_cancel$"))
    app.add_handler(CallbackQueryHandler(role_chosen,     pattern="^role_"))
    app.add_handler(CallbackQueryHandler(status_change_cb,  pattern=r"^st_\d+_"))
    app.add_handler(CallbackQueryHandler(tlist_mine_cb,    pattern="^tlist_mine$"))
    app.add_handler(CallbackQueryHandler(tlist_given_cb,   pattern="^tlist_given$"))
    app.add_handler(CallbackQueryHandler(lmine_action_cb,  pattern=r"^lmst_\d+_"))
    app.add_handler(CallbackQueryHandler(lgiven_action_cb, pattern=r"^lgst_\d+_"))
    app.add_handler(CallbackQueryHandler(brief_field_chosen, pattern="^br_"))
    app.add_handler(CallbackQueryHandler(mm_content_cb,      pattern="^mm_content$"))
    app.add_handler(CallbackQueryHandler(cp_manage_cb,       pattern=r"^cp_manage_\d"))
    app.add_handler(CallbackQueryHandler(cp_noop_cb,         pattern="^cp_noop$"))
    app.add_handler(CallbackQueryHandler(cp_edit_cb,         pattern="^cpEdit_"))
    app.add_handler(CallbackQueryHandler(cp_edit_field_cb,   pattern="^cpEf_"))
    app.add_handler(CallbackQueryHandler(cp_edit_type_cb,    pattern="^cpt_"))
    app.add_handler(CallbackQueryHandler(cp_task_cb,         pattern="^cpTask_"))
    app.add_handler(CallbackQueryHandler(cptask_toggle_cb,   pattern="^cptoggle_"))
    app.add_handler(CallbackQueryHandler(cptask_confirm_cb,  pattern="^cpconfirm"))
    app.add_handler(CallbackQueryHandler(cp_del_cb,          pattern="^cpDel_"))
    app.add_handler(CallbackQueryHandler(cp_del_ok_cb,       pattern="^cpDelOk_"))
    app.add_handler(CallbackQueryHandler(cp_status_cb,       pattern="^cpSt_"))
    # Photos & generic text
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("✅ WhyNot бот v12 запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
