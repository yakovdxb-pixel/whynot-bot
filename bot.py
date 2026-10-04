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
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await msg.reply_text(
        "👋 *WHY NOT? OS*\n\nЗадачи, контент-план и команда — в приложении 👇",
        parse_mode='Markdown', reply_markup=kb,
    )


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


async def submit_file_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A photo/video/document dropped in a project's bound topic -> ask which active task it's for."""
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

    thread_id = getattr(msg, "message_thread_id", None) or 0
    try:
        from db.models import AsyncSessionLocal, ProjectChat, Project, Client, Task
        from sqlalchemy import select
        async with AsyncSessionLocal() as s:
            proj_id = (await s.execute(select(ProjectChat.project_id).where(
                ProjectChat.chat_id == msg.chat_id,
                ProjectChat.thread_id == (thread_id or None)))).scalar_one_or_none()
            if not proj_id:
                proj_id = (await s.execute(select(ProjectChat.project_id).where(
                    ProjectChat.chat_id == msg.chat_id))).scalar_one_or_none()
            if not proj_id:
                return  # chat/topic isn't bound to a project (see /bind) — nothing to do
            proj = (await s.execute(select(Project.name, Project.client_id)
                    .where(Project.id == proj_id))).first()
            if not proj:
                return
            client_name = proj[0]
            if proj[1]:
                cn = (await s.execute(select(Client.name).where(Client.id == proj[1]))).scalar_one_or_none()
                client_name = cn or client_name
            tasks = (await s.execute(select(Task.id, Task.title).where(
                Task.project_id == proj_id,
                Task.status.notin_(("done", "published"))
            ).order_by(Task.deadline.is_(None), Task.deadline).limit(30))).all()
    except Exception as e:
        logger.warning(f"submit_file_prompt: {e}")
        return

    # stash the file — the callback can't carry it (64-byte callback_data limit)
    context.chat_data[f"subfile_{msg.message_id}"] = {
        "file_id": fid, "file_type": ftype, "from_user": update.effective_user.id,
        "project_id": proj_id, "mime": mime, "file_name": fname,
        "caption": (msg.caption or "").strip(), "chat_id": msg.chat_id,
    }
    kb = [[InlineKeyboardButton(f"📋 {title}"[:64], callback_data=f"subfile_{msg.message_id}_{tid}")]
          for tid, title in tasks]
    # Always offer a way to keep the file, even when no task fits.
    kb.append([
        InlineKeyboardButton("📌 В референсы", callback_data=f"subref_{msg.message_id}"),
        InlineKeyboardButton("➕ Новая задача", callback_data=f"subnew_{msg.message_id}"),
    ])
    text = ("📎 К какой задаче относится файл?" if tasks else
            f"📎 У клиента {client_name} нет активных задач. Что сделать с файлом?")
    await msg.reply_text(text, message_thread_id=thread_id or None,
                         reply_markup=InlineKeyboardMarkup(kb))


async def subfile_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executor picked which task the submitted file belongs to -> status=review, notify managers."""
    q = update.callback_query
    await q.answer()
    m = re.match(r"^subfile_(\d+)_(\d+)$", q.data or "")
    if not m:
        return
    orig_msg_id, task_id = int(m.group(1)), int(m.group(2))
    pending = context.chat_data.pop(f"subfile_{orig_msg_id}", None)
    if not pending:
        await q.edit_message_text("⌛ Файл потерян (бот перезапускался) — отправь его ещё раз.")
        return

    task = None
    cname = None
    submitter_name = "Кто-то"
    admins = []
    try:
        from db.models import AsyncSessionLocal, Task, User, Project, Client
        from sqlalchemy import select
        from datetime import datetime as _dt, timezone as _tz
        async with AsyncSessionLocal() as s:
            task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
            if not task:
                await q.edit_message_text("Задача не найдена (возможно, удалена).")
                return
            task.file_id = pending["file_id"]
            task.file_type = pending["file_type"]
            task.status = "review"
            task.submitted_at = _dt.now(_tz.utc)
            await s.commit()
            await _log_task_status(s, task.id, "review", pending["from_user"])
            version = await _task_version(s, task.id)

            row = (await s.execute(select(User.full_name).where(
                User.telegram_id == pending["from_user"]))).first()
            if row:
                submitter_name = row[0]

            if task.project_id:
                proj = (await s.execute(select(Project.name, Project.client_id)
                        .where(Project.id == task.project_id))).first()
                if proj:
                    cname = proj[0]
                    if proj[1]:
                        cn = (await s.execute(select(Client.name)
                              .where(Client.id == proj[1]))).scalar_one_or_none()
                        cname = cn or cname

            admins = list((await s.execute(select(User.telegram_id).where(
                User.role.in_(("admin", "am")), User.telegram_id.isnot(None),
                User.is_active.is_(True)))).scalars().all())
    except Exception as e:
        logger.warning(f"subfile_pick_cb: {e}")
        await q.edit_message_text("Что-то пошло не так, попробуй ещё раз.")
        return

    await q.edit_message_text(f"📎 Версия {version} · «{task.title}» — на проверке\n"
                              f"AM: смотри файл выше и жми кнопку 👇",
                              reply_markup=_review_kb(task.id))

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    who = f" по клиенту {cname}" if cname else ""
    for tg in admins:
        if tg == pending["from_user"]:
            continue
        try:
            await context.bot.send_message(
                tg, f"📎 {submitter_name} сдал задачу «{task.title}»{who}", reply_markup=kb)
        except Exception:
            pass


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


def _review_kb(task_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять", callback_data=f"rvok_{task_id}"),
        InlineKeyboardButton("🔄 Правки", callback_data=f"rvfix_{task_id}"),
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


async def _dm_assignees(context, s, task, text):
    for tg in await _assignee_tgs(s, task):
        try:
            await context.bot.send_message(tg, text)
        except Exception:
            pass   # never pressed Start — Telegram won't let the bot write first


def _thread_of(msg):
    return msg.message_thread_id if getattr(msg, "is_topic_message", False) else None


async def review_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """✅ Принять / 🔄 Правки pressed under a submitted file (AM / director / admin only)."""
    q = update.callback_query
    m = re.match(r"^rv(ok|fix)_(\d+)$", q.data or "")
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
            if task.status != "review":
                await q.answer("Уже решено — смотри статус в приложении", show_alert=True)
                await q.edit_message_reply_markup(None)
                return
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
                await q.edit_message_text(f"✅ Версия {version} принята — «{task.title}» ({am})")
                await _dm_assignees(context, s, task, f"✅ Принято! Задача «{task.title}» (версия {version})")
                try:   # all jobs of the content done -> content goes to the AM for approval
                    from routes_whynot import _advance_chain
                    await _advance_chain(s, _Bg(context.application), task)
                except Exception as e:
                    logger.warning(f"review_cb advance: {e}")
                return
            title = task.title
    except Exception as e:
        logger.warning(f"review_cb: {e}")
        await q.answer("Что-то пошло не так, попробуй ещё раз", show_alert=True)
        return
    # 🔄 Правки: start collecting the AM's notes as replies to one message
    await q.answer()
    msg = q.message
    await q.edit_message_text(f"🔄 Версия {version} · «{title}» — {am} пишет правки…")
    from html import escape
    who = f'<a href="tg://user?id={q.from_user.id}">{escape(q.from_user.first_name or "AM")}</a>'
    ask = await context.bot.send_message(
        msg.chat_id,
        f"✍️ {who}, правки к версии {version} «{escape(title)}»: отвечай на это сообщение — "
        f"по одной правке в ответе. Когда всё — жми «📨 Отправить», исполнитель получит их одним сообщением.",
        parse_mode="HTML", message_thread_id=_thread_of(msg),
        reply_markup=ForceReply(selective=True, input_field_placeholder="Что поправить?"))
    # ForceReply and inline buttons can't share a message — the buttons go on a second one
    ctl = await context.bot.send_message(msg.chat_id, "Правок пока нет.",
                                         message_thread_id=_thread_of(msg), reply_markup=_collect_kb(0))
    context.chat_data[f"rvask_{ask.message_id}"] = {
        "task": task_id, "version": version, "items": [], "ctl": ctl.message_id, "am": q.from_user.id}
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
    n = len(st["items"])
    lines = "\n".join(f"{i}. {t}" for i, t in enumerate(st["items"], 1))
    try:
        await context.bot.edit_message_text(f"Правки ({n}):\n{lines}", chat_id=msg.chat_id,
                                            message_id=st["ctl"], reply_markup=_collect_kb(n))
    except Exception as e:
        logger.warning(f"revision list update: {e}")


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
        await q.answer("Отменено")
        await q.edit_message_text("✖ Правки отменены — файл всё ещё на проверке.",
                                  reply_markup=_review_kb(st["task"]))
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
            task.review_comment = f"Версия {st['version']}:\n{lines}"
            task.updated_at = _dt.now(_tz.utc)
            await s.commit()
            await _log_task_status(s, task.id, "revision", q.from_user.id)
            n = (await s.execute(select(func.count()).select_from(StatusEvent).where(
                StatusEvent.entity == "task", StatusEvent.entity_id == task.id,
                StatusEvent.status == "revision"))).scalar_one()
            await _dm_assignees(context, s, task,
                                f"🔄 Правки к версии {st['version']} — «{task.title}»:\n{lines}\n\n"
                                f"Исправь и пришли новую версию в тему проекта.")
            title = task.title
    except Exception as e:
        logger.warning(f"revision_send_cb: {e}")
        await q.answer("Что-то пошло не так, попробуй ещё раз", show_alert=True)
        return
    context.chat_data.pop(f"rvask_{ask_id}", None)
    context.chat_data.pop(f"rvctl_{q.message.message_id}", None)
    await q.answer("Отправлено исполнителю")
    await q.edit_message_text(f"🔄 Правка №{n} · версия {st['version']} «{title}» — отправлено исполнителю:\n{lines}")


# ── /task: a task from any message in a bound topic ─────────────────
# Reply to a message with «/task @исполнитель 25.09 18:00»: text → task, its photo /
# video / file and links → the task's references, project = the topic's project.

def _tk_now():
    from datetime import datetime as _dt
    return _dt.now(_TASHKENT)


def _parse_deadline(words):
    """«15:00» · «25.09» · «25.09 18:00» · «завтра 12:00» → aware datetime (Tashkent) or None.
    A bare time that already passed today means tomorrow; a bare date means 18:00."""
    from datetime import datetime as _dt
    now = _tk_now()
    day, hm = None, None
    for w in words:
        w = w.strip(",.").lower()
        if w in ("сегодня", "bugun"):
            day = now.date()
        elif w in ("завтра", "ertaga"):
            day = (now + timedelta(days=1)).date()
        elif re.fullmatch(r"\d{1,2}[:.]\d{2}", w) and ":" in w:
            h, mi = map(int, w.split(":"))
            if h < 24 and mi < 60:
                hm = (h, mi)
        elif re.fullmatch(r"\d{1,2}\.\d{1,2}(\.\d{2,4})?", w):
            parts = [int(x) for x in w.split(".")]
            y = parts[2] if len(parts) == 3 else now.year
            y = y + 2000 if y < 100 else y
            try:
                day = _dt(y, parts[1], parts[0]).date()
            except ValueError:
                continue
            if len(parts) == 2 and (now.date() - day).days > 60:
                day = day.replace(year=y + 1)     # «05.01» said in December
    if not day and not hm:
        return None
    if not hm:
        hm = (18, 0)
    if not day:
        day = now.date()
        if (hm[0], hm[1]) <= (now.hour, now.minute):
            day = (now + timedelta(days=1)).date()
    return _dt(day.year, day.month, day.day, hm[0], hm[1], tzinfo=_TASHKENT)


async def _topic_project(s, chat_id, thread_id):
    from db.models import ProjectChat
    from sqlalchemy import select
    pid = (await s.execute(select(ProjectChat.project_id).where(
        ProjectChat.chat_id == chat_id, ProjectChat.thread_id == (thread_id or None)))).scalar_one_or_none()
    if not pid:
        pid = (await s.execute(select(ProjectChat.project_id).where(
            ProjectChat.chat_id == chat_id))).scalars().first()
    return pid


async def task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or update.effective_chat.type not in ("group", "supergroup"):
        await msg.reply_text("Команда /task работает в теме проекта: ответь ею на сообщение.")
        return
    if await _pg_role(update.effective_user.id) not in MANAGER_ROLES:
        await msg.reply_text("Ставить задачи командой /task может только AM.")
        return
    src = msg.reply_to_message
    # in forum topics a plain message is technically a «reply» to the topic's service message
    if not src or src.forum_topic_created:
        await msg.reply_text(
            "Ответь командой на сообщение, из которого сделать задачу:\n"
            "/task @исполнитель 15:00\n/task Баходир 25.09 18:00\n/task @аня завтра 12:00")
        return
    words = (msg.text or "").split()[1:]
    try:
        from db.models import (AsyncSessionLocal, Task, TaskAssignee, User, Project,
                               ReferenceItem, StatusEvent)
        from sqlalchemy import select, func
        async with AsyncSessionLocal() as s:
            pid = await _topic_project(s, msg.chat_id, _thread_of(msg) or 0)
            if not pid:
                await msg.reply_text("Эта тема не привязана к проекту — сначала /bind.")
                return
            proj = (await s.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
            people = (await s.execute(select(User).where(User.is_active.is_(True)))).scalars().all()
            # who: @username / tapped name (text_mention) / a plain first name
            picked, unknown = [], []
            for e in msg.entities or []:
                if e.type == "text_mention" and e.user:
                    u = next((p for p in people if p.telegram_id == e.user.id), None)
                    (picked.append(u) if u else unknown.append(e.user.first_name))
                elif e.type == "mention":
                    nick = msg.text[e.offset + 1:e.offset + e.length].lower()
                    u = next((p for p in people if (p.username or "").lower() == nick), None)
                    (picked.append(u) if u else unknown.append("@" + nick))
            if not picked and not unknown:
                for w in words:
                    lw = w.strip(",.").lower()
                    u = next((p for p in people if p.full_name and (
                        p.full_name.lower() == lw or p.full_name.lower().split()[0] == lw)), None)
                    if u:
                        picked.append(u)
            if unknown or not picked:
                names = ", ".join(sorted(p.full_name.split()[0] for p in people if p.full_name))
                miss = f"Не нашёл: {', '.join(unknown)}. " if unknown else ""
                await msg.reply_text(
                    f"{miss}Укажи исполнителя: @username или имя — {names}.\n"
                    f"(@username бот узнаёт, когда человек хоть раз открыл приложение или написал в группе.)")
                return
            picked = list({u.id: u for u in picked}.values())
            deadline = _parse_deadline(words)
            text = (src.text or src.caption or "").strip()
            default = {"photo": "Фото", "video": "Видео", "document": "Файл"}
            kind = "photo" if src.photo else "video" if src.video else "document" if src.document else None
            title = (text.split("\n")[0][:120] if text else
                     f"{default.get(kind, 'Задача')} из темы ({_tk_now().strftime('%d.%m')})")
            creator = next((p for p in people if p.telegram_id == update.effective_user.id), None)
            task = Task(title=title, description=text or None, status="pending", type="general",
                        priority="normal", project_id=pid, client_id=proj.client_id if proj else None,
                        created_by=creator.id if creator else None, assignee_id=picked[0].id,
                        deadline=deadline)
            s.add(task)
            await s.flush()
            for u in picked:
                s.add(TaskAssignee(task_id=task.id, user_id=u.id))
            # attachments + links of the source message → the task's references
            refs = 0
            f = (src.photo[-1] if src.photo else src.video or src.document)
            if f:
                s.add(ReferenceItem(task_id=task.id, kind="file", tg_file_id=f.file_id,
                                    mime=getattr(f, "mime_type", None) or ("image/jpeg" if src.photo else None),
                                    file_name=getattr(f, "file_name", None) or default[kind].lower(),
                                    tg_chat_id=src.chat_id, tg_message_id=src.message_id,
                                    added_by=creator.id if creator else None))
                refs += 1
            ents = src.entities or src.caption_entities or ()
            body = src.text or src.caption or ""
            urls = []
            for e in ents:
                if e.type == "url":
                    urls.append(src.parse_entity(e) if src.text else src.parse_caption_entity(e))
                elif e.type == "text_link" and e.url:
                    urls.append(e.url)
            for u in dict.fromkeys(x for x in urls if x):
                s.add(ReferenceItem(task_id=task.id, kind="link",
                                    url=u if "://" in u else "https://" + u,
                                    added_by=creator.id if creator else None))
                refs += 1
            s.add(StatusEvent(entity="task", entity_id=task.id, status="pending",
                              actor_id=creator.id if creator else None))
            await s.commit()
            tgs = [u.telegram_id for u in picked if u.telegram_id]
    except Exception as e:
        logger.warning(f"task_cmd: {e}")
        await msg.reply_text("Не получилось создать задачу, попробуй ещё раз.")
        return
    who = ", ".join(u.full_name for u in picked)
    dl = deadline.strftime("%d.%m %H:%M") if deadline else "без срока"
    card = (f"📋 Задача #{task.id}: {title}\n👤 {who}\n⏰ {dl}"
            + (f"\n📎 референсов: {refs}" if refs else "")
            + ("\n\nСрок не понял — поставь в приложении." if not deadline and len(words) > len(picked) else ""))
    await src.reply_text(card)
    for tg in tgs:
        try:
            await context.bot.send_message(
                tg, f"📋 Тебе назначена задача: {title}\n⏰ {dl}\n📁 {proj.name if proj else ''}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))]]))
        except Exception:
            pass


async def remember_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Keep users.username fresh so «/task @someone» finds people (checked once per run)."""
    u = update.effective_user
    if not u or u.is_bot or not u.username:
        return
    seen = context.bot_data.setdefault("unames", {})
    if seen.get(u.id) == u.username.lower():
        return
    seen[u.id] = u.username.lower()
    try:
        from db.models import AsyncSessionLocal, User
        from sqlalchemy import update as sa_update
        async with AsyncSessionLocal() as s:
            await s.execute(sa_update(User).where(User.telegram_id == u.id,
                                                  User.username.is_distinct_from(u.username.lower()))
                            .values(username=u.username.lower()))
            await s.commit()
    except Exception as e:
        logger.warning(f"remember_username: {e}")


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
                                    f"Готово — сдай файл в тему проекта.")
                task.remind_notified_at = now
            await s.commit()

            if _quiet_now():
                return
            tasks = (await s.execute(select(Task).where(
                Task.deadline.isnot(None), Task.deadline < now - _td(minutes=15),
                Task.status.in_(("pending", "in_progress")),
                Task.overdue_notified_at.is_(None)).limit(50))).scalars().all()
            for task in tasks:
                dl = task.deadline.astimezone(_TASHKENT).strftime("%d.%m %H:%M")
                await _dm_assignees(context, s, task, f"⏰ Просрочена задача: {task.title}\nДедлайн был {dl}")
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
            BotCommand("task",    "Задача из сообщения: ответь /task @кто 15:00 (AM)"),
        ])
        logger.info("✅ menu button + commands set")
    except Exception as e:
        logger.warning(f"post_init setup failed: {e}")


# ── main ────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TOKEN).post_init(_post_init).build()

    app.job_queue.run_repeating(pg_overdue_job, interval=300, first=90)
    app.job_queue.run_repeating(shoot_reminder_job, interval=300, first=120)

    # Commands
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("install", install_cmd))
    app.add_handler(CommandHandler("bind",    bind_cmd))
    app.add_handler(CommandHandler("unbind",  unbind_cmd))
    app.add_handler(CommandHandler("task",    task_cmd))
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
    # AM review in the group: ✅ / 🔄 buttons + the reply with what to fix
    app.add_handler(CallbackQueryHandler(review_cb, pattern=r"^rv(ok|fix)_\d+$"))
    app.add_handler(CallbackQueryHandler(revision_send_cb, pattern=r"^rv(send|cancel)$"))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.REPLY & filters.ChatType.GROUPS & ~filters.COMMAND,
        revision_comment_reply), group=3)

    # remember @usernames (for /task @someone) — its own group, sees every update
    app.add_handler(MessageHandler(filters.ALL, remember_username), group=4)

    logger.info("✅ WHY NOT? OS бот запущен")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
