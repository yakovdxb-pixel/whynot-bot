"""
WHY NOT? OS — Telegram bot (@whynotagencybot).

Everything works against Postgres (db.models); the Mini App is the main UI.
The old sqlite "Agency" bot (reply-keyboard menus, /join, /my, KPI, brief…)
was removed in 2026-10 — its data lived in a container-local agency.db that
was wiped on every deploy.
"""
import logging
import os
import re
from datetime import timezone, timedelta
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ForceReply,
    ReplyKeyboardRemove, WebAppInfo, MenuButtonWebApp, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters, CallbackQueryHandler, MessageReactionHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN = os.getenv('BOT_TOKEN')
# the team works in Tashkent: times in messages are Tashkent time (DB stores UTC)
_TASHKENT = timezone(timedelta(hours=5))
WEBAPP_URL = os.getenv("WEBAPP_URL") or "https://worker-production-7137.up.railway.app/webapp"


# ── /start ──────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    if chat.type in ['group', 'supergroup']:
        # Also clears the old sqlite-bot reply keyboard left in some groups.
        await msg.reply_text(
            "👋 Я бот WHY NOT? OS.\n\n"
            "Привязать эту тему к проекту: /bind\n"
            "Файлы для задач присылайте прямо в привязанную тему.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    role = await _pg_role(update.effective_user.id)
    if not role:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])
        await msg.reply_text(
            "👋 *WHY NOT? OS*\n\nЗадачи, контент-план и команда — в приложении 👇",
            parse_mode='Markdown', reply_markup=kb,
        )
        return
    if role in MANAGER_ROLES:
        hint = ("Планы, задачи, сроки и загрузка команды — в приложении.\n"
                "Здесь — проверка работ: «👀 На проверке» покажет присланные файлы "
                "с кнопками «✅ Принять / 🔄 Правки».")
    else:
        hint = ("Твои задачи и сроки — в приложении.\n"
                "Готово? Жми «📤 Сдать работу», выбери задачу и пришли файл или ссылку — "
                "AM проверит его в теме проекта.")
    await msg.reply_text(f"👋 WHY NOT? OS\n\n{hint}", reply_markup=_menu_kb(role))


async def _pg_role(telegram_id: int):
    """Look up a user's WHY NOT? OS (Postgres) role. None if not registered."""
    try:
        from db.models import AsyncSessionLocal, User
        from sqlalchemy import select
        async with AsyncSessionLocal() as s:
            row = (await s.execute(
                select(User.role, User.is_active).where(User.telegram_id == telegram_id)
            )).first()
        if row and row[1]:
            return row[0]
    except Exception as e:
        logger.warning(f"_pg_role failed: {e}")
    return None


# ✍ is the documented "→ references" reaction (free set); 🏆 and the old Premium-only
# 📌 📎 ⭐ ✅ keep working too. ✍ comes in two encodings (with/without U+FE0F).
REF_EMOJI = {"✍", "✍️", "🏆", "📌", "📎", "⭐", "✅"}


async def group_media_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remember photos/files posted in group topics so a reaction can pick them as references."""
    msg = update.effective_message
    if not msg or update.effective_chat.type not in ("group", "supergroup"):
        return
    fid = kind = mime = fname = None
    if msg.photo:
        fid, kind = msg.photo[-1].file_id, "photo"
    elif msg.document:
        d = msg.document
        fid, kind, mime, fname = d.file_id, "document", d.mime_type, d.file_name
    if not fid:
        return
    try:
        from db.models import AsyncSessionLocal, GroupMedia
        from sqlalchemy import select
        async with AsyncSessionLocal() as s:
            exists = (await s.execute(select(GroupMedia.id).where(
                GroupMedia.chat_id == update.effective_chat.id,
                GroupMedia.message_id == msg.message_id))).scalar_one_or_none()
            if exists:
                return
            s.add(GroupMedia(chat_id=update.effective_chat.id, message_id=msg.message_id,
                             thread_id=getattr(msg, "message_thread_id", None),
                             tg_file_id=fid, kind=kind, mime=mime, file_name=fname))
            await s.commit()
    except Exception as e:
        logger.warning(f"group_media_log: {e}")


async def reaction_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """React to a group photo/file with ✍ (also 🏆, 📌, 📎, ⭐, ✅) → it lands in the project's references. Un-react removes it."""
    r = update.message_reaction
    if not r or not r.chat:
        return
    logger.info("reaction msg=%s old=%s new=%s", r.message_id,
                [getattr(e, "emoji", None) or e.type for e in (r.old_reaction or [])],
                [getattr(e, "emoji", None) or e.type for e in (r.new_reaction or [])])
    old = {e.emoji for e in (r.old_reaction or []) if getattr(e, "emoji", None)}
    new = {e.emoji for e in (r.new_reaction or []) if getattr(e, "emoji", None)}
    added = (new & REF_EMOJI) - old
    removed = (old & REF_EMOJI) - new
    if not (added or removed):
        return
    actor = r.user.id if r.user else None
    if actor and await _pg_role(actor) not in ("admin", "am", "director"):
        return
    try:
        from db.models import (AsyncSessionLocal, GroupMedia, ProjectChat,
                               ReferenceItem, User)
        from sqlalchemy import select, delete as sa_delete
        async with AsyncSessionLocal() as s:
            gm = (await s.execute(select(GroupMedia).where(
                GroupMedia.chat_id == r.chat.id,
                GroupMedia.message_id == r.message_id))).scalar_one_or_none()
            if not gm:
                return
            proj = (await s.execute(select(ProjectChat.project_id).where(
                ProjectChat.chat_id == r.chat.id,
                ProjectChat.thread_id == (gm.thread_id or None)))).scalar_one_or_none()
            if not proj:
                proj = (await s.execute(select(ProjectChat.project_id).where(
                    ProjectChat.chat_id == r.chat.id))).scalar_one_or_none()
            if not proj:
                return
            uid = (await s.execute(select(User.id).where(User.telegram_id == actor))).scalar_one_or_none()
            existing = (await s.execute(select(ReferenceItem).where(
                ReferenceItem.project_id == proj,
                ReferenceItem.tg_chat_id == r.chat.id,
                ReferenceItem.tg_message_id == r.message_id))).scalar_one_or_none()
            if added and not existing:
                s.add(ReferenceItem(project_id=proj, kind="file", tg_file_id=gm.tg_file_id,
                                    mime=gm.mime, file_name=gm.file_name or "фото",
                                    tg_chat_id=r.chat.id, tg_message_id=r.message_id,
                                    added_by=uid))
                await s.commit()
            elif removed and existing:
                await s.execute(sa_delete(ReferenceItem).where(ReferenceItem.id == existing.id))
                await s.commit()
    except Exception as e:
        logger.warning(f"reaction_ref: {e}")


SUBMIT_STATUSES = ("pending", "in_progress", "revision", "overdue", "review")   # a new file = new version


async def submit_file_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A photo/video/document dropped in a project's bound topic by someone with tasks there:
    one open task → attached right away as the next version; several → «к какой задаче?»
    (only their own). No own task here (raw footage, an AM sharing something) → silence;
    the ✍ reaction still saves it to references. An album asks once."""
    msg = update.effective_message
    if not msg or update.effective_chat.type not in ("group", "supergroup"):
        return
    fid = ftype = mime = fname = None
    if msg.photo:
        fid, ftype = msg.photo[-1].file_id, "photo"
    elif msg.video:
        fid, ftype, mime, fname = msg.video.file_id, "video", msg.video.mime_type, msg.video.file_name
    elif msg.document:
        d = msg.document
        fid, ftype, mime, fname = d.file_id, "document", d.mime_type, d.file_name
    if not fid:
        return
    # an album = several updates with one media_group_id: only its first file is handled
    if msg.media_group_id:
        seen = context.chat_data.setdefault("albums", {})
        if msg.media_group_id in seen:
            return
        seen[msg.media_group_id] = True
        if len(seen) > 200:
            seen.pop(next(iter(seen)))

    thread_id = getattr(msg, "message_thread_id", None) or 0
    try:
        from db.models import AsyncSessionLocal, Task, TaskAssignee, User
        from sqlalchemy import select, or_
        async with AsyncSessionLocal() as s:
            proj_id = await _topic_project(s, msg.chat_id, thread_id)
            if not proj_id:
                return  # chat/topic isn't bound to a project (see /bind) — nothing to do
            me = (await s.execute(select(User.id).where(
                User.telegram_id == update.effective_user.id))).scalar_one_or_none()
            if not me:
                return
            mine = select(TaskAssignee.task_id).where(TaskAssignee.user_id == me)
            tasks = (await s.execute(select(Task.id, Task.title).where(
                Task.project_id == proj_id, Task.status.in_(SUBMIT_STATUSES),
                or_(Task.assignee_id == me, Task.id.in_(mine)))
                .order_by(Task.priority != "urgent", Task.deadline.is_(None), Task.deadline)
                .limit(20))).all()
    except Exception as e:
        logger.warning(f"submit_file_prompt: {e}")
        return
    if not tasks:
        return

    pending = {"file_id": fid, "file_type": ftype, "from_user": update.effective_user.id,
               "project_id": proj_id, "mime": mime, "file_name": fname,
               "caption": (msg.caption or "").strip(), "chat_id": msg.chat_id}
    if len(tasks) == 1:
        res = await _submit_from_group(context, tasks[0][0], pending)
        if res:
            task, version, prev = res
            context.chat_data[f"undo_{task.id}"] = {**prev, "by": update.effective_user.id}
            await msg.reply_text(f"📎 Версия {version} · «{task.title}» — на проверке\n"
                                 f"AM: смотри файл выше и жми кнопку 👇",
                                 message_thread_id=thread_id or None,
                                 reply_markup=_review_kb(task.id, undo=True))
        return

    # several tasks: which one? (the callback can't carry the file — 64-byte limit)
    context.chat_data[f"subfile_{msg.message_id}"] = pending
    kb = [[InlineKeyboardButton(f"📋 {title}"[:64], callback_data=f"subfile_{msg.message_id}_{tid}")]
          for tid, title in tasks]
    kb.append([InlineKeyboardButton("✖ Это не сдача", callback_data=f"subskip_{msg.message_id}")])
    await msg.reply_text("📎 К какой задаче этот файл?", message_thread_id=thread_id or None,
                         reply_markup=InlineKeyboardMarkup(kb))


async def _submit_from_group(context, task_id, pending):
    """Attach a file posted in the topic to a task as its next version (status → review),
    tell the AM in charge, offer the executor their next task. Returns (task, version, prev)
    where prev lets «✖ Это не сдача» put things back."""
    try:
        from db.models import AsyncSessionLocal, Task, User, Project, Client
        from sqlalchemy import select
        from datetime import datetime as _dt, timezone as _tz
        async with AsyncSessionLocal() as s:
            task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
            if not task:
                return None
            prev = {"status": task.status, "file_id": task.file_id, "file_type": task.file_type,
                    "submitted_at": task.submitted_at}
            task.file_id, task.file_type = pending["file_id"], pending["file_type"]
            task.status = "review"
            task.submitted_at = _dt.now(_tz.utc)
            await s.commit()
            await _log_task_status(s, task.id, "review", pending["from_user"])
            version = await _task_version(s, task.id)
            who = (await s.execute(select(User.full_name).where(
                User.telegram_id == pending["from_user"]))).scalar_one_or_none() or "Кто-то"
            cname = None
            if task.project_id:
                cname = (await s.execute(select(Client.name).join(Project, Project.client_id == Client.id)
                         .where(Project.id == task.project_id))).scalar_one_or_none()
            for tg in await _project_am_tgs(s, task.project_id):
                if tg == pending["from_user"]:
                    continue
                try:
                    await context.bot.send_message(
                        tg, f"📎 {who} сдал «{task.title}»" + (f" по клиенту {cname}" if cname else "")
                        + f" — версия {version}. Файл в теме проекта.", reply_markup=_review_kb(task.id))
                except Exception:
                    pass
            await _suggest_next(context, s, task)
            return task, version, prev
    except Exception as e:
        logger.warning(f"_submit_from_group: {e}")
        return None


async def subfile_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The executor picked which of their tasks the file is for."""
    q = update.callback_query
    m = re.match(r"^subfile_(\d+)_(\d+)$", q.data or "")
    if not m:
        await q.answer()
        return
    orig_msg_id, task_id = int(m.group(1)), int(m.group(2))
    pending = context.chat_data.get(f"subfile_{orig_msg_id}")
    if pending and q.from_user.id != pending["from_user"]:
        await q.answer("Выбирает тот, кто прислал файл", show_alert=True)
        return
    await q.answer()
    context.chat_data.pop(f"subfile_{orig_msg_id}", None)
    if not pending:
        await q.edit_message_text("⌛ Файл потерян (бот перезапускался) — отправь его ещё раз.")
        return
    res = await _submit_from_group(context, task_id, pending)
    if not res:
        await q.edit_message_text("Задача не найдена (возможно, удалена).")
        return
    task, version, prev = res
    context.chat_data[f"undo_{task.id}"] = {**prev, "by": q.from_user.id}
    await q.edit_message_text(f"📎 Версия {version} · «{task.title}» — на проверке\n"
                              f"AM: смотри файл выше и жми кнопку 👇",
                              reply_markup=_review_kb(task.id, undo=True))


async def subfile_skip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«✖ Это не сдача» under the «к какой задаче?» question."""
    q = update.callback_query
    mid = int(q.data.split("_")[1])
    pending = context.chat_data.get(f"subfile_{mid}")
    if pending and q.from_user.id != pending["from_user"] and await _pg_role(q.from_user.id) not in MANAGER_ROLES:
        await q.answer("Это решает тот, кто прислал файл", show_alert=True)
        return
    context.chat_data.pop(f"subfile_{mid}", None)
    await q.answer("Ок")
    try:
        await q.message.delete()
    except Exception:
        await q.edit_message_text("✖ Не сдача — файл просто остался в теме.")


async def subfile_undo_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«✖ Это не сдача» under an auto-attached file: put the task back as it was."""
    q = update.callback_query
    tid = int(q.data.split("_")[1])
    prev = context.chat_data.get(f"undo_{tid}")
    if not prev:
        await q.answer("Уже нельзя отменить — поправь в приложении", show_alert=True)
        return
    if q.from_user.id != prev["by"]:
        await q.answer("Отменить может тот, кто прислал файл", show_alert=True)
        return
    try:
        from db.models import AsyncSessionLocal, Task, StatusEvent
        from sqlalchemy import select, delete as sa_delete
        async with AsyncSessionLocal() as s:
            task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one_or_none()
            if not task or task.status != "review":
                await q.answer("AM уже посмотрел — поправь в приложении", show_alert=True)
                return
            task.status, task.file_id, task.file_type, task.submitted_at = (
                prev["status"], prev["file_id"], prev["file_type"], prev["submitted_at"])
            last = (await s.execute(select(StatusEvent.id).where(
                StatusEvent.entity == "task", StatusEvent.entity_id == tid, StatusEvent.status == "review")
                .order_by(StatusEvent.created_at.desc()).limit(1))).scalar_one_or_none()
            if last:      # not a version after all
                await s.execute(sa_delete(StatusEvent).where(StatusEvent.id == last))
            await s.commit()
    except Exception as e:
        logger.warning(f"subfile_undo_cb: {e}")
        await q.answer("Не получилось, попробуй ещё раз", show_alert=True)
        return
    context.chat_data.pop(f"undo_{tid}", None)
    await q.answer("Отменено")
    try:
        await q.message.delete()
    except Exception:
        await q.edit_message_text("✖ Не сдача — задача как была.")


async def subfile_keep_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """File with no fitting task: 📌 save it to the project's references, or
    ➕ create a new task (caption = title) with the file attached, status «на проверке»."""
    q = update.callback_query
    await q.answer()
    m = re.match(r"^sub(ref|new)_(\d+)$", q.data or "")
    if not m:
        return
    action, orig_msg_id = m.group(1), int(m.group(2))
    pending = context.chat_data.pop(f"subfile_{orig_msg_id}", None)
    if not pending:
        await q.edit_message_text("⌛ Файл потерян (бот перезапускался) — отправь его ещё раз.")
        return
    try:
        from db.models import AsyncSessionLocal, ReferenceItem, Task, TaskAssignee, User
        from sqlalchemy import select
        from datetime import datetime as _dt, timezone as _tz
        async with AsyncSessionLocal() as s:
            uid = (await s.execute(select(User.id).where(
                User.telegram_id == pending["from_user"]))).scalar_one_or_none()
            default_name = {"photo": "фото", "video": "видео"}.get(pending["file_type"], "файл")
            if action == "ref":
                s.add(ReferenceItem(project_id=pending["project_id"], kind="file",
                                    tg_file_id=pending["file_id"], mime=pending["mime"],
                                    file_name=pending["file_name"] or default_name,
                                    tg_chat_id=pending["chat_id"], tg_message_id=orig_msg_id,
                                    added_by=uid))
                await s.commit()
                await q.edit_message_text("📌 Файл сохранён в референсы проекта.")
                return
            title = (pending["caption"].split("\n")[0][:120]
                     or f"{default_name.capitalize()} из темы ({_dt.now().strftime('%d.%m')})")
            task = Task(title=title, project_id=pending["project_id"], status="review",
                        assignee_id=uid, created_by=uid, file_id=pending["file_id"],
                        file_type=pending["file_type"], submitted_at=_dt.now(_tz.utc))
            s.add(task)
            await s.flush()
            if uid:
                s.add(TaskAssignee(task_id=task.id, user_id=uid))
            await s.commit()
            await _log_task_status(s, task.id, "review", pending["from_user"])
        await q.edit_message_text(
            f"✅ Создана задача «{title}» с этим файлом. Версия 1 — на проверке 📎\n"
            f"Название и срок можно поправить в приложении.",
            reply_markup=_review_kb(task.id))
    except Exception as e:
        logger.warning(f"subfile_keep_cb: {e}")
        await q.edit_message_text("Что-то пошло не так, попробуй ещё раз.")


# ── AM review right in the group: ✅ Принять / 🔄 Правки under a submitted file ──
# Watching a video is easier in Telegram than in the Mini App, so the verdict is given
# here and lands in the app. Every file handed in = a new version (count of «review»
# events). «🔄 Правки» collects the AM's replies one by one; «📨 Отправить» sends them
# to the executor as ONE message and records one revision (status_events → counter).

MANAGER_ROLES = ("admin", "am", "director")


def _review_kb(task_id, client=False, undo=False):
    if client:   # the AM sent it to the client and waits for their answer
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Клиент принял", callback_data=f"rvok_{task_id}"),
            InlineKeyboardButton("🔥 Правки клиента", callback_data=f"rvfix_{task_id}"),
        ]])
    rows = [
        [InlineKeyboardButton("✅ Принять", callback_data=f"rvok_{task_id}"),
         InlineKeyboardButton("🔄 Правки", callback_data=f"rvfix_{task_id}")],
        [InlineKeyboardButton("🕓 На утверждении у клиента", callback_data=f"rvcli_{task_id}")],
    ]
    if undo:   # for the executor: the file was attached automatically — maybe it isn't a hand-in
        rows.append([InlineKeyboardButton("✖ Это не сдача", callback_data=f"subundo_{task_id}")])
    return InlineKeyboardMarkup(rows)


def _eta_kb(task_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Беру сейчас", callback_data=f"etanow_{task_id}"),
        InlineKeyboardButton("⏰ Укажу время", callback_data=f"etaset_{task_id}"),
    ]])


def _collect_kb(n):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📨 Отправить исполнителю ({n})", callback_data="rvsend"),
        InlineKeyboardButton("✖ Отмена", callback_data="rvcancel"),
    ]])


async def _log_task_status(s, task_id, status, actor_tg):
    """Append to status_events like the API's _log_status (best-effort)."""
    try:
        from db.models import StatusEvent, User
        from sqlalchemy import select
        uid = (await s.execute(select(User.id).where(User.telegram_id == actor_tg))).scalar_one_or_none()
        s.add(StatusEvent(entity="task", entity_id=task_id, status=status, actor_id=uid))
        await s.commit()
    except Exception as e:
        await s.rollback()
        logger.warning(f"status log failed (task/{task_id}): {e}")


async def _task_version(s, task_id):
    """Version of the work = how many times it was handed in for review."""
    from db.models import StatusEvent
    from sqlalchemy import select, func
    n = (await s.execute(select(func.count()).select_from(StatusEvent).where(
        StatusEvent.entity == "task", StatusEvent.entity_id == task_id,
        StatusEvent.status == "review"))).scalar_one()
    return max(n, 1)


class _Bg:
    """Stand-in for FastAPI BackgroundTasks so API helpers can be reused here."""
    def __init__(self, app):
        self.app = app

    def add_task(self, fn, *args, **kwargs):
        self.app.create_task(fn(*args, **kwargs))


async def _assignee_tgs(s, task):
    from db.models import TaskAssignee, User
    from sqlalchemy import select
    uids = list((await s.execute(select(TaskAssignee.user_id)
                                 .where(TaskAssignee.task_id == task.id))).scalars().all())
    if not uids and task.assignee_id:
        uids = [task.assignee_id]
    if not uids:
        return []
    return list((await s.execute(select(User.telegram_id).where(
        User.id.in_(uids), User.telegram_id.isnot(None)))).scalars().all())


async def _dm_assignees(context, s, task, text, kb=None):
    for tg in await _assignee_tgs(s, task):
        try:
            await context.bot.send_message(tg, text, reply_markup=kb)
        except Exception:
            pass   # never pressed Start — Telegram won't let the bot write first


def _thread_of(msg):
    return msg.message_thread_id if getattr(msg, "is_topic_message", False) else None


async def _edit(q, text, reply_markup=None):
    """Edit the pressed message: text for text posts, caption for photo/video/file posts."""
    m = q.message
    if m.photo or m.video or m.document or m.caption is not None:
        await q.edit_message_caption(text[:1024], reply_markup=reply_markup)
    else:
        await q.edit_message_text(text, reply_markup=reply_markup)


async def review_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """✅ Принять / 🔄 Правки / 🕓 На утверждении under a submitted file (AM / director / admin);
    once at the client: ✅ Клиент принял / 🔥 Правки клиента."""
    q = update.callback_query
    m = re.match(r"^rv(ok|fix|cli)_(\d+)$", q.data or "")
    if not m:
        await q.answer()
        return
    action, task_id = m.group(1), int(m.group(2))
    if await _pg_role(q.from_user.id) not in MANAGER_ROLES:
        await q.answer("Принять или отправить на правку может только AM", show_alert=True)
        return
    try:
        from db.models import AsyncSessionLocal, Task, User
        from sqlalchemy import select
        from datetime import datetime as _dt, timezone as _tz
        async with AsyncSessionLocal() as s:
            task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
            if not task:
                await q.answer("Задача не найдена (возможно, удалена)", show_alert=True)
                await q.edit_message_reply_markup(None)
                return
            if task.status not in ("review", "client") or (action == "cli" and task.status != "review"):
                await q.answer("Уже решено — смотри статус в приложении", show_alert=True)
                await q.edit_message_reply_markup(None)
                return
            at_client = task.status == "client"
            version = await _task_version(s, task.id)
            am = (await s.execute(select(User.full_name).where(
                User.telegram_id == q.from_user.id))).scalar_one_or_none() or q.from_user.first_name
            if action == "ok":
                now = _dt.now(_tz.utc)
                task.status = "done"
                if task.actual_completion is None:
                    task.actual_completion = now
                task.updated_at = now
                await s.commit()
                await _log_task_status(s, task.id, "done", q.from_user.id)
                await q.answer("Принято ✅")
                who_ok = "клиент принял" if at_client else "принята"
                await _edit(q, f"✅ Версия {version} {who_ok} — «{task.title}» ({am})")
                await _dm_assignees(context, s, task, f"✅ {'Клиент принял' if at_client else 'Принято'}! "
                                                      f"Задача «{task.title}» (версия {version})")
                try:   # all jobs of the content done -> content goes to the AM for approval
                    from routes_whynot import _advance_chain
                    await _advance_chain(s, _Bg(context.application), task)
                except Exception as e:
                    logger.warning(f"review_cb advance: {e}")
                return
            if action == "cli":
                task.status = "client"
                task.client_nudged_at = None
                task.updated_at = _dt.now(_tz.utc)
                await s.commit()
                await _log_task_status(s, task.id, "client", q.from_user.id)
                await q.answer("Ждём ответа клиента")
                await _edit(q, f"🕓 Версия {version} «{task.title}» — на утверждении у клиента ({am}).\n"
                               f"Ответил клиент — жми кнопку 👇", reply_markup=_review_kb(task.id, client=True))
                await _dm_assignees(context, s, task,
                                    f"🕓 «{task.title}» (версия {version}) — у клиента на утверждении. "
                                    f"Пока ждём ответа, ты свободен.")
                await _suggest_next(context, s, task)
                return
            title = task.title
    except Exception as e:
        logger.warning(f"review_cb: {e}")
        await q.answer("Что-то пошло не так, попробуй ещё раз", show_alert=True)
        return
    # 🔄 Правки: start collecting the AM's notes as replies to one message
    await q.answer()
    msg = q.message
    kind = "правки клиента" if at_client else "правки"
    await _edit(q, f"🔄 Версия {version} · «{title}» — {am} пишет {kind}…")
    from html import escape
    who = f'<a href="tg://user?id={q.from_user.id}">{escape(q.from_user.first_name or "AM")}</a>'
    ask = await context.bot.send_message(
        msg.chat_id,
        f"✍️ {who}, {kind} к версии {version} «{escape(title)}»: отвечай на это сообщение — "
        f"по одной правке в ответе. Когда всё — жми «📨 Отправить», исполнитель получит их одним сообщением.",
        parse_mode="HTML", message_thread_id=_thread_of(msg),
        reply_markup=ForceReply(selective=True, input_field_placeholder="Что поправить?"))
    # ForceReply and inline buttons can't share a message — the buttons go on a second one
    ctl = await context.bot.send_message(msg.chat_id, "Правок пока нет.",
                                         message_thread_id=_thread_of(msg), reply_markup=_collect_kb(0))
    context.chat_data[f"rvask_{ask.message_id}"] = {
        "task": task_id, "version": version, "items": [], "ctl": ctl.message_id, "am": q.from_user.id,
        "client": at_client}
    context.chat_data[f"rvctl_{ctl.message_id}"] = ask.message_id


async def revision_comment_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A reply to «правки к версии N» = one revision note, collected until «📨 Отправить»."""
    msg = update.effective_message
    if not msg or not msg.reply_to_message or not msg.text:
        return
    st = context.chat_data.get(f"rvask_{msg.reply_to_message.message_id}")
    if not st or await _pg_role(update.effective_user.id) not in MANAGER_ROLES:
        return
    st["items"].append(msg.text.strip()[:1000])
    st.setdefault("msgs", []).append(msg.message_id)
    n = len(st["items"])
    lines = "\n".join(f"{i}. {t}" for i, t in enumerate(st["items"], 1))
    try:
        await context.bot.edit_message_text(f"Правки ({n}):\n{lines}", chat_id=msg.chat_id,
                                            message_id=st["ctl"], reply_markup=_collect_kb(n))
    except Exception as e:
        logger.warning(f"revision list update: {e}")


async def _tidy_revision(context, chat_id, ask_id, st):
    """The notes now live in one summary message — remove the prompt and the single replies."""
    for mid in [ask_id] + st.get("msgs", []):
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception:
            pass   # no rights / too old — harmless


async def revision_send_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📨 Отправить / ✖ Отмена under the collected revision notes."""
    q = update.callback_query
    if await _pg_role(q.from_user.id) not in MANAGER_ROLES:
        await q.answer("Это может только AM", show_alert=True)
        return
    ask_id = context.chat_data.get(f"rvctl_{q.message.message_id}")
    st = context.chat_data.get(f"rvask_{ask_id}") if ask_id else None
    if not st:
        await q.answer("⌛ Бот перезапускался — нажми «🔄 Правки» под файлом ещё раз", show_alert=True)
        await q.edit_message_reply_markup(None)
        return
    if q.data == "rvcancel":
        context.chat_data.pop(f"rvask_{ask_id}", None)
        context.chat_data.pop(f"rvctl_{q.message.message_id}", None)
        await _tidy_revision(context, q.message.chat_id, ask_id, st)
        await q.answer("Отменено")
        await q.edit_message_text("✖ Правки отменены — работа всё ещё " +
                                  ("у клиента." if st.get("client") else "на проверке."),
                                  reply_markup=_review_kb(st["task"], client=st.get("client")))
        return
    if not st["items"]:
        await q.answer("Сначала напиши хотя бы одну правку ответом на сообщение выше", show_alert=True)
        return
    lines = "\n".join(f"{i}. {t}" for i, t in enumerate(st["items"], 1))
    try:
        from db.models import AsyncSessionLocal, Task, StatusEvent
        from sqlalchemy import select, func
        from datetime import datetime as _dt, timezone as _tz
        async with AsyncSessionLocal() as s:
            task = (await s.execute(select(Task).where(Task.id == st["task"]))).scalar_one_or_none()
            if not task:
                await q.answer("Задача не найдена", show_alert=True)
                return
            task.status = "revision"
            client = st.get("client")
            head = "🔥 Правки клиента" if client else "🔄 Правки"
            task.review_comment = f"{head} к версии {st['version']}:\n{lines}"
            task.eta_at = task.eta_notified_at = None
            if client:
                task.priority = "urgent"     # client is waiting: goes first everywhere
            task.updated_at = _dt.now(_tz.utc)
            await s.commit()
            await _log_task_status(s, task.id, "revision", q.from_user.id)
            n = (await s.execute(select(func.count()).select_from(StatusEvent).where(
                StatusEvent.entity == "task", StatusEvent.entity_id == task.id,
                StatusEvent.status == "revision"))).scalar_one()
            await _dm_assignees(context, s, task,
                                f"{'🔥 Срочно: правки клиента' if client else '🔄 Правки'} к версии "
                                f"{st['version']} — «{task.title}»:\n{lines}\n\n"
                                f"Когда возьмёшься? AM увидит твой ответ.", kb=_eta_kb(task.id))
            title = task.title
    except Exception as e:
        logger.warning(f"revision_send_cb: {e}")
        await q.answer("Что-то пошло не так, попробуй ещё раз", show_alert=True)
        return
    context.chat_data.pop(f"rvask_{ask_id}", None)
    context.chat_data.pop(f"rvctl_{q.message.message_id}", None)
    await _tidy_revision(context, q.message.chat_id, ask_id, st)
    await q.answer("Отправлено исполнителю")
    await q.edit_message_text(f"🔄 Правка №{n} · версия {st['version']} «{title}» — отправлено исполнителю:\n{lines}")


# ── keep the executor busy while the AM / client decide ──────────────────

async def _suggest_next(context, s, task):
    """After a hand-in (or when it went to the client): offer the executor their next task."""
    from db.models import Task, TaskAssignee, User
    from sqlalchemy import select, or_
    uids = list((await s.execute(select(TaskAssignee.user_id)
                                 .where(TaskAssignee.task_id == task.id))).scalars().all()) \
        or ([task.assignee_id] if task.assignee_id else [])
    for uid in uids:
        tg = (await s.execute(select(User.telegram_id).where(User.id == uid))).scalar_one_or_none()
        if not tg:
            continue
        mine = select(TaskAssignee.task_id).where(TaskAssignee.user_id == uid)
        busy = (await s.execute(select(Task.id).where(
            or_(Task.assignee_id == uid, Task.id.in_(mine)), Task.id != task.id,
            Task.status == "in_progress").limit(1))).first()
        if busy:
            continue       # already working on something — don't distract
        nxt = (await s.execute(select(Task).where(
            or_(Task.assignee_id == uid, Task.id.in_(mine)), Task.id != task.id,
            Task.status.in_(("pending", "overdue", "revision", "in_progress")))
            .order_by(Task.priority != "urgent", Task.deadline.is_(None), Task.deadline)
            .limit(1))).scalar_one_or_none()
        seen = context.bot_data.setdefault("nudged", {})
        if seen.get(uid) == (nxt.id if nxt else 0):
            continue       # already offered exactly this
        seen[uid] = nxt.id if nxt else 0
        try:
            if nxt:
                dl = f" (до {nxt.deadline.astimezone(_TASHKENT).strftime('%d.%m %H:%M')})" if nxt.deadline else ""
                kb = None if nxt.status == "in_progress" else InlineKeyboardMarkup([[
                    InlineKeyboardButton("▶️ Взял в работу", callback_data=f"take_{nxt.id}")]])
                await context.bot.send_message(tg, f"👉 Пока ждём ответа — следующая: «{nxt.title}»{dl}",
                                               reply_markup=kb)
            else:
                await context.bot.send_message(tg, "👌 Других задач нет — напиши AM, что свободен.")
        except Exception:
            pass


async def take_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """▶️ Взял в работу."""
    q = update.callback_query
    tid = int(q.data.split("_")[1])
    from db.models import AsyncSessionLocal, Task
    from sqlalchemy import select
    async with AsyncSessionLocal() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one_or_none()
        if not task or task.status not in ("pending", "overdue"):
            await q.answer("Задача уже в работе или закрыта")
            await q.edit_message_reply_markup(None)
            return
        task.status = "in_progress"
        await s.commit()
        await _log_task_status(s, task.id, "in_progress", q.from_user.id)
    await q.answer("В работе ▶️")
    await q.edit_message_text(f"▶️ В работе: «{task.title}»", reply_markup=_submit_btn(task.id))


async def _tell_revision_author(context, s, task, text):
    """DM whoever sent the last revision (the AM) — e.g. when the executor will take it."""
    from db.models import StatusEvent, User
    from sqlalchemy import select
    tg = (await s.execute(select(User.telegram_id).join(StatusEvent, StatusEvent.actor_id == User.id).where(
        StatusEvent.entity == "task", StatusEvent.entity_id == task.id, StatusEvent.status == "revision")
        .order_by(StatusEvent.created_at.desc()).limit(1))).scalar_one_or_none()
    if tg:
        try:
            await context.bot.send_message(tg, text)
        except Exception:
            pass


async def eta_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """▶️ Беру сейчас / ⏰ Укажу время — the executor's answer to revisions."""
    q = update.callback_query
    action, tid = q.data.split("_")
    tid = int(tid)
    from db.models import AsyncSessionLocal, Task, User
    from sqlalchemy import select
    from datetime import datetime as _dt, timezone as _tz
    async with AsyncSessionLocal() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one_or_none()
        if not task or task.status != "revision":
            await q.answer("Правки уже сданы или задача закрыта")
            await q.edit_message_reply_markup(None)
            return
        if action == "etaset":
            context.user_data["eta_task"] = tid
            await q.answer()
            await q.message.reply_text("Во сколько возьмёшься? Напиши время, например 15:40")
            return
        now = _dt.now(_tz.utc)
        task.eta_at = task.eta_notified_at = now
        await s.commit()
        who = (await s.execute(select(User.full_name).where(
            User.telegram_id == q.from_user.id))).scalar_one_or_none() or q.from_user.first_name
        await _tell_revision_author(context, s, task, f"▶️ {who} взял правки по «{task.title}» сейчас "
                                                      f"({now.astimezone(_TASHKENT).strftime('%H:%M')})")
    await q.answer("AM знает, что ты взялся")
    await q.edit_message_reply_markup(_submit_btn(tid, "📤 Сдать новую версию"))


def _parse_hhmm(text):
    """«15:40» / «15.40» / «1540» → today in Tashkent (tomorrow if already passed)."""
    from datetime import datetime as _dt
    m = re.fullmatch(r"\s*(\d{1,2})[:.\s]?(\d{2})\s*", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    now = _tk_now()
    at = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


async def _eta_answer(update, context, tid):
    msg = update.effective_message
    at = _parse_hhmm(msg.text)
    if not at:
        await msg.reply_text("Не понял время. Напиши так: 15:40")
        return
    context.user_data.pop("eta_task", None)
    from db.models import AsyncSessionLocal, Task, User
    from sqlalchemy import select
    async with AsyncSessionLocal() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one_or_none()
        if not task or task.status != "revision":
            await msg.reply_text("Правки уже сданы или задача закрыта.")
            return
        task.eta_at, task.eta_notified_at = at, None
        await s.commit()
        who = (await s.execute(select(User.full_name).where(
            User.telegram_id == update.effective_user.id))).scalar_one_or_none() or update.effective_user.first_name
        day = "" if at.date() == _tk_now().date() else " завтра"
        await _tell_revision_author(context, s, task, f"⏰ {who} возьмёт правки по «{task.title}»{day} в {at:%H:%M}")
    await msg.reply_text(f"👌 Записал: берёшь{day} в {at:%H:%M}. AM в курсе. Напомню в это время.",
                         reply_markup=_submit_btn(tid, "📤 Сдать новую версию"))


# ── work in Telegram: role keyboard in the bot's private chat ─────────────
# Planning lives in the Mini App; the hand-in / review loop runs here on buttons.
BTN_SUBMIT = "📤 Сдать работу"
BTN_REVIEW = "👀 На проверке"
BTN_APP = "📱 Приложение"
OPEN_STATUSES = ("pending", "in_progress", "revision", "overdue")


def _menu_kb(role):
    from telegram import ReplyKeyboardMarkup, KeyboardButton
    main = BTN_REVIEW if role in MANAGER_ROLES else BTN_SUBMIT
    return ReplyKeyboardMarkup([[KeyboardButton(main),
                                 KeyboardButton(BTN_APP, web_app=WebAppInfo(url=WEBAPP_URL))]],
                               resize_keyboard=True, is_persistent=True)


def _submit_btn(task_id, label="📤 Сдать"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"dmsub_{task_id}")]])


async def submit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«📤 Сдать работу»: pick one of my open tasks."""
    msg = update.effective_message
    try:
        from db.models import AsyncSessionLocal, Task, TaskAssignee, User
        from sqlalchemy import select, or_
        async with AsyncSessionLocal() as s:
            uid = (await s.execute(select(User.id).where(
                User.telegram_id == update.effective_user.id))).scalar_one_or_none()
            if not uid:
                await msg.reply_text("Ты ещё не в команде — открой приложение и зарегистрируйся.")
                return
            mine = select(TaskAssignee.task_id).where(TaskAssignee.user_id == uid)
            tasks = (await s.execute(select(Task).where(
                or_(Task.assignee_id == uid, Task.id.in_(mine)),
                Task.status.in_(OPEN_STATUSES))
                .order_by(Task.priority != "urgent", Task.status != "revision",
                          Task.deadline.is_(None), Task.deadline).limit(20))).scalars().all()
    except Exception as e:
        logger.warning(f"submit_menu: {e}")
        await msg.reply_text("Что-то пошло не так, попробуй ещё раз.")
        return
    if not tasks:
        await msg.reply_text("У тебя нет открытых задач 👌")
        return
    def label(t):
        dl = f" · до {t.deadline.astimezone(_TASHKENT).strftime('%d.%m %H:%M')}" if t.deadline else ""
        mark = ("🔥 " if t.priority == "urgent" and t.status == "revision" else "🔄 " if t.status == "revision"
                else "🔴 " if t.status == "overdue" else "")
        return f"{mark}{t.title}{dl}"[:64]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(label(t), callback_data=f"dmsub_{t.id}")] for t in tasks])
    await msg.reply_text("Какую задачу сдаёшь?", reply_markup=kb)


async def submit_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A task picked for hand-in (from the list, a reminder or the revision message)."""
    q = update.callback_query
    tid = int(q.data.split("_")[1])
    from db.models import AsyncSessionLocal, Task
    from sqlalchemy import select
    async with AsyncSessionLocal() as s:
        task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one_or_none()
    if not task or task.status not in OPEN_STATUSES:
        await q.answer("Эта задача уже сдана или закрыта", show_alert=True)
        return
    if q.message.chat.type != "private":
        # pressed in a group — the file goes to the private chat, not to everyone
        await q.answer("Сдать можно в личке с ботом: «📤 Сдать работу»", show_alert=True)
        return
    await q.answer()
    context.user_data["submit_task"] = tid
    await q.message.reply_text(
        f"📎 «{task.title}»\nПришли сюда файл (фото, видео, документ) или ссылку на работу.")


async def dm_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The file / link for the picked task: → review, posted to the project topic as a new
    version with ✅ Принять / 🔄 Правки for the AM."""
    msg = update.effective_message
    if context.user_data.get("eta_task") and msg.text:
        await _eta_answer(update, context, context.user_data["eta_task"])
        return
    tid = context.user_data.get("submit_task")
    if not tid:
        if not msg.text:   # a file with no task picked
            await msg.reply_text("Сначала выбери задачу: «📤 Сдать работу» внизу.")
        return
    fid = ftype = link = None
    if msg.photo:
        fid, ftype = msg.photo[-1].file_id, "photo"
    elif msg.video:
        fid, ftype = msg.video.file_id, "video"
    elif msg.document:
        fid, ftype = msg.document.file_id, "document"
    elif msg.text:
        m = re.search(r"https?://\S+|\b[\w-]+\.[a-z]{2,}/\S*", msg.text)
        link = m.group(0) if m else None
    if not fid and not link:
        await msg.reply_text("Нужен файл или ссылка. Пришли ещё раз 🙂")
        return
    context.user_data.pop("submit_task", None)
    try:
        from db.models import AsyncSessionLocal, Task, User, ReferenceItem
        from sqlalchemy import select
        from datetime import datetime as _dt, timezone as _tz
        async with AsyncSessionLocal() as s:
            task = (await s.execute(select(Task).where(Task.id == tid))).scalar_one_or_none()
            if not task or task.status not in OPEN_STATUSES:
                await msg.reply_text("Эта задача уже сдана или закрыта.")
                return
            me = (await s.execute(select(User).where(
                User.telegram_id == update.effective_user.id))).scalar_one_or_none()
            if fid:
                task.file_id, task.file_type = fid, ftype
            else:
                s.add(ReferenceItem(task_id=task.id, kind="link",
                                    url=link if "://" in link else "https://" + link,
                                    title="Сдано", added_by=me.id if me else None))
            task.status = "review"
            task.submitted_at = _dt.now(_tz.utc)
            await s.commit()
            await _log_task_status(s, task.id, "review", update.effective_user.id)
            version = await _task_version(s, task.id)
            who = me.full_name if me else update.effective_user.first_name
            posted = await _post_for_review(context, s, task, version, who, fid, ftype, link)
            if posted:   # the file is in the topic; the AM in charge also gets it in private
                await _post_for_review(context, s, task, version, who, fid, ftype, link,
                                       chats=[(tg, None) for tg in await _project_am_tgs(s, task.project_id)
                                              if tg != update.effective_user.id])
    except Exception as e:
        logger.warning(f"dm_submission: {e}")
        await msg.reply_text("Не получилось сдать, попробуй ещё раз.")
        return
    await msg.reply_text(f"✅ Сдано: «{task.title}», версия {version}. "
                         + ("AM проверит в теме проекта." if posted else "AM получил на проверку."))
    try:
        from db.models import AsyncSessionLocal
        async with AsyncSessionLocal() as s:
            await _suggest_next(context, s, task)
    except Exception as e:
        logger.warning(f"suggest_next: {e}")


async def _project_am_tgs(s, project_id):
    """Telegram ids of the AM responsible for the project (project or client AM);
    all managers if nobody is set."""
    from db.models import Project, Client, User
    from sqlalchemy import select
    am_ids = set()
    if project_id:
        row = (await s.execute(select(Project.am_id, Client.am_id).join(
            Client, Client.id == Project.client_id, isouter=True).where(Project.id == project_id))).first()
        am_ids = {x for x in (row or ()) if x}
    q = select(User.telegram_id).where(User.is_active.is_(True), User.telegram_id.isnot(None))
    q = q.where(User.id.in_(am_ids)) if am_ids else q.where(User.role.in_(("admin", "am")))
    return list((await s.execute(q)).scalars().all())


async def _post_for_review(context, s, task, version, who, fid=None, ftype=None, link=None, chats=None,
                           client=False):
    """Show a handed-in work with ✅ Принять / 🔄 Правки: in the project's topic(s), or —
    if the project has none — in the managers' private chats. Returns True if posted to a topic."""
    from db.models import ProjectChat, User
    from sqlalchemy import select
    cap = (f"📎 Версия {version} · «{task.title}»\n👤 {who}" + (f"\n🔗 {link}" if link else "")
           + ("\n🕓 у клиента на утверждении" if client else ""))
    targets, in_topic = chats, False
    if targets is None:
        targets = (await s.execute(select(ProjectChat.chat_id, ProjectChat.thread_id)
                                   .where(ProjectChat.project_id == task.project_id))).all() if task.project_id else []
        in_topic = bool(targets)
        if not targets:
            targets = [(tg, None) for tg in (await s.execute(select(User.telegram_id).where(
                User.role.in_(MANAGER_ROLES), User.is_active.is_(True),
                User.telegram_id.isnot(None)))).scalars().all()]
    send = {"photo": context.bot.send_photo, "video": context.bot.send_video}.get(ftype, context.bot.send_document)
    for chat_id, thread_id in targets:
        try:
            if fid:
                await send(chat_id, fid, caption=cap[:1024], message_thread_id=thread_id or None,
                           reply_markup=_review_kb(task.id, client))
            else:
                await context.bot.send_message(chat_id, cap, message_thread_id=thread_id or None,
                                               reply_markup=_review_kb(task.id, client))
        except Exception as e:
            logger.warning(f"post_for_review {chat_id}: {e}")
    return in_topic


async def review_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«👀 На проверке» (AM): every handed-in work, each with ✅ Принять / 🔄 Правки."""
    msg = update.effective_message
    if await _pg_role(update.effective_user.id) not in MANAGER_ROLES:
        await msg.reply_text("Это меню для AM.")
        return
    try:
        from db.models import AsyncSessionLocal, Task, User, Project
        from sqlalchemy import select
        async with AsyncSessionLocal() as s:
            q_rev = select(Task).where(Task.status.in_(("review", "client")))
            if await _pg_role(update.effective_user.id) == "am":
                from db.models import Client
                me = (await s.execute(select(User.id).where(
                    User.telegram_id == update.effective_user.id))).scalar_one()
                my_projects = select(Project.id).join(Client, Client.id == Project.client_id, isouter=True) \
                    .where((Project.am_id == me) | (Client.am_id == me))
                if (await s.execute(my_projects.limit(1))).first():
                    q_rev = q_rev.where(Task.project_id.in_(my_projects))
            tasks = (await s.execute(q_rev
                                     .order_by(Task.submitted_at.is_(None), Task.submitted_at)
                                     .limit(15))).scalars().all()
            if not tasks:
                await msg.reply_text("На проверке ничего нет 👌")
                return
            n_cli = len([t for t in tasks if t.status == "client"])
            await msg.reply_text(f"На проверке: {len(tasks) - n_cli}" + (f" · у клиента: {n_cli}" if n_cli else ""))
            for t in tasks:
                who = (await s.execute(select(User.full_name).where(
                    User.id == t.assignee_id))).scalar_one_or_none() if t.assignee_id else None
                proj = (await s.execute(select(Project.name).where(
                    Project.id == t.project_id))).scalar_one_or_none() if t.project_id else None
                version = await _task_version(s, t.id)
                await _post_for_review(context, s, t, version,
                                       f"{who or '—'}" + (f" · 📁 {proj}" if proj else ""),
                                       t.file_id, t.file_type, chats=[(msg.chat_id, None)],
                                       client=t.status == "client")
    except Exception as e:
        logger.warning(f"review_list: {e}")
        await msg.reply_text("Что-то пошло не так, попробуй ещё раз.")


async def _topic_project(s, chat_id, thread_id):
    """Project bound to this forum topic (or to the whole chat)."""
    from db.models import ProjectChat
    from sqlalchemy import select
    pid = (await s.execute(select(ProjectChat.project_id).where(
        ProjectChat.chat_id == chat_id, ProjectChat.thread_id == (thread_id or None)))).scalar_one_or_none()
    if not pid:
        pid = (await s.execute(select(ProjectChat.project_id).where(
            ProjectChat.chat_id == chat_id))).scalars().first()
    return pid


def _tk_now():
    from datetime import datetime as _dt
    return _dt.now(_TASHKENT)


# ── deadlines: «2 часа до дедлайна» + «просрочена» (not at night) ─────

def _quiet_now():
    """21:00–09:00 Tashkent: overdue notices wait for the morning."""
    h = _tk_now().hour
    return h >= 21 or h < 9


async def pg_overdue_job(context: ContextTypes.DEFAULT_TYPE):
    """Every ~5 min: (1) remind assignees 2 h before the deadline (task not handed in),
    (2) notify about tasks overdue by >15 min, once each — but not 21:00–09:00."""
    try:
        from db.models import AsyncSessionLocal, Task, ProjectChat
        from sqlalchemy import select
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        now = _dt.now(_tz.utc)
        async with AsyncSessionLocal() as s:
            soon = (await s.execute(select(Task).where(
                Task.deadline.isnot(None), Task.deadline > now, Task.deadline <= now + _td(hours=2),
                Task.status.in_(("pending", "in_progress", "revision")),
                Task.remind_notified_at.is_(None)).limit(50))).scalars().all()
            for task in soon:
                left = int((task.deadline - now).total_seconds() // 60)
                left_s = f"{left // 60} ч {left % 60} мин" if left >= 60 else f"{left} мин"
                dl = task.deadline.astimezone(_TASHKENT).strftime("%H:%M")
                await _dm_assignees(context, s, task,
                                    f"⏳ До дедлайна {left_s} (в {dl}): {task.title}\n"
                                    f"Готово — сдавай 👇", kb=_submit_btn(task.id))
                task.remind_notified_at = now
            await s.commit()

            # «you said you'd take the revisions at 15:40»
            due = (await s.execute(select(Task).where(
                Task.status == "revision", Task.eta_at.isnot(None), Task.eta_at <= now,
                Task.eta_notified_at.is_(None)).limit(50))).scalars().all()
            for task in due:
                await _dm_assignees(context, s, task,
                                    f"⏰ {task.eta_at.astimezone(_TASHKENT):%H:%M} — время взяться за правки "
                                    f"«{task.title}»", kb=_submit_btn(task.id, "📤 Сдать новую версию"))
                task.eta_notified_at = now
            await s.commit()

            if _quiet_now():
                return
            # the client has been silent for a day -> nudge the AM who sent it
            from db.models import StatusEvent, User
            silent = (await s.execute(select(Task, User.telegram_id)
                      .join(StatusEvent, (StatusEvent.entity == "task") & (StatusEvent.entity_id == Task.id)
                            & (StatusEvent.status == "client"))
                      .join(User, User.id == StatusEvent.actor_id)
                      .where(Task.status == "client", Task.client_nudged_at.is_(None),
                             StatusEvent.created_at < now - _td(hours=24))
                      .order_by(StatusEvent.created_at.desc()).limit(50))).all()
            for task, am_tg in silent:
                if task.client_nudged_at:
                    continue
                try:
                    await context.bot.send_message(
                        am_tg, f"🕓 Клиент молчит больше суток по «{task.title}» — напомни ему.",
                        reply_markup=_review_kb(task.id, client=True))
                except Exception:
                    pass
                task.client_nudged_at = now
            await s.commit()

            tasks = (await s.execute(select(Task).where(
                Task.deadline.isnot(None), Task.deadline < now - _td(minutes=15),
                Task.status.in_(("pending", "in_progress")),
                Task.overdue_notified_at.is_(None)).limit(50))).scalars().all()
            for task in tasks:
                dl = task.deadline.astimezone(_TASHKENT).strftime("%d.%m %H:%M")
                await _dm_assignees(context, s, task, f"⏰ Просрочена задача: {task.title}\nДедлайн был {dl}",
                                    kb=_submit_btn(task.id))
                for chat_id, thread_id in (await s.execute(select(
                        ProjectChat.chat_id, ProjectChat.thread_id)
                        .where(ProjectChat.project_id == task.project_id))).all():
                    try:
                        await context.bot.send_message(chat_id, f"⏰ Просрочена: {task.title} (дедлайн {dl})",
                                                       message_thread_id=thread_id or None)
                    except Exception:
                        pass
                task.overdue_notified_at = _dt.now(_tz.utc)
                task.status = "overdue"
            await s.commit()
    except Exception as e:
        logger.warning(f"pg_overdue_job: {e}")


async def shoot_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """From 19:00 Tashkent: remind the crew about tomorrow's shoots (once per shoot),
    with place, time, project and the gear checklist."""
    now = _tk_now()
    if now.hour < 19:
        return
    try:
        import json
        from datetime import datetime as _dt
        from db.models import AsyncSessionLocal, ShootSession, ShootParticipant, User, Project
        from sqlalchemy import select
        tomorrow = (now + timedelta(days=1)).date()
        start = _dt(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=_TASHKENT)
        async with AsyncSessionLocal() as s:
            shoots = (await s.execute(select(ShootSession).where(
                ShootSession.shoot_at >= start, ShootSession.shoot_at < start + timedelta(days=1),
                ShootSession.status == "planned", ShootSession.reminded_at.is_(None)))).scalars().all()
            for sh in shoots:
                tgs = (await s.execute(select(User.telegram_id)
                       .join(ShootParticipant, ShootParticipant.user_id == User.id)
                       .where(ShootParticipant.shoot_id == sh.id, User.telegram_id.isnot(None)))).scalars().all()
                proj = (await s.execute(select(Project.name).where(
                    Project.id == sh.project_id))).scalar_one_or_none() if sh.project_id else None
                try:
                    items = json.loads(sh.checklist or "[]")
                except ValueError:
                    items = []
                gear = "\n".join(f"{'✅' if it.get('done') else '⬜'} {it.get('t')}" for it in items if it.get("t"))
                text = (f"🎬 Завтра съёмка: {sh.title}\n"
                        f"🕒 {sh.shoot_at.astimezone(_TASHKENT).strftime('%d.%m %H:%M')}\n"
                        + (f"📍 {sh.location}\n" if sh.location else "")
                        + (f"📁 {proj}\n" if proj else "")
                        + (f"\nЧек-лист техники:\n{gear}\n" if gear else "")
                        + "\nПроверь технику и заряди батареи сегодня вечером 🔋")
                for tg in tgs:
                    try:
                        await context.bot.send_message(tg, text)
                    except Exception:
                        pass
                sh.reminded_at = _dt.now(_TASHKENT)
            await s.commit()
    except Exception as e:
        logger.warning(f"shoot_reminder_job: {e}")


async def _pg_user_id(telegram_id: int):
    try:
        from db.models import AsyncSessionLocal, User
        from sqlalchemy import select
        async with AsyncSessionLocal() as s:
            return (await s.execute(
                select(User.id).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    except Exception as e:
        logger.warning(f"_pg_user_id failed: {e}")
        return None


async def bind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bind [название] — в теме супергруппы: привязать тему к проекту, чтобы
    уведомления по нему падали сюда. С аргументом — сразу создаёт клиента и
    проект с этим именем. admin / am / director."""
    msg = update.effective_message
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Команду /bind нужно писать в нужной теме супергруппы проекта.")
        return
    role = await _pg_role(update.effective_user.id)
    if role not in ("admin", "am", "director"):
        await msg.reply_text("Привязывать темы к проектам может админ, АМ или Директор.")
        return

    thread_id = getattr(msg, "message_thread_id", None) or 0
    arg = " ".join(context.args).strip() if context.args else ""

    # ── /bind Название → создать клиента + проект + привязать ──
    if arg:
        name = arg[:80]
        uid = await _pg_user_id(update.effective_user.id)
        try:
            from db.models import AsyncSessionLocal, Client, Project, ProjectChat
            from sqlalchemy import select, func, delete as sa_delete
            async with AsyncSessionLocal() as s:
                # Same name as an existing active client -> bind to it instead of a duplicate.
                client = (await s.execute(select(Client).where(
                    func.lower(func.trim(Client.name)) == name.lower(),
                    Client.is_active.is_(True)).order_by(Client.id).limit(1))).scalar_one_or_none()
                existed = client is not None
                if not client:
                    client = Client(name=name, am_id=uid, telegram_id=chat.id, is_active=True)
                    s.add(client)
                    await s.flush()
                project = (await s.execute(select(Project).where(
                    Project.client_id == client.id, Project.is_active.is_(True))
                    .order_by(Project.id).limit(1))).scalar_one_or_none() if existed else None
                if not project:
                    project = Project(client_id=client.id, name=client.name, am_id=uid, is_active=True)
                    s.add(project)
                    await s.flush()
                await s.execute(sa_delete(ProjectChat).where(
                    ProjectChat.chat_id == chat.id,
                    ProjectChat.thread_id == (thread_id or None)))
                s.add(ProjectChat(project_id=project.id, chat_id=chat.id,
                                  thread_id=thread_id or None, title=chat.title, bound_by=uid))
                await s.commit()
        except Exception as e:
            logger.warning(f"bind create failed: {e}")
            await msg.reply_text("Не получилось создать клиента. Попробуй позже.")
            return
        if existed:
            await msg.reply_text(
                f"✅ Клиент «{client.name}» уже есть — эта тема привязана к его проекту "
                f"«{project.name}».")
            return
        await msg.reply_text(
            f"✅ Клиент и проект «{name}» созданы, эта тема привязана.\n"
            f"Задачи, контент и съёмки по проекту будут падать сюда. "
            f"Переименовать или добавить данные — в приложении: Ещё → Клиенты.")
        return

    # ── /bind без аргумента → список проектов + подсказка ──
    try:
        from db.models import AsyncSessionLocal, Project, Client
        from sqlalchemy import select
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(
                select(Project.id, Project.name, Client.name)
                .join(Client, Client.id == Project.client_id, isouter=True)
                .where(Project.is_active.is_(True))
                .order_by(Client.name, Project.name)
            )).all()
    except Exception as e:
        logger.warning(f"bind_cmd list failed: {e}")
        await msg.reply_text("Не получилось загрузить проекты. Попробуй позже.")
        return

    hint = "Новый клиент: напиши  /bind Название клиента"
    if not rows:
        await msg.reply_text(
            "Пока нет ни одного проекта.\n\n" + hint)
        return
    kb = [[InlineKeyboardButton(
        (f"{cl} — {pn}" if cl else pn)[:60],
        callback_data=f"pcbind_{pid}_{thread_id}")] for pid, pn, cl in rows[:60]]
    await msg.reply_text(
        "К какому проекту привязать эту тему?\n"
        "Уведомления по проекту будут приходить сюда (и в личку исполнителю).\n\n"
        + hint,
        reply_markup=InlineKeyboardMarkup(kb))


async def bind_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, pid, thread_id = q.data.split("_")
        pid, thread_id = int(pid), int(thread_id)
    except ValueError:
        return
    chat = update.effective_chat
    role = await _pg_role(update.effective_user.id)
    if role not in ("admin", "am", "director"):
        await q.edit_message_text("Нет прав.")
        return
    try:
        from db.models import AsyncSessionLocal, Project, ProjectChat
        from sqlalchemy import select, delete as sa_delete
        async with AsyncSessionLocal() as s:
            proj = (await s.execute(
                select(Project.name).where(Project.id == pid))).scalar_one_or_none()
            if not proj:
                await q.edit_message_text("Проект не найден.")
                return
            await s.execute(sa_delete(ProjectChat).where(
                ProjectChat.chat_id == chat.id,
                ProjectChat.thread_id == (thread_id or None)))
            s.add(ProjectChat(project_id=pid, chat_id=chat.id,
                              thread_id=thread_id or None,
                              title=chat.title,
                              bound_by=None))
            await s.commit()
    except Exception as e:
        logger.warning(f"bind_pick_cb failed: {e}")
        await q.edit_message_text("Не получилось сохранить привязку.")
        return
    await q.edit_message_text(
        f"✅ Тема привязана к проекту «{proj}».\n"
        f"Новые задачи этого проекта будут падать сюда. Отвязать: /unbind")


async def unbind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    role = await _pg_role(update.effective_user.id)
    if role not in ("admin", "am", "director"):
        await msg.reply_text("Только админ или АМ.")
        return
    thread_id = getattr(msg, "message_thread_id", None)
    try:
        from db.models import AsyncSessionLocal, ProjectChat
        from sqlalchemy import delete as sa_delete
        async with AsyncSessionLocal() as s:
            res = await s.execute(sa_delete(ProjectChat).where(
                ProjectChat.chat_id == chat.id,
                ProjectChat.thread_id == (thread_id or None)))
            await s.commit()
        await msg.reply_text("🔌 Привязка снята." if res.rowcount else "Тут ничего не было привязано.")
    except Exception as e:
        logger.warning(f"unbind failed: {e}")


async def install_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📲 *WHY NOT? OS на рабочий стол*\n\n"
        "1. Открой приложение кнопкой ниже\n"
        "2. Внутри: меню *⋮* (вверху справа) → «Добавить на главный экран»\n"
        "3. Готово — иконка на экране, открывается сразу в приложение\n\n"
        "_Так приложение работает с твоим Telegram-аккаунтом. "
        "Не добавляй через Safari — там будет отдельная веб-версия без входа._"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown",
                                    disable_web_page_preview=True)


async def _post_init(app: Application):
    """Set the persistent 'Открыть приложение' menu button + command list."""
    try:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Открыть приложение",
                                        web_app=WebAppInfo(url=WEBAPP_URL))
        )
        await app.bot.set_my_commands([
            BotCommand("start",   "Открыть приложение"),
            BotCommand("install", "Поставить иконку на телефон"),
            BotCommand("bind",    "Привязать тему к проекту / завести клиента (AM)"),
        ])
        logger.info("✅ menu button + commands set")
    except Exception as e:
        logger.warning(f"post_init setup failed: {e}")


# ── main ────────────────────────────────────────────────────────

def build_app(builder=None):
    """All handlers + jobs. `builder` lets a test harness plug in a recording bot."""
    app = (builder or Application.builder().token(TOKEN)).post_init(_post_init).build()

    app.job_queue.run_repeating(pg_overdue_job, interval=300, first=90)
    app.job_queue.run_repeating(shoot_reminder_job, interval=300, first=120)

    # Commands
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("install", install_cmd))
    app.add_handler(CommandHandler("bind",    bind_cmd))
    app.add_handler(CommandHandler("unbind",  unbind_cmd))
    app.add_handler(CallbackQueryHandler(bind_pick_cb, pattern=r"^pcbind_\d+_\d+$"))

    # Group media log + reaction-to-reference
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.ALL) & filters.ChatType.GROUPS, group_media_log), group=1)
    app.add_handler(MessageReactionHandler(reaction_ref))
    # File submission -> "which task?" prompt, in its own group too
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.VIDEO | filters.Document.ALL) & filters.ChatType.GROUPS,
        submit_file_prompt), group=2)
    app.add_handler(CallbackQueryHandler(subfile_pick_cb, pattern=r"^subfile_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(subfile_keep_cb, pattern=r"^sub(ref|new)_\d+$"))
    app.add_handler(CallbackQueryHandler(subfile_skip_cb, pattern=r"^subskip_\d+$"))
    app.add_handler(CallbackQueryHandler(subfile_undo_cb, pattern=r"^subundo_\d+$"))
    # AM review in the group: ✅ / 🔄 buttons + the reply with what to fix
    app.add_handler(CallbackQueryHandler(review_cb, pattern=r"^rv(ok|fix|cli)_\d+$"))
    app.add_handler(CallbackQueryHandler(eta_cb, pattern=r"^eta(now|set)_\d+$"))
    app.add_handler(CallbackQueryHandler(take_cb, pattern=r"^take_\d+$"))
    app.add_handler(CallbackQueryHandler(revision_send_cb, pattern=r"^rv(send|cancel)$"))
    # private chat: role keyboard, hand-in of a file / link, review list
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Text([BTN_SUBMIT]), submit_menu))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Text([BTN_REVIEW]), review_list))
    app.add_handler(CallbackQueryHandler(submit_pick_cb, pattern=r"^dmsub_\d+$"))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.REPLY & ~filters.COMMAND,      # group topic or the AM's private chat
        revision_comment_reply), group=3)
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND & ~filters.REPLY
        & ~filters.Text([BTN_SUBMIT, BTN_REVIEW, BTN_APP])
        & (filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.TEXT), dm_submission), group=5)

    return app


def main():
    app = build_app()
    logger.info("✅ WHY NOT? OS бот запущен")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
