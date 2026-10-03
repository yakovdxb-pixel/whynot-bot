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
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
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


# 🏆 is in Telegram's free reaction set; the others need Premium (kept for those who have it).
REF_EMOJI = {"🏆", "✍", "✍️", "📌", "📎", "⭐", "✅"}


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
    """React to a group photo/file with 🏆 (or 📌/✍ with Premium) → it lands in the project's references. Un-react removes it."""
    r = update.message_reaction
    if not r or not r.chat:
        return
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
    fid = ftype = None
    if msg.photo:
        fid, ftype = msg.photo[-1].file_id, "photo"
    elif msg.video:
        fid, ftype = msg.video.file_id, "video"
    elif msg.document:
        fid, ftype = msg.document.file_id, "document"
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

    if not tasks:
        await msg.reply_text(f"У клиента {client_name} нет активных задач",
                             message_thread_id=thread_id or None)
        return

    # stash the file — the callback can't carry it (64-byte callback_data limit)
    context.chat_data[f"subfile_{msg.message_id}"] = {
        "file_id": fid, "file_type": ftype, "from_user": update.effective_user.id,
    }
    kb = [[InlineKeyboardButton(f"📋 {title}"[:64], callback_data=f"subfile_{msg.message_id}_{tid}")]
          for tid, title in tasks]
    await msg.reply_text("📎 К какой задаче относится файл?",
                         message_thread_id=thread_id or None,
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

    await q.edit_message_text(f"✅ Файл прикреплён к задаче «{task.title}». Статус: На проверке 📎")

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


async def pg_overdue_job(context: ContextTypes.DEFAULT_TYPE):
    """Every ~5 min: notify assignees of tasks overdue by >15 min, once each."""
    try:
        from db.models import AsyncSessionLocal, Task, TaskAssignee, User, ProjectChat
        from sqlalchemy import select
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        cutoff = _dt.now(_tz.utc) - _td(minutes=15)
        async with AsyncSessionLocal() as s:
            tasks = (await s.execute(select(Task).where(
                Task.deadline.isnot(None), Task.deadline < cutoff,
                Task.status.in_(("pending", "in_progress")),
                Task.overdue_notified_at.is_(None)).limit(50))).scalars().all()
            for task in tasks:
                aids = list((await s.execute(select(TaskAssignee.user_id)
                            .where(TaskAssignee.task_id == task.id))).scalars().all())
                if not aids and task.assignee_id:
                    aids = [task.assignee_id]
                tgs = list((await s.execute(select(User.telegram_id).where(
                    User.id.in_(aids), User.telegram_id.isnot(None)))).scalars().all())
                dl = task.deadline.strftime("%d.%m %H:%M")
                for tg in tgs:
                    try:
                        await context.bot.send_message(tg, f"⏰ Просрочена задача: {task.title}\nДедлайн был {dl}")
                    except Exception:
                        pass
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
            from sqlalchemy import delete as sa_delete
            async with AsyncSessionLocal() as s:
                client = Client(name=name, am_id=uid, telegram_id=chat.id, is_active=True)
                s.add(client)
                await s.flush()
                project = Project(client_id=client.id, name=name, am_id=uid, is_active=True)
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

def main():
    app = Application.builder().token(TOKEN).post_init(_post_init).build()

    app.job_queue.run_repeating(pg_overdue_job, interval=300, first=90)

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

    logger.info("✅ WHY NOT? OS бот запущен")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
