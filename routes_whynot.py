"""
WHY NOT? OS — REST API for the Telegram Mini App.

All routes are mounted under /api. Backed by the async SQLAlchemy models in
db/models.py (Postgres). Auth mirrors the legacy behaviour in api.py:

  * no init-data header            -> dev user (first row in `users`, else id=0)
  * X-Init-Data / X-Telegram-Init-Data present -> validated against BOT_TOKEN
"""
import os, json, hmac, hashlib
from datetime import datetime, date, timezone
from decimal import Decimal
from urllib.parse import unquote

import httpx
from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, Request, UploadFile)
from fastapi.responses import Response, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select, or_, func, delete as sa_delete, update as sa_update
from sqlalchemy.exc import IntegrityError

from db.models import (
    AsyncSessionLocal, User, Task, ContentItem, Idea, IdeaVote, Blocker,
    Client, Project, ActivityEvent, TaskAssignee, ReferenceItem,
    ProjectChat, ShootSession, ShootParticipant, ContentAssignee, StatusEvent,
    PIPELINE_SEQUENCE, task_status_enum, user_role_enum,
    content_format_enum, task_priority_enum,
)

PIPELINE_ORDER = ["script", "approval", "revisions", "done", "published"]

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")

router = APIRouter(prefix="/api", tags=["whynot-os"])


# ── db session ──────────────────────────────────────────────────

async def get_session():
    async with AsyncSessionLocal() as session:
        yield session


# ── serialization ───────────────────────────────────────────────

def row_to_dict(row) -> dict:
    out = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if isinstance(val, (datetime, date)):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = float(val)
        out[col.name] = val
    return out


def _now():
    return datetime.now(timezone.utc)


# ── auth ────────────────────────────────────────────────────────

def _parse_init_data(init_data: str) -> dict:
    parsed = {}
    for chunk in init_data.split("&"):
        key, _, val = chunk.partition("=")
        parsed[key] = unquote(val)
    return parsed


def _validate_init_data(init_data: str) -> dict:
    """Return the Telegram `user` dict, or raise 403."""
    parsed = _parse_init_data(init_data)
    received_hash = parsed.pop("hash", "")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(403, "Invalid Telegram signature")
    try:
        return json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(403, "Malformed init data")


# ── standalone browser sessions (Telegram Login Widget) ─────────

_SESSION_SECRET = hashlib.sha256(("wn-session:" + BOT_TOKEN).encode()).digest()
SESSION_DAYS = 30


def _sign_session(telegram_id: int) -> str:
    exp = int(_now().timestamp()) + SESSION_DAYS * 86400
    payload = f"{telegram_id}.{exp}"
    sig = hmac.new(_SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _read_session(token: str):
    try:
        tg_s, exp_s, sig = token.split(".")
        good = hmac.new(_SESSION_SECRET, f"{tg_s}.{exp_s}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        if int(exp_s) < _now().timestamp():
            return None
        return int(tg_s)
    except Exception:  # noqa: BLE001
        return None


def _verify_login_widget(data: dict) -> bool:
    received = data.pop("hash", "")
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    good = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, received)


async def _user_dict(session, tg_id: int, fallback_name: str = "Guest") -> dict:
    row = (await session.execute(
        select(User).where(User.telegram_id == tg_id)
    )).scalar_one_or_none()
    if row and row.is_active:
        return {"id": row.id, "telegram_id": tg_id,
                "full_name": row.full_name, "role": row.role, "registered": True}
    return {"id": 0, "telegram_id": tg_id,
            "full_name": fallback_name or "Guest", "role": "guest", "registered": False}


async def current_user(request: Request, session=Depends(get_session)) -> dict:
    """Resolve the acting user.

    Shape: {"id": <internal id or 0>, "telegram_id": int, "full_name": str,
            "role": str, "registered": bool}
    """
    init_data = (
        request.headers.get("X-Init-Data")
        or request.headers.get("X-Telegram-Init-Data")
        or ""
    )

    tg_user = None
    if init_data and BOT_TOKEN:
        tg_user = _validate_init_data(init_data)
    elif init_data and not BOT_TOKEN:
        # dev tunnel with a real Telegram client but no server token
        try:
            tg_user = json.loads(_parse_init_data(init_data).get("user", "{}"))
        except json.JSONDecodeError:
            tg_user = None

    if tg_user and tg_user.get("id"):
        name = " ".join(filter(None, [tg_user.get("first_name"), tg_user.get("last_name")]))
        return await _user_dict(session, int(tg_user["id"]), name)

    # ── standalone browser: signed session cookie ──
    if BOT_TOKEN:
        tok = request.cookies.get("wn_session")
        tg_id = _read_session(tok) if tok else None
        if tg_id:
            return await _user_dict(session, tg_id)

    # ── no valid init data ──
    if not BOT_TOKEN:
        # local dev only (no token to validate against) — act as the first user
        row = (await session.execute(
            select(User).order_by(User.id).limit(1)
        )).scalar_one_or_none()
        if row:
            return {"id": row.id, "telegram_id": row.telegram_id or 0,
                    "full_name": row.full_name, "role": row.role, "registered": True}
        return {"id": 0, "telegram_id": 0, "full_name": "Dev",
                "role": "admin", "registered": True}
    return {"id": 0, "telegram_id": 0, "full_name": "Guest",
            "role": "guest", "registered": False}


_bot_username_cache = {"v": None}


@router.get("/webapp-config")
async def webapp_config():
    """Public: what the standalone login screen needs."""
    u = _bot_username_cache["v"]
    if u is None and BOT_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                r = await c.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe")
            u = (r.json().get("result") or {}).get("username")
            _bot_username_cache["v"] = u
        except Exception:  # noqa: BLE001
            u = None
    return {"bot_username": u, "standalone": bool(BOT_TOKEN)}


@router.get("/auth/telegram")
async def auth_telegram(request: Request):
    """Telegram Login Widget callback -> set a 30-day signed cookie, bounce to the app."""
    data = dict(request.query_params)
    if not (data.get("hash") and data.get("id")):
        raise HTTPException(400, "missing auth data")
    if not _verify_login_widget(dict(data)):
        raise HTTPException(403, "bad Telegram signature")
    try:
        if _now().timestamp() - int(data.get("auth_date", "0")) > 86400:
            raise HTTPException(403, "auth data expired — try again")
    except ValueError:
        raise HTTPException(400, "bad auth_date")
    resp = RedirectResponse(url="/webapp", status_code=303)
    resp.set_cookie("wn_session", _sign_session(int(data["id"])),
                    max_age=SESSION_DAYS * 86400, httponly=True,
                    secure=True, samesite="lax", path="/")
    return resp


@router.post("/auth/logout")
async def auth_logout():
    resp = Response(status_code=204)
    resp.delete_cookie("wn_session", path="/")
    return resp


async def member(user: dict = Depends(current_user)) -> dict:
    """Closed access: only users present in the `users` table get through."""
    if not user.get("registered"):
        if not user.get("telegram_id"):
            raise HTTPException(401, {
                "code": "no_auth",
                "message": "Войди через Telegram.",
            })
        raise HTTPException(403, {
            "code": "not_registered",
            "message": "Доступ закрыт. Передай свой Telegram ID администратору.",
            "telegram_id": user.get("telegram_id") or 0,
        })
    return user


# ── models ──────────────────────────────────────────────────────

TASK_STATUSES = set(task_status_enum.enums)
OPEN_TASK_STATUSES = ("pending", "in_progress", "overdue")
USER_ROLES = {"admin", "am", "director", "editor", "designer",
              "videographer", "mobilographer", "driver", "intern"}
MANAGER_ROLES = ("admin", "am", "director")   # full access: team, clients, dashboard, /bind
TASK_PRIORITIES = set(task_priority_enum.enums)          # low, normal, high, urgent
CONTENT_FORMATS = set(content_format_enum.enums)
# tolerate the labels the Mini App form uses
PRIORITY_ALIASES = {"critical": "urgent", "medium": "normal"}
FORMAT_ALIASES = {"reel": "reels", "video": "youtube_long", "article": "other"}
STATUS_RU = {"pending": "Ожидает", "in_progress": "В работе", "done": "Готово",
             "overdue": "Просрочено", "cancelled": "Отменено"}


class TaskPatch(BaseModel):
    status: str | None = None
    title: str | None = None
    description: str | None = None
    priority: str | None = None
    deadline: str | None = None
    assignee_ids: list[int] | None = None
    client_id: int | None = None
    project_id: int | None = None
    location: str | None = None


class ContentJobCreate(BaseModel):
    assignee_ids: list[int] | None = None
    assignee_id: int | None = None
    deadline: str | None = None
    location: str | None = None
    description: str | None = None


class RolePatch(BaseModel):
    role: str
    full_name: str | None = None
    username: str | None = None


class TaskCreate(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: str | None = "normal"
    deadline: str | None = None
    assignee_id: int | None = None
    assignee_ids: list[int] | None = None
    client_id: int | None = None
    project_id: int | None = None
    reference_url: str | None = None


class LinkRef(BaseModel):
    task_id: int | None = None
    content_id: int | None = None
    idea_id: int | None = None
    project_id: int | None = None
    url: str
    title: str | None = None


class TeamRolePatch(BaseModel):
    role: str
    full_name: str | None = None


class ClientCreate(BaseModel):
    name: str
    contact: str | None = None
    notes: str | None = None
    project_name: str | None = None   # empty -> a project named after the client


class ClientPatch(BaseModel):
    name: str | None = None
    contact: str | None = None
    notes: str | None = None
    am_id: int | None = None
    is_active: bool | None = None
    monthly_posts: int | None = None   # applied to the client's project(s)


class ProjectPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    am_id: int | None = None
    is_active: bool | None = None
    monthly_posts: int | None = None


class ProjectCreate(BaseModel):
    client_id: int
    name: str
    description: str | None = None


class ContentCreate(BaseModel):
    format: str
    topic: str | None = None
    publish_date: str | None = None
    publish_at: str | None = None
    project_id: int | None = None
    client_id: int | None = None
    rubric: str | None = None
    platform: str | None = None
    hook: str | None = None
    script: str | None = None
    caption: str | None = None
    hashtags: str | None = None
    smm_id: int | None = None
    copywriter_id: int | None = None
    editor_id: int | None = None
    designer_id: int | None = None
    assignee_ids: list[int] | None = None
    content_kind: str | None = None
    reference_url: str | None = None


class ContentPatch(BaseModel):
    topic: str | None = None
    format: str | None = None
    pipeline_status: str | None = None
    publish_date: str | None = None
    publish_at: str | None = None
    project_id: int | None = None
    client_id: int | None = None
    client_approved: bool | None = None
    rubric: str | None = None
    platform: str | None = None
    hook: str | None = None
    script: str | None = None
    caption: str | None = None
    hashtags: str | None = None
    smm_id: int | None = None
    copywriter_id: int | None = None
    editor_id: int | None = None
    designer_id: int | None = None
    assignee_ids: list[int] | None = None
    content_kind: str | None = None


class IdeaCreate(BaseModel):
    title: str
    description: str | None = None
    format: str | None = None
    project_id: int | None = None
    reference_url: str | None = None


class BlockerCreate(BaseModel):
    title: str
    description: str | None = None
    assigned_to: int | None = None


class ShootCreate(BaseModel):
    title: str
    shoot_at: str | None = None
    location: str | None = None
    client_id: int | None = None
    project_id: int | None = None
    notes: str | None = None
    participant_ids: list[int] | None = None


class ShootPatch(BaseModel):
    title: str | None = None
    shoot_at: str | None = None
    location: str | None = None
    client_id: int | None = None
    project_id: int | None = None
    notes: str | None = None
    status: str | None = None
    participant_ids: list[int] | None = None


SHOOT_STATUSES = {"planned", "done", "cancelled"}


def _clean(s):
    if s is None:
        return None
    s = s.strip()
    return s or None


def _parse_date(v):
    v = _clean(v)
    if not v:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        raise HTTPException(422, f"bad date: {v!r} (expected YYYY-MM-DD)")


def _parse_dt(v):
    v = _clean(v)
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v + "T00:00:00+00:00") if len(v) == 10 \
            else datetime.fromisoformat(v)
    except ValueError:
        raise HTTPException(422, f"bad datetime: {v!r}")
    if dt.tzinfo is None:                      # datetime-local inputs have no tz
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── helpers ─────────────────────────────────────────────────────

async def _names_for(session, ids) -> dict:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = (await session.execute(
        select(User.id, User.full_name).where(User.id.in_(ids))
    )).all()
    return {r.id: r.full_name for r in rows}


CONTENT_ROLE_COLS = (
    "author_id", "producer_id", "photographer_id",
    "editor_id", "designer_id", "am_id", "created_by",
)


# ── routes ──────────────────────────────────────────────────────

async def _mine_task_ids(session, uid):
    """Task ids where uid is a (co-)assignee."""
    if not uid:
        return []
    return list((await session.execute(
        select(TaskAssignee.task_id).where(TaskAssignee.user_id == uid)
    )).scalars().all())


async def _ref_summaries(session, *, task_ids=None, content_ids=None, idea_ids=None, project_ids=None):
    """id -> {"count": n, "thumbs": [{"id", "download"}]} (thumbs = up to 4 image files)."""
    if task_ids is not None:
        col, ids, key_attr = ReferenceItem.task_id, task_ids, "task_id"
    elif idea_ids is not None:
        col, ids, key_attr = ReferenceItem.idea_id, idea_ids, "idea_id"
    elif project_ids is not None:
        col, ids, key_attr = ReferenceItem.project_id, project_ids, "project_id"
    else:
        col, ids, key_attr = ReferenceItem.content_id, content_ids, "content_id"
    if not ids:
        return {}
    rows = (await session.execute(
        select(ReferenceItem).where(col.in_(ids)).order_by(ReferenceItem.id.desc())
    )).scalars().all()
    out = {}
    for r in rows:
        key = getattr(r, key_attr)
        s = out.setdefault(key, {"count": 0, "thumbs": []})
        s["count"] += 1
        if r.kind == "file" and (r.mime or "").startswith("image/") and len(s["thumbs"]) < 4:
            s["thumbs"].append({"id": r.id, "download": f"/api/references/{r.id}/file"})
    return out


async def _attach_assignees(session, tasks):
    """Add `assignees: [{id, name}]` and `refs_count` to each serialized task dict."""
    if not tasks:
        return []
    ids = [t.id for t in tasks]
    rows = (await session.execute(
        select(TaskAssignee.task_id, TaskAssignee.user_id).where(TaskAssignee.task_id.in_(ids))
    )).all()
    by_task = {}
    for tid, uid in rows:
        by_task.setdefault(tid, []).append(uid)
    all_uids = {u for lst in by_task.values() for u in lst} | {t.assignee_id for t in tasks}
    names = await _names_for(session, all_uids)
    refs = await _ref_summaries(session, task_ids=ids)
    out = []
    for t in tasks:
        d = row_to_dict(t)
        aids = by_task.get(t.id) or ([t.assignee_id] if t.assignee_id else [])
        d["assignees"] = [{"id": i, "name": names.get(i)} for i in aids]
        d["assignee_name"] = names.get(t.assignee_id) or (d["assignees"][0]["name"] if d["assignees"] else None)
        s = refs.get(t.id, {})
        d["refs_count"] = s.get("count", 0)
        d["ref_thumbs"] = s.get("thumbs", [])
        out.append(d)
    return out


@router.get("/home")
async def home(user: dict = Depends(member), session=Depends(get_session)):
    uid = user["id"]
    mine = await _mine_task_ids(session, uid)

    tasks = (await session.execute(
        select(Task)
        .where(or_(Task.assignee_id == uid, Task.id.in_(mine)),
               Task.status.in_(OPEN_TASK_STATUSES))
        .order_by(Task.deadline.is_(None), Task.deadline, Task.id)
        .limit(100)
    )).scalars().all() if uid else []

    blockers = (await session.execute(
        select(Blocker)
        .where(Blocker.status == "active",
               or_(Blocker.reported_by == uid, Blocker.assigned_to == uid))
        .order_by(Blocker.created_at.desc())
        .limit(50)
    )).scalars().all() if uid else []

    content = (await session.execute(
        select(ContentItem)
        .where(or_(*[getattr(ContentItem, c) == uid for c in CONTENT_ROLE_COLS]),
               ContentItem.pipeline_status.notin_(("published", "analytics")))
        .order_by(ContentItem.publish_date.is_(None), ContentItem.publish_date)
        .limit(50)
    )).scalars().all() if uid else []

    return {
        "user": user,
        "my_tasks": await _attach_assignees(session, tasks),
        "my_blockers": [row_to_dict(b) for b in blockers],
        "my_content": [row_to_dict(c) for c in content],
    }


@router.get("/tasks")
async def list_tasks(my: bool = False, status: str | None = None,
                     user: dict = Depends(member), session=Depends(get_session)):
    q = select(Task)
    if my:
        mine = await _mine_task_ids(session, user["id"])
        q = q.where(or_(Task.assignee_id == user["id"], Task.id.in_(mine)))
    if status:
        q = q.where(Task.status == status)
    q = q.order_by(Task.deadline.is_(None), Task.deadline, Task.id.desc()).limit(200)
    rows = (await session.execute(q)).scalars().all()
    return await _attach_assignees(session, rows)


@router.patch("/tasks/{task_id}")
async def update_task(task_id: int, patch: TaskPatch, bg: BackgroundTasks,
                      user: dict = Depends(member), session=Depends(get_session)):
    task = (await session.execute(
        select(Task).where(Task.id == task_id)
    )).scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")

    status_changed = False
    if patch.status is not None:
        if patch.status not in TASK_STATUSES:
            raise HTTPException(422, f"status must be one of {sorted(TASK_STATUSES)}")
        status_changed = patch.status != task.status
        task.status = patch.status
        if patch.status == "done" and task.actual_completion is None:
            task.actual_completion = _now()

    if patch.title is not None:
        task.title = _clean(patch.title) or task.title
    if patch.description is not None:
        task.description = _clean(patch.description)
    if patch.priority is not None:
        p = PRIORITY_ALIASES.get(patch.priority.lower(), patch.priority.lower())
        if p not in TASK_PRIORITIES:
            raise HTTPException(422, f"priority must be one of {sorted(TASK_PRIORITIES)}")
        task.priority = p
    if patch.deadline is not None:
        task.deadline = _parse_dt(patch.deadline)
    if patch.location is not None:
        task.location = _clean(patch.location)
    if patch.client_id is not None:
        task.client_id = patch.client_id or None
    if patch.project_id is not None:
        task.project_id = patch.project_id or None

    new_notify = []
    if patch.assignee_ids is not None:
        aids = list(dict.fromkeys(patch.assignee_ids))
        cur = set((await session.execute(
            select(TaskAssignee.user_id).where(TaskAssignee.task_id == task_id)
        )).scalars().all())
        new_notify = [a for a in aids if a not in cur]
        await session.execute(sa_delete(TaskAssignee).where(TaskAssignee.task_id == task_id))
        for a in aids:
            session.add(TaskAssignee(task_id=task_id, user_id=a))
        task.assignee_id = aids[0] if aids else None

    task.updated_at = _now()
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "один из id не найден")
    await _log(session, "task",
               "completed" if patch.status == "done" else "updated",
               task.id, user["id"], task.title)
    if status_changed:
        await _log_status(session, "task", task.id, patch.status, user["id"])

    for a in new_notify:
        tg = await _telegram_id_for(session, a)
        if tg:
            bg.add_task(_tg_send, tg, f"📋 Тебе назначена задача: {task.title}")
    if new_notify:
        who = ", ".join(filter(None, (await _names_for(session, new_notify)).values()))
        await _notify_project(bg, session, task.project_id,
                              f"📋 {task.title}\nНазначено: {who}")
    if patch.status:
        await _notify_project(bg, session, task.project_id,
                              f"✏️ Задача «{task.title}» → {STATUS_RU.get(patch.status, patch.status)}")

    result = await _attach_assignees(session, [task])
    return result[0]


@router.get("/tasks/{task_id}/timeline")
async def task_timeline(task_id: int,
                        user: dict = Depends(member), session=Depends(get_session)):
    return await _timeline(session, "task", task_id)


@router.get("/content/{content_id}/timeline")
async def content_timeline(content_id: int,
                           user: dict = Depends(member), session=Depends(get_session)):
    return await _timeline(session, "content", content_id)


CONTENT_PEOPLE = ("smm_id", "copywriter_id", "editor_id", "designer_id",
                  "producer_id", "author_id", "am_id")

CONTENT_START = "script"
# simplified 4-step publication flow (reusing existing enum values)
CONTENT_NEXT = {
    "script": "approval", "approval": "done", "done": "published",
    "revisions": "done", "published": None,
    # graceful for any legacy rows
    "idea": "script", "shoot": "approval", "edit": "approval",
    "review": "done", "client": "done", "analytics": None,
}
CONTENT_STAGE_RU = {"script": "В процессе", "approval": "На одобрении",
                    "revisions": "Правка", "done": "Готов", "published": "Опубликовано"}

# production jobs dispatched from a content card (stored as linked tasks)
CJOB_KINDS = ("shoot", "design", "edit")
CJOB_RU = {"shoot": "Съёмка", "design": "Дизайн", "edit": "Монтаж"}
CJOB_EMOJI = {"shoot": "🎥", "design": "🎨", "edit": "✂️"}


async def _content_jobs(session, cids):
    """content_id -> [job dicts] (linked tasks with a job_kind)."""
    if not cids:
        return {}
    rows = (await session.execute(
        select(Task).where(Task.content_id.in_(cids), Task.job_kind.isnot(None))
        .order_by(Task.id))).scalars().all()
    names = await _names_for(session, [t.assignee_id for t in rows])
    out = {}
    for t in rows:
        out.setdefault(t.content_id, []).append({
            "id": t.id, "kind": t.job_kind, "status": t.status, "title": t.title,
            "assignee_id": t.assignee_id, "assignee_name": names.get(t.assignee_id),
            "deadline": t.deadline.isoformat() if t.deadline else None,
            "location": t.location, "notes": t.description,
        })
    return out


async def _content_assignees(session, cids):
    if not cids:
        return {}
    rows = (await session.execute(
        select(ContentAssignee.content_id, ContentAssignee.user_id)
        .where(ContentAssignee.content_id.in_(cids)))).all()
    out = {}
    for cid, uid in rows:
        out.setdefault(cid, []).append(uid)
    return out


def _can_edit_content(user, item, my_pids, assignees):
    if user["role"] in MANAGER_ROLES:
        return True
    uid = user["id"]
    return bool(uid) and (
        item.project_id in my_pids
        or getattr(item, "created_by", None) == uid
        or getattr(item, "am_id", None) == uid
        or uid in assignees
    )


async def _sync_content_assignees(session, cid, ids):
    ids = list(dict.fromkeys(ids or []))
    await session.execute(sa_delete(ContentAssignee).where(ContentAssignee.content_id == cid))
    for uid in ids:
        session.add(ContentAssignee(content_id=cid, user_id=uid))
    return ids


def _content_out(c, names, projects, refs, assignees=None, can_edit=True, jobs=None):
    d = row_to_dict(c)
    for col in CONTENT_PEOPLE:
        d[col.replace("_id", "_name")] = names.get(getattr(c, col, None))
    d["project_name"] = projects.get(c.project_id)
    d["publish_at"] = c.publish_at.isoformat() if c.publish_at else None
    d["content_kind"] = getattr(c, "content_kind", None)
    d["assignees"] = [{"id": i, "name": names.get(i)} for i in (assignees or [])]
    d["jobs"] = jobs or []
    d["can_edit"] = bool(can_edit)
    s = refs.get(c.id, {})
    d["refs_count"] = s.get("count", 0)
    d["ref_thumbs"] = s.get("thumbs", [])
    return d


async def _projects_map(session, ids):
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = (await session.execute(
        select(Project.id, Project.name).where(Project.id.in_(ids)))).all()
    return {i: n for i, n in rows}


async def _my_project_ids(session, uid):
    """Project ids where the user is the AM (directly, or via the client)."""
    if not uid:
        return set()
    a = (await session.execute(
        select(Project.id).where(Project.am_id == uid))).scalars().all()
    b = (await session.execute(
        select(Project.id).join(Client, Client.id == Project.client_id)
        .where(Client.am_id == uid))).scalars().all()
    return set(a) | set(b)


@router.get("/content")
async def list_content(client_id: int | None = None, project_id: int | None = None,
                       scope: str | None = None,
                       user: dict = Depends(member), session=Depends(get_session)):
    q = select(ContentItem)
    if client_id:
        q = q.where(ContentItem.client_id == client_id)
    if project_id:
        q = q.where(ContentItem.project_id == project_id)
    if scope == "mine":
        mine = await _my_project_ids(session, user["id"])
        q = q.where(or_(ContentItem.project_id.in_(mine) if mine else False,
                        ContentItem.am_id == user["id"],
                        ContentItem.created_by == user["id"]))
    q = q.order_by(ContentItem.pipeline_status,
                   ContentItem.publish_at.is_(None), ContentItem.publish_at,
                   ContentItem.publish_date.is_(None), ContentItem.publish_date,
                   ContentItem.id.desc()).limit(300)
    rows = (await session.execute(q)).scalars().all()
    refs = await _ref_summaries(session, content_ids=[c.id for c in rows])
    assg = await _content_assignees(session, [c.id for c in rows])
    jobs = await _content_jobs(session, [c.id for c in rows])
    uids = {getattr(c, col, None) for c in rows for col in CONTENT_PEOPLE}
    uids |= {u for lst in assg.values() for u in lst}
    names = await _names_for(session, uids)
    projects = await _projects_map(session, [c.project_id for c in rows])
    my_pids = await _my_project_ids(session, user["id"])
    return [_content_out(c, names, projects, refs, assg.get(c.id, []),
                         _can_edit_content(user, c, my_pids, assg.get(c.id, [])),
                         jobs.get(c.id, []))
            for c in rows]


@router.post("/content/{content_id}/advance")
async def advance_content(content_id: int, bg: BackgroundTasks,
                          user: dict = Depends(member), session=Depends(get_session)):
    item = (await session.execute(
        select(ContentItem).where(ContentItem.id == content_id)
    )).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Content not found")
    assg = (await _content_assignees(session, [item.id])).get(item.id, [])
    my_pids = await _my_project_ids(session, user["id"])
    if not _can_edit_content(user, item, my_pids, assg):
        raise HTTPException(403, "можно двигать только свой контент")
    cur = item.pipeline_status
    nxt = CONTENT_NEXT.get(cur, PIPELINE_SEQUENCE.get(cur, {}).get("next"))
    if not nxt:
        raise HTTPException(400, "это финальный этап")
    item.pipeline_status = nxt
    item.updated_at = _now()
    await session.commit()
    await session.refresh(item)
    await _log(session, "content", "moved", item.id, user["id"], item.topic)
    await _log_status(session, "content", item.id, nxt, user["id"])
    # notify the content's assignees only now (not on assign) + the project topic
    stage = CONTENT_STAGE_RU.get(nxt, nxt)
    title = item.topic or f"контент #{item.id}"
    for uid in assg:
        tg = await _telegram_id_for(session, uid)
        if tg:
            bg.add_task(_tg_send, tg, f"▶ Контент «{title}» → {stage}")
    await _notify_project(bg, session, item.project_id,
                          f"▶ Контент «{title}» → {stage}")
    return await _one_content(session, item, user)


@router.post("/content/{content_id}/jobs/{kind}", status_code=201)
async def create_content_job(content_id: int, kind: str, body: ContentJobCreate,
                             bg: BackgroundTasks,
                             user: dict = Depends(member), session=Depends(get_session)):
    """Dispatch a production job (shoot / design / edit) from a content card.
    Stored as a linked task so it shows up in the assignee's Главная + overdue flow."""
    if kind not in CJOB_KINDS:
        raise HTTPException(422, f"kind must be one of {list(CJOB_KINDS)}")
    item = (await session.execute(
        select(ContentItem).where(ContentItem.id == content_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Content not found")
    assg = (await _content_assignees(session, [item.id])).get(item.id, [])
    my_pids = await _my_project_ids(session, user["id"])
    if not _can_edit_content(user, item, my_pids, assg):
        raise HTTPException(403, "можно ставить задачи только по своему контенту")
    uid = user["id"] or None
    aids = list(dict.fromkeys(
        body.assignee_ids or ([] if body.assignee_id is None else [body.assignee_id])))
    due = _parse_dt(body.deadline)
    topic = item.topic or f"контент #{item.id}"
    obj = Task(
        title=f"{CJOB_EMOJI[kind]} {CJOB_RU[kind]}: {topic}"[:120],
        description=_clean(body.description),
        job_kind=kind,
        location=_clean(body.location) if kind == "shoot" else None,
        type="content_pipeline",
        priority="normal",
        deadline=due,
        status="pending",
        created_by=uid,
        assignee_id=aids[0] if aids else None,
        content_id=item.id,
        client_id=item.client_id,
        project_id=item.project_id,
    )
    session.add(obj)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "ссылка на несуществующий id")
    await session.refresh(obj)
    for a in aids:
        session.add(TaskAssignee(task_id=obj.id, user_id=a))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "исполнитель не найден")
    await _log(session, "task", "created", obj.id, uid, obj.title)
    await _log_status(session, "task", obj.id, "pending", uid)

    dl = due.strftime("%d.%m.%Y %H:%M") if due else "без срока"
    for a in aids:
        tg = await _telegram_id_for(session, a)
        if tg:
            lines = [f"{CJOB_EMOJI[kind]} {CJOB_RU[kind]} · контент «{topic}»",
                     f"Дедлайн: {dl}"]
            if obj.location:
                lines.append(f"Адрес: {obj.location}")
            if obj.description:
                lines.append(f"Заметки: {obj.description}")
            bg.add_task(_tg_send, tg, "\n".join(lines))
    who = ", ".join(filter(None, (await _names_for(session, aids)).values())) or "—"
    await _notify_project(
        bg, session, item.project_id,
        f"{CJOB_EMOJI[kind]} {CJOB_RU[kind]} по контенту «{topic}»\n"
        f"Исполнитель: {who}\nДедлайн: {dl}")
    return await _one_content(session, item, user)


PIPELINE_STEPS = set(PIPELINE_ORDER)


async def _one_content(session, item, user=None):
    refs = await _ref_summaries(session, content_ids=[item.id])
    assg = (await _content_assignees(session, [item.id])).get(item.id, [])
    jobs = (await _content_jobs(session, [item.id])).get(item.id, [])
    ids = {getattr(item, c, None) for c in CONTENT_PEOPLE} | set(assg)
    ids |= {j["assignee_id"] for j in jobs}
    names = await _names_for(session, ids)
    projects = await _projects_map(session, [item.project_id])
    can_edit = True
    if user is not None:
        my_pids = await _my_project_ids(session, user["id"])
        can_edit = _can_edit_content(user, item, my_pids, assg)
    return _content_out(item, names, projects, refs, assg, can_edit, jobs)


@router.patch("/content/{content_id}")
async def update_content(content_id: int, patch: ContentPatch,
                         user: dict = Depends(member), session=Depends(get_session)):
    item = (await session.execute(
        select(ContentItem).where(ContentItem.id == content_id)
    )).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Content not found")
    assg0 = (await _content_assignees(session, [item.id])).get(item.id, [])
    my_pids = await _my_project_ids(session, user["id"])
    if not _can_edit_content(user, item, my_pids, assg0):
        raise HTTPException(403, "можно редактировать только свой контент")
    data = patch.model_dump(exclude_unset=True)
    if "content_kind" in data:
        item.content_kind = _clean(data["content_kind"])
    if "assignee_ids" in data and data["assignee_ids"] is not None:
        await _sync_content_assignees(session, item.id, data["assignee_ids"])
    if "format" in data and data["format"]:
        fmt = FORMAT_ALIASES.get(data["format"].lower(), data["format"].lower())
        if fmt not in CONTENT_FORMATS:
            raise HTTPException(422, f"format must be one of {sorted(CONTENT_FORMATS)}")
        item.format = fmt
    content_stage_changed = None
    if "pipeline_status" in data and data["pipeline_status"]:
        if data["pipeline_status"] not in PIPELINE_STEPS:
            raise HTTPException(422, f"status must be one of {sorted(PIPELINE_STEPS)}")
        if data["pipeline_status"] != item.pipeline_status:
            content_stage_changed = data["pipeline_status"]
        item.pipeline_status = data["pipeline_status"]
    if "publish_date" in data:
        item.publish_date = _parse_date(data["publish_date"])
    if "publish_at" in data:
        item.publish_at = _parse_dt(data["publish_at"])
    if "client_approved" in data and data["client_approved"] is not None:
        item.client_approved = bool(data["client_approved"])
    for f in ("topic", "rubric", "platform", "hook", "script", "caption", "hashtags"):
        if f in data:
            setattr(item, f, _clean(data[f]))
    for f in ("project_id", "client_id", "smm_id", "copywriter_id",
              "editor_id", "designer_id"):
        if f in data:
            setattr(item, f, data[f] or None)
    if "project_id" in data and data["project_id"] and "client_id" not in data:
        item.client_id = (await session.execute(
            select(Project.client_id).where(Project.id == data["project_id"]))).scalar_one_or_none()
    item.updated_at = _now()
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "ссылка на несуществующий id")
    await session.refresh(item)
    await _log(session, "content", "updated", item.id, user["id"], item.topic)
    if content_stage_changed:
        await _log_status(session, "content", item.id, content_stage_changed, user["id"])
    return await _one_content(session, item, user)


@router.get("/ideas")
async def list_ideas(scope: str | None = None,
                     user: dict = Depends(member), session=Depends(get_session)):
    q = select(Idea)
    if scope == "mine":
        mine = await _my_project_ids(session, user["id"])
        q = q.where(or_(Idea.proposed_by == user["id"],
                        Idea.project_id.in_(mine) if mine else False))
    rows = (await session.execute(
        q.order_by(Idea.votes_count.desc(), Idea.created_at.desc()).limit(200)
    )).scalars().all()
    voted = set()
    if user["id"]:
        voted = set((await session.execute(
            select(IdeaVote.idea_id).where(IdeaVote.user_id == user["id"])
        )).scalars().all())
    proposers = await _names_for(session, [r.proposed_by for r in rows])
    refs = await _ref_summaries(session, idea_ids=[r.id for r in rows])
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["voted"] = r.id in voted
        d["proposed_by_name"] = proposers.get(r.proposed_by)
        s = refs.get(r.id, {})
        d["refs_count"] = s.get("count", 0)
        d["ref_thumbs"] = s.get("thumbs", [])
        out.append(d)
    return out


@router.post("/ideas/{idea_id}/vote")
async def vote_idea(idea_id: int,
                    user: dict = Depends(member), session=Depends(get_session)):
    if not user["id"]:
        raise HTTPException(403, "Register in the bot before voting")
    idea = (await session.execute(
        select(Idea).where(Idea.id == idea_id)
    )).scalar_one_or_none()
    if not idea:
        raise HTTPException(404, "Idea not found")
    existing = (await session.execute(
        select(IdeaVote).where(IdeaVote.idea_id == idea_id,
                               IdeaVote.user_id == user["id"])
    )).scalar_one_or_none()
    if existing:
        await session.delete(existing)
        idea.votes_count = max(0, (idea.votes_count or 0) - 1)
        voted = False
    else:
        session.add(IdeaVote(idea_id=idea_id, user_id=user["id"], emoji="👍"))
        idea.votes_count = (idea.votes_count or 0) + 1
        voted = True
    idea.updated_at = _now()
    await session.commit()
    if voted:
        await _log(session, "idea", "voted", idea_id, user["id"], idea.title)
    return {"idea_id": idea_id, "votes_count": idea.votes_count, "voted": voted}


@router.get("/blockers")
async def list_blockers(user: dict = Depends(member), session=Depends(get_session)):
    rows = (await session.execute(
        select(Blocker).where(Blocker.status == "active")
        .order_by(Blocker.created_at.desc()).limit(200)
    )).scalars().all()
    names = await _names_for(
        session, [i for r in rows for i in (r.reported_by, r.assigned_to)]
    )
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["reported_by_name"] = names.get(r.reported_by)
        d["assigned_to_name"] = names.get(r.assigned_to)
        out.append(d)
    return out


# ── creation (from the Mini App forms) ─────────────────────────

async def _commit_new(session, obj):
    session.add(obj)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "a referenced id (project_id / client_id / assigned_to) does not exist")
    await session.refresh(obj)
    return row_to_dict(obj)


async def _log_status(session, entity, entity_id, status, actor_id):
    """Append to status_events for the executor-timing view (best-effort)."""
    try:
        session.add(StatusEvent(entity=entity, entity_id=entity_id,
                                status=status, actor_id=actor_id or None))
        await session.commit()
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        print(f"status log failed ({entity}/{entity_id}): {e}")


async def _timeline(session, entity, entity_id):
    rows = (await session.execute(
        select(StatusEvent).where(StatusEvent.entity == entity,
                                  StatusEvent.entity_id == entity_id)
        .order_by(StatusEvent.created_at, StatusEvent.id))).scalars().all()
    names = await _names_for(session, [r.actor_id for r in rows])
    out, prev = [], None
    for r in rows:
        at = r.created_at
        dur = None
        if prev is not None and at is not None:
            dur = int((at - prev).total_seconds())
        out.append({"status": r.status, "at": at.isoformat() if at else None,
                    "actor_name": names.get(r.actor_id), "since_prev_sec": dur})
        prev = at
    return out


async def _log(session, entity, action, entity_id, actor_id, title=None):
    """Append an activity_events row + commit. Best-effort — never raises."""
    try:
        session.add(ActivityEvent(
            entity=entity, action=action, entity_id=entity_id,
            actor_id=actor_id or None,
            payload={"title": title} if title else {},
        ))
        await session.commit()
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        print(f"activity log failed ({entity}/{action}): {e}")


async def _tg_send(chat_id: int, text: str, thread_id: int | None = None):
    """Fire a Telegram message (DM or group/topic). Best-effort — never raises."""
    if not (BOT_TOKEN and chat_id):
        return
    payload = {"chat_id": chat_id, "text": text}
    if thread_id:
        payload["message_thread_id"] = thread_id
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload,
            )
        if r.status_code != 200:
            print(f"notify {chat_id}: telegram {r.status_code} {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"notify {chat_id} failed: {e}")


async def _project_chats(session, project_id):
    """[(chat_id, thread_id), ...] bound to this project."""
    if not project_id:
        return []
    rows = (await session.execute(
        select(ProjectChat.chat_id, ProjectChat.thread_id)
        .where(ProjectChat.project_id == project_id)
    )).all()
    return [(c, t) for c, t in rows]


async def _notify_project(bg: BackgroundTasks, session, project_id, text: str):
    for chat_id, thread_id in await _project_chats(session, project_id):
        bg.add_task(_tg_send, chat_id, text, thread_id)


async def _telegram_id_for(session, user_id):
    if not user_id:
        return None
    return (await session.execute(
        select(User.telegram_id).where(User.id == user_id)
    )).scalar_one_or_none()


@router.post("/tasks", status_code=201)
async def create_task(body: TaskCreate, bg: BackgroundTasks,
                      user: dict = Depends(member), session=Depends(get_session)):
    title = _clean(body.title) or (_clean(body.description) or "").split("\n")[0][:80] or "Задача"
    prio = (_clean(body.priority) or "normal").lower()
    prio = PRIORITY_ALIASES.get(prio, prio)
    if prio not in TASK_PRIORITIES:
        raise HTTPException(422, f"priority must be one of {sorted(TASK_PRIORITIES)}")
    uid = user["id"] or None
    # assignees: explicit list from the picker, else the single field, else self-assign
    aids = list(dict.fromkeys(body.assignee_ids or ([] if body.assignee_id is None else [body.assignee_id])))
    if not aids and uid:
        aids = [uid]
    deadline = _parse_dt(body.deadline)
    project_id = body.project_id
    if not project_id and body.client_id:
        project_id = (await session.execute(
            select(Project.id).where(Project.client_id == body.client_id,
                                     Project.is_active.is_(True))
            .order_by(Project.id).limit(1))).scalar_one_or_none()
    obj = Task(
        title=title,
        description=_clean(body.description),
        priority=prio,
        deadline=deadline,
        status="pending",
        created_by=uid,
        assignee_id=aids[0] if aids else None,   # keep the single field = first assignee
        client_id=body.client_id,
        project_id=project_id,
    )
    result = await _commit_new(session, obj)
    await _log_status(session, "task", obj.id, "pending", uid)
    for a in aids:
        session.add(TaskAssignee(task_id=obj.id, user_id=a))
    ref_url = _clean(body.reference_url)
    if ref_url:
        session.add(ReferenceItem(task_id=obj.id, kind="link", url=ref_url, added_by=uid))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "один из исполнителей не найден")
    await _log(session, "task", "created", obj.id, uid, title)

    dl = deadline.strftime("%d.%m.%Y") if deadline else "без срока"
    for a in aids:
        tg = await _telegram_id_for(session, a)
        if tg:
            bg.add_task(_tg_send, tg,
                        f"📋 Тебе назначена задача: {title}\nПриоритет: {prio}\nДедлайн: {dl}")
    who = ", ".join(filter(None, (await _names_for(session, aids)).values())) or "—"
    await _notify_project(
        bg, session, obj.project_id,
        f"📋 Новая задача: {title}\nИсполнители: {who}\nДедлайн: {dl}")
    result = await _attach_assignees(session, [obj])
    return result[0]


@router.post("/content", status_code=201)
async def create_content(body: ContentCreate,
                         user: dict = Depends(member), session=Depends(get_session)):
    fmt = (_clean(body.format) or "").lower()
    fmt = FORMAT_ALIASES.get(fmt, fmt)
    if fmt not in CONTENT_FORMATS:
        raise HTTPException(422, f"format must be one of {sorted(CONTENT_FORMATS)}")
    uid = user["id"] or None
    client_id = body.client_id
    if body.project_id and not client_id:
        client_id = (await session.execute(
            select(Project.client_id).where(Project.id == body.project_id))).scalar_one_or_none()
    obj = ContentItem(
        format=fmt,
        topic=_clean(body.topic),
        publish_date=_parse_date(body.publish_date),
        publish_at=_parse_dt(body.publish_at),
        project_id=body.project_id,
        client_id=client_id,
        pipeline_status=CONTENT_START,
        created_by=uid,
        author_id=uid,
        content_kind=_clean(body.content_kind),
        rubric=_clean(body.rubric),
        platform=_clean(body.platform),
        hook=_clean(body.hook),
        script=_clean(body.script),
        caption=_clean(body.caption),
        hashtags=_clean(body.hashtags),
    )
    session.add(obj)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "ссылка на несуществующий id")
    await session.refresh(obj)
    # assignees — stored, but NOT notified until the content is advanced
    if body.assignee_ids:
        await _sync_content_assignees(session, obj.id, body.assignee_ids)
    ref_url = _clean(body.reference_url)
    if ref_url:
        if not ref_url.startswith(("http://", "https://")):
            ref_url = "https://" + ref_url
        session.add(ReferenceItem(content_id=obj.id, kind="link", url=ref_url,
                                  title=ref_url, added_by=uid))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "исполнитель не найден")
    await _log(session, "content", "created", obj.id, uid, obj.topic)
    await _log_status(session, "content", obj.id, CONTENT_START, uid)
    return await _one_content(session, obj, user)


@router.post("/ideas", status_code=201)
async def create_idea(body: IdeaCreate,
                      user: dict = Depends(member), session=Depends(get_session)):
    title = _clean(body.title)
    if not title:
        raise HTTPException(422, "title is required")
    fmt = _clean(body.format)
    if fmt:
        fmt = FORMAT_ALIASES.get(fmt.lower(), fmt.lower())
        if fmt not in CONTENT_FORMATS:
            raise HTTPException(422, f"format must be one of {sorted(CONTENT_FORMATS)}")
    uid = user["id"] or None
    obj = Idea(
        title=title,
        description=_clean(body.description),
        format=fmt,
        status="new",
        proposed_by=uid,
        project_id=body.project_id,
        votes_count=0,
    )
    await _commit_new(session, obj)
    ref_url = _clean(body.reference_url)
    if ref_url:
        if not ref_url.startswith(("http://", "https://")):
            ref_url = "https://" + ref_url
        session.add(ReferenceItem(idea_id=obj.id, kind="link", url=ref_url,
                                  title=ref_url, added_by=uid))
        await session.commit()
    await _log(session, "idea", "created", obj.id, uid, title)
    d = row_to_dict(obj)
    d["proposed_by_name"] = user.get("full_name")
    return d


@router.post("/blockers", status_code=201)
async def create_blocker(body: BlockerCreate, bg: BackgroundTasks,
                         user: dict = Depends(member), session=Depends(get_session)):
    title = _clean(body.title)
    if not title:
        raise HTTPException(422, "title is required")
    uid = user["id"] or None
    desc = _clean(body.description)
    obj = Blocker(
        title=title,
        description=desc,
        status="active",
        reported_by=uid,
        assigned_to=body.assigned_to,
    )
    result = await _commit_new(session, obj)
    await _log(session, "blocker", "created", obj.id, uid, title)
    tg = await _telegram_id_for(session, obj.assigned_to)
    if tg:
        bg.add_task(_tg_send, tg, f"🚫 На тебе блокер: {title}\n{desc or ''}".rstrip())
    return result


# ── clients & projects ──────────────────────────────────────────

def _client_out(c, am_name=None):
    return {"id": c.id, "name": c.name, "contact": c.contact, "notes": c.notes,
            "am_id": c.am_id, "am_name": am_name, "is_active": c.is_active}


def _project_out(p, client_name=None, bound=0, is_mine=False):
    return {"id": p.id, "client_id": p.client_id, "name": p.name,
            "client_name": client_name,
            "label": f"{client_name} — {p.name}" if client_name else p.name,
            "bound_chats": bound, "is_mine": is_mine,
            "monthly_posts": getattr(p, "monthly_posts", None),
            "description": p.description, "is_active": p.is_active}


@router.get("/clients")
async def list_clients(user: dict = Depends(member), session=Depends(get_session)):
    rows = (await session.execute(
        select(Client).order_by(Client.is_active.desc(), Client.name)
    )).scalars().all()
    projs = {}
    prows = []
    if rows:
        prows = (await session.execute(
            select(Project.id, Project.client_id, Project.name, Project.am_id,
                   Project.monthly_posts)
            .where(Project.client_id.in_([c.id for c in rows]),
                   Project.is_active.is_(True))
            .order_by(Project.name))).all()
    names = await _names_for(
        session, [c.am_id for c in rows] + [r[3] for r in prows])
    prefs = await _ref_summaries(session, project_ids=[r[0] for r in prows])
    for pid, cid, pn, pam, mp in prows:
        projs.setdefault(cid, []).append(
            {"id": pid, "name": pn, "am_id": pam, "am_name": names.get(pam),
             "monthly_posts": mp, "refs_count": prefs.get(pid, {}).get("count", 0)})
    out = []
    for c in rows:
        d = _client_out(c, names.get(c.am_id))
        d["projects"] = projs.get(c.id, [])
        out.append(d)
    return out


@router.post("/clients", status_code=201)
async def create_client(body: ClientCreate,
                        user: dict = Depends(member), session=Depends(get_session)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "only admin / am can add clients")
    name = _clean(body.name)
    if not name:
        raise HTTPException(422, "name is required")
    uid = user["id"] or None
    client = Client(name=name, contact=_clean(body.contact),
                    notes=_clean(body.notes), am_id=uid, is_active=True)
    session.add(client)
    await session.flush()
    project = Project(client_id=client.id, name=_clean(body.project_name) or name,
                      am_id=uid, is_active=True)
    session.add(project)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "не удалось создать клиента")
    await session.refresh(client)
    await session.refresh(project)
    await _log(session, "project", "created", project.id, uid, project.name)
    out = _client_out(client, user.get("full_name"))
    out["projects"] = [{"id": project.id, "name": project.name}]
    return out


@router.get("/projects")
async def list_projects(client_id: int | None = None, active: bool = True,
                        user: dict = Depends(member), session=Depends(get_session)):
    q = select(Project)
    if client_id:
        q = q.where(Project.client_id == client_id)
    if active:
        q = q.where(Project.is_active.is_(True))
    q = q.order_by(Project.name)
    rows = (await session.execute(q)).scalars().all()
    cnames = await _client_names(session, [p.client_id for p in rows])
    mine = await _my_project_ids(session, user["id"])
    bcount = {}
    if rows:
        for pid, n in (await session.execute(
            select(ProjectChat.project_id, func.count())
            .where(ProjectChat.project_id.in_([p.id for p in rows]))
            .group_by(ProjectChat.project_id))).all():
            bcount[pid] = n
    return [_project_out(p, cnames.get(p.client_id), bcount.get(p.id, 0), p.id in mine)
            for p in rows]


async def _client_names(session, ids):
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {i: n for i, n in (await session.execute(
        select(Client.id, Client.name).where(Client.id.in_(ids)))).all()}


@router.post("/projects", status_code=201)
async def create_project(body: ProjectCreate,
                         user: dict = Depends(member), session=Depends(get_session)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "only admin / am can add projects")
    name = _clean(body.name)
    if not name:
        raise HTTPException(422, "name is required")
    obj = Project(
        client_id=body.client_id,
        name=name,
        description=_clean(body.description),
        am_id=user["id"] or None,
        is_active=True,
    )
    result = await _commit_new(session, obj)
    await _log(session, "project", "created", obj.id, user["id"], name)
    return result


async def _client_with_projects(session, c):
    rows = (await session.execute(
        select(Project.id, Project.name, Project.am_id, Project.monthly_posts)
        .where(Project.client_id == c.id, Project.is_active.is_(True))
        .order_by(Project.name))).all()
    names = await _names_for(session, [c.am_id] + [r[2] for r in rows])
    refs = await _ref_summaries(session, project_ids=[r[0] for r in rows])
    d = _client_out(c, names.get(c.am_id))
    d["projects"] = [{"id": pid, "name": pn, "am_id": pam, "am_name": names.get(pam),
                      "monthly_posts": mp, "refs_count": refs.get(pid, {}).get("count", 0)}
                     for pid, pn, pam, mp in rows]
    return d


@router.patch("/clients/{client_id}")
async def update_client(client_id: int, patch: ClientPatch,
                        user: dict = Depends(member), session=Depends(get_session)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "only admin / am can edit clients")
    c = (await session.execute(
        select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if not c:
        raise HTTPException(404, "client not found")
    data = patch.model_dump(exclude_unset=True)
    if "name" in data and _clean(data["name"]):
        c.name = _clean(data["name"])
    for f in ("contact", "notes"):
        if f in data:
            setattr(c, f, _clean(data[f]))
    if "is_active" in data and data["is_active"] is not None:
        c.is_active = bool(data["is_active"])
    if "am_id" in data:
        c.am_id = data["am_id"] or None
        # keep the client's projects on the same AM (1 client ≈ 1 project)
        await session.execute(sa_update(Project)
                              .where(Project.client_id == client_id)
                              .values(am_id=c.am_id))
    if "monthly_posts" in data:
        mp = data["monthly_posts"]
        await session.execute(sa_update(Project)
                              .where(Project.client_id == client_id)
                              .values(monthly_posts=(int(mp) if mp not in (None, "", 0) else None)))
    c.updated_at = _now()
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "ссылка на несуществующий id")
    await session.refresh(c)
    return await _client_with_projects(session, c)


@router.patch("/projects/{project_id}")
async def update_project(project_id: int, patch: ProjectPatch,
                         user: dict = Depends(member), session=Depends(get_session)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "only admin / am can edit projects")
    p = (await session.execute(
        select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "project not found")
    data = patch.model_dump(exclude_unset=True)
    if "name" in data and _clean(data["name"]):
        p.name = _clean(data["name"])
    if "description" in data:
        p.description = _clean(data["description"])
    if "is_active" in data and data["is_active"] is not None:
        p.is_active = bool(data["is_active"])
    if "am_id" in data:
        p.am_id = data["am_id"] or None
    if "monthly_posts" in data:
        mp = data["monthly_posts"]
        p.monthly_posts = int(mp) if mp not in (None, "", 0) else None
    p.updated_at = _now()
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "ссылка на несуществующий id")
    await session.refresh(p)
    cn = await _client_names(session, [p.client_id])
    return _project_out(p, cn.get(p.client_id))


# ── members (lightweight list for assignee pickers — any member) ─

@router.get("/members")
async def list_members(user: dict = Depends(member), session=Depends(get_session)):
    rows = (await session.execute(
        select(User).where(User.is_active.is_(True)).order_by(User.full_name)
    )).scalars().all()
    return [{"id": u.id, "full_name": u.full_name, "role": u.role} for u in rows]


# ── shoots (Съёмки) ────────────────────────────────────────────

async def _attach_shoot(session, shoots):
    if not shoots:
        return []
    ids = [s.id for s in shoots]
    parts = (await session.execute(
        select(ShootParticipant.shoot_id, ShootParticipant.user_id)
        .where(ShootParticipant.shoot_id.in_(ids)))).all()
    by_shoot = {}
    for sid, uid in parts:
        by_shoot.setdefault(sid, []).append(uid)
    names = await _names_for(session, {u for lst in by_shoot.values() for u in lst})
    cnames = await _client_names(session, [s.client_id for s in shoots])
    pnames = await _projects_map(session, [s.project_id for s in shoots])
    out = []
    for s in shoots:
        d = row_to_dict(s)
        d["shoot_at"] = s.shoot_at.isoformat() if s.shoot_at else None
        d["participants"] = [{"id": i, "name": names.get(i)}
                             for i in by_shoot.get(s.id, [])]
        d["client_name"] = cnames.get(s.client_id)
        d["project_name"] = pnames.get(s.project_id)
        out.append(d)
    return out


@router.get("/shoots")
async def list_shoots(upcoming: bool = False,
                      user: dict = Depends(member), session=Depends(get_session)):
    q = select(ShootSession)
    if upcoming:
        q = q.where(or_(ShootSession.shoot_at.is_(None), ShootSession.shoot_at >= _now()),
                    ShootSession.status == "planned")
    q = q.order_by(ShootSession.shoot_at.is_(None), ShootSession.shoot_at, ShootSession.id.desc()).limit(200)
    rows = (await session.execute(q)).scalars().all()
    return await _attach_shoot(session, rows)


async def _sync_shoot_participants(session, shoot_id, ids):
    ids = list(dict.fromkeys(ids or []))
    await session.execute(sa_delete(ShootParticipant).where(ShootParticipant.shoot_id == shoot_id))
    for uid in ids:
        session.add(ShootParticipant(shoot_id=shoot_id, user_id=uid))
    return ids


@router.post("/shoots", status_code=201)
async def create_shoot(body: ShootCreate, bg: BackgroundTasks,
                       user: dict = Depends(member), session=Depends(get_session)):
    title = _clean(body.title)
    if not title:
        raise HTTPException(422, "title is required")
    project_id = body.project_id or None
    if not project_id and body.client_id:
        project_id = (await session.execute(
            select(Project.id).where(Project.client_id == body.client_id,
                                     Project.is_active.is_(True))
            .order_by(Project.id).limit(1))).scalar_one_or_none()
    obj = ShootSession(
        title=title,
        shoot_at=_parse_dt(body.shoot_at),
        location=_clean(body.location),
        client_id=body.client_id or None,
        project_id=project_id,
        notes=_clean(body.notes),
        status="planned",
        created_by=user["id"] or None,
    )
    session.add(obj)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "ссылка на несуществующий id")
    await session.refresh(obj)
    pids = await _sync_shoot_participants(session, obj.id, body.participant_ids)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "участник не найден")
    await _log(session, "task", "created", obj.id, user["id"], f"Съёмка: {title}")
    when = obj.shoot_at.strftime("%d.%m %H:%M") if obj.shoot_at else "дата не задана"
    for uid in pids:
        tg = await _telegram_id_for(session, uid)
        if tg:
            bg.add_task(_tg_send, tg,
                        f"🎬 Съёмка: {title}\nКогда: {when}\nМесто: {obj.location or '—'}")
    await _notify_project(bg, session, obj.project_id,
                          f"🎬 Съёмка запланирована: {title}\nКогда: {when}")
    return (await _attach_shoot(session, [obj]))[0]


@router.patch("/shoots/{shoot_id}")
async def update_shoot(shoot_id: int, patch: ShootPatch, bg: BackgroundTasks,
                       user: dict = Depends(member), session=Depends(get_session)):
    obj = (await session.execute(
        select(ShootSession).where(ShootSession.id == shoot_id))).scalar_one_or_none()
    if not obj:
        raise HTTPException(404, "Shoot not found")
    data = patch.model_dump(exclude_unset=True)
    if "title" in data and _clean(data["title"]):
        obj.title = _clean(data["title"])
    if "shoot_at" in data:
        obj.shoot_at = _parse_dt(data["shoot_at"])
    if "location" in data:
        obj.location = _clean(data["location"])
    if "notes" in data:
        obj.notes = _clean(data["notes"])
    for f in ("client_id", "project_id"):
        if f in data:
            setattr(obj, f, data[f] or None)
    if "status" in data and data["status"]:
        if data["status"] not in SHOOT_STATUSES:
            raise HTTPException(422, f"status must be one of {sorted(SHOOT_STATUSES)}")
        obj.status = data["status"]
    obj.updated_at = _now()
    if "participant_ids" in data:
        await _sync_shoot_participants(session, obj.id, data["participant_ids"])
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "ссылка на несуществующий id")
    await session.refresh(obj)
    return (await _attach_shoot(session, [obj]))[0]


# ── project ↔ chat bindings (read-only; writes come from the bot /bind) ─

@router.get("/project-chats")
async def list_project_chats(user: dict = Depends(member), session=Depends(get_session)):
    rows = (await session.execute(select(ProjectChat))).scalars().all()
    pnames = await _projects_map(session, [r.project_id for r in rows])
    return [{"id": r.id, "project_id": r.project_id,
             "project_name": pnames.get(r.project_id),
             "chat_id": r.chat_id, "thread_id": r.thread_id, "title": r.title}
            for r in rows]


# ── references (links + files) ──────────────────────────────────

def _ref_out(r, name=None):
    return {
        "id": r.id, "kind": r.kind, "url": r.url, "title": r.title,
        "file_name": r.file_name, "mime": r.mime, "added_by_name": name,
        "task_id": r.task_id, "content_id": r.content_id, "idea_id": r.idea_id, "project_id": r.project_id,
        "download": f"/api/references/{r.id}/file" if r.kind == "file" else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def _ref_scope(body_task, body_content, body_idea=None, body_project=None):
    if not (body_task or body_content or body_idea or body_project):
        raise HTTPException(422, "task_id / content_id / idea_id / project_id обязателен")


@router.get("/references")
async def list_references(task_id: int | None = None, content_id: int | None = None,
                          idea_id: int | None = None, project_id: int | None = None,
                          user: dict = Depends(member), session=Depends(get_session)):
    await _ref_scope(task_id, content_id, idea_id, project_id)
    q = select(ReferenceItem)
    if task_id:
        q = q.where(ReferenceItem.task_id == task_id)
    if content_id:
        q = q.where(ReferenceItem.content_id == content_id)
    if idea_id:
        q = q.where(ReferenceItem.idea_id == idea_id)
    if project_id:
        q = q.where(ReferenceItem.project_id == project_id)
    rows = (await session.execute(q.order_by(ReferenceItem.id.desc()))).scalars().all()
    names = await _names_for(session, [r.added_by for r in rows])
    return [_ref_out(r, names.get(r.added_by)) for r in rows]


@router.post("/references", status_code=201)
async def add_link_reference(body: LinkRef,
                             user: dict = Depends(member), session=Depends(get_session)):
    await _ref_scope(body.task_id, body.content_id, body.idea_id, body.project_id)
    url = _clean(body.url)
    if not url:
        raise HTTPException(422, "url обязателен")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    obj = ReferenceItem(kind="link", url=url, title=_clean(body.title) or url,
                        task_id=body.task_id, content_id=body.content_id,
                        idea_id=body.idea_id, project_id=body.project_id,
                        added_by=user["id"] or None)
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return _ref_out(obj, user.get("full_name"))


@router.post("/references/upload", status_code=201)
async def upload_file_reference(
        file: UploadFile = File(...),
        task_id: int | None = Form(None),
        content_id: int | None = Form(None),
        idea_id: int | None = Form(None),
        project_id: int | None = Form(None),
        user: dict = Depends(member), session=Depends(get_session)):
    await _ref_scope(task_id, content_id, idea_id, project_id)
    if not (BOT_TOKEN and user.get("telegram_id")):
        raise HTTPException(400, "загрузка файлов недоступна")
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "файл больше 20 МБ")
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
            data={"chat_id": user["telegram_id"], "caption": f"📎 референс: {file.filename}"},
            files={"document": (file.filename, raw, file.content_type or "application/octet-stream")},
        )
    j = r.json()
    if not j.get("ok"):
        raise HTTPException(502, f"Telegram: {j.get('description')}")
    res = j["result"]
    doc = res.get("document") or (res.get("photo") or [{}])[-1] or {}
    fid = doc.get("file_id")
    if not fid:
        raise HTTPException(502, "Telegram не вернул file_id")
    obj = ReferenceItem(
        kind="file", tg_file_id=fid,
        file_name=doc.get("file_name") or file.filename,
        mime=doc.get("mime_type") or file.content_type,
        task_id=task_id, content_id=content_id, idea_id=idea_id, project_id=project_id,
        added_by=user["id"] or None,
    )
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return _ref_out(obj, user.get("full_name"))


@router.get("/references/{ref_id}/file")
async def reference_file(ref_id: int, request: Request, ia: str | None = None,
                         session=Depends(get_session)):
    # auth via header / ?ia= query (so plain <a>/<img> links work) / session cookie
    init_data = (request.headers.get("X-Init-Data")
                 or request.headers.get("X-Telegram-Init-Data") or ia or "")
    ok = not BOT_TOKEN
    if init_data and BOT_TOKEN:
        try:
            ok = bool(_validate_init_data(init_data).get("id"))
        except HTTPException:
            ok = False
    if not ok:
        tok = request.cookies.get("wn_session")
        ok = bool(tok and _read_session(tok))
    if not ok:
        raise HTTPException(403, "нет доступа")

    r = (await session.execute(
        select(ReferenceItem).where(ReferenceItem.id == ref_id)
    )).scalar_one_or_none()
    if not r or r.kind != "file" or not r.tg_file_id:
        raise HTTPException(404, "файл не найден")
    async with httpx.AsyncClient(timeout=60) as c:
        gf = (await c.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                          params={"file_id": r.tg_file_id})).json()
        path = gf.get("result", {}).get("file_path")
        if not path:
            raise HTTPException(410, "файл больше недоступен")
        fr = await c.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}")
    return Response(
        content=fr.content, media_type=r.mime or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{(r.file_name or "file")}"',
            "Cache-Control": "private, max-age=300",
        },
    )


@router.delete("/references/{ref_id}")
async def delete_reference(ref_id: int,
                           user: dict = Depends(member), session=Depends(get_session)):
    r = (await session.execute(
        select(ReferenceItem).where(ReferenceItem.id == ref_id)
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "не найдено")
    if not (user["role"] in MANAGER_ROLES or r.added_by == user["id"]):
        raise HTTPException(403, "можно удалить только свой референс")
    await session.delete(r)
    await session.commit()
    return {"ok": True}


# ── team ────────────────────────────────────────────────────────

class TeamMemberCreate(BaseModel):
    telegram_id: int
    full_name: str
    role: str = "smm"


def _user_out(u):
    return {"id": u.id, "telegram_id": u.telegram_id, "full_name": u.full_name,
            "username": u.username, "role": u.role, "is_active": u.is_active}


@router.get("/team")
async def list_team(user: dict = Depends(member), session=Depends(get_session)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "team list is for admin / am only")
    rows = (await session.execute(
        select(User).where(User.is_active.is_(True))
        .order_by(User.role, User.full_name)
    )).scalars().all()
    return [_user_out(u) for u in rows]


@router.patch("/team/{user_id}/role")
async def set_team_role(user_id: int, body: TeamRolePatch,
                        user: dict = Depends(member), session=Depends(get_session)):
    """Change a teammate's role. admin / am."""
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "only admin / am can change roles")
    if body.role not in USER_ROLES:
        raise HTTPException(422, f"role must be one of {sorted(USER_ROLES)}")
    target = (await session.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if not target:
        raise HTTPException(404, "user not found")
    is_self = target.id == user["id"]
    if is_self and body.role not in MANAGER_ROLES:
        raise HTTPException(400, "нельзя понизить свою роль ниже управляющей")
    if user["role"] in ("am", "director") and (target.role == "admin" or body.role == "admin") and not is_self:
        raise HTTPException(403, "роль admin может назначать только admin")
    target.role = body.role
    if body.full_name is not None:
        name = _clean(body.full_name)
        if name:
            target.full_name = name
    target.updated_at = _now()
    await session.commit()
    await session.refresh(target)
    return _user_out(target)


@router.post("/team", status_code=201)
async def add_team_member(body: TeamMemberCreate,
                          user: dict = Depends(member), session=Depends(get_session)):
    """Add a teammate by Telegram ID so they get access. admin / am only."""
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "only admin / am can add members")
    if body.role not in USER_ROLES:
        raise HTTPException(422, f"role must be one of {sorted(USER_ROLES)}")
    exists = (await session.execute(
        select(User).where(User.telegram_id == body.telegram_id)
    )).scalar_one_or_none()
    if exists and exists.is_active:
        raise HTTPException(409, "user with this Telegram ID already exists")
    if exists:  # was removed earlier — re-activate
        exists.is_active = True
        exists.role = body.role
        exists.full_name = _clean(body.full_name) or exists.full_name
        exists.updated_at = _now()
        await session.commit()
        await session.refresh(exists)
        return _user_out(exists)
    obj = User(
        telegram_id=body.telegram_id,
        full_name=_clean(body.full_name) or f"User {body.telegram_id}",
        role=body.role,
        is_active=True,
    )
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    await _log(session, "user", "created", obj.id, user["id"], obj.full_name)
    return _user_out(obj)


@router.delete("/team/{user_id}")
async def remove_team_member(user_id: int,
                             user: dict = Depends(member), session=Depends(get_session)):
    """Revoke a teammate's access (soft delete). admin / am."""
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "only admin / am can remove members")
    if user_id == user["id"]:
        raise HTTPException(400, "нельзя удалить себя")
    target = (await session.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if not target:
        raise HTTPException(404, "user not found")
    if user["role"] == "am" and target.role == "admin":
        raise HTTPException(403, "am не может удалить admin")
    target.is_active = False
    target.updated_at = _now()
    await session.commit()
    await _log(session, "user", "deleted", user_id, user["id"], target.full_name)
    return {"ok": True, "id": user_id}


# ── dashboard & activity ────────────────────────────────────────

def _activity_out(a, name):
    return {
        "id": a.id, "entity": a.entity, "action": a.action,
        "entity_id": a.entity_id, "actor_id": a.actor_id, "actor_name": name,
        "title": (a.payload or {}).get("title"),
        "happened_at": a.happened_at.isoformat() if a.happened_at else None,
    }


@router.get("/activity")
async def list_activity(limit: int = 20,
                        user: dict = Depends(member), session=Depends(get_session)):
    limit = max(1, min(limit, 100))
    rows = (await session.execute(
        select(ActivityEvent)
        .order_by(ActivityEvent.happened_at.desc(), ActivityEvent.id.desc())
        .limit(limit)
    )).scalars().all()
    names = await _names_for(session, [a.actor_id for a in rows])
    return [_activity_out(a, names.get(a.actor_id)) for a in rows]


@router.get("/dashboard")
async def dashboard(user: dict = Depends(member), session=Depends(get_session)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "dashboard is for admin / am only")

    now = _now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async def count(model, *where):
        return (await session.scalar(
            select(func.count()).select_from(model).where(*where))) or 0

    open_tasks = await count(Task, Task.status.in_(("pending", "in_progress")))
    active_blockers = await count(Blocker, Blocker.status == "active")
    content_in_progress = await count(
        ContentItem, ContentItem.pipeline_status.notin_(("done", "published")))
    active_ideas = await count(Idea, Idea.status.in_(("new", "under_review")))

    users = (await session.execute(
        select(User).where(User.is_active.is_(True)).order_by(User.full_name)
    )).scalars().all()
    workload = []
    for u in users:
        workload.append({
            "user_id": u.id,
            "name": u.full_name,
            "open_tasks": await count(
                Task, Task.assignee_id == u.id,
                Task.status.in_(("pending", "in_progress", "overdue"))),
            "total_this_month": await count(
                Task, Task.assignee_id == u.id, Task.created_at >= month_start),
            "done_this_month": await count(
                Task, Task.assignee_id == u.id, Task.created_at >= month_start,
                Task.status == "done"),
        })

    rows = (await session.execute(
        select(ContentItem.pipeline_status, func.count())
        .group_by(ContentItem.pipeline_status)
    )).all()
    counts = {k: v for k, v in rows}
    pipeline_breakdown = {step: counts.get(step, 0) for step in PIPELINE_ORDER}

    # publication plan: target (monthly_posts) vs published this month, per project
    plan_rows = (await session.execute(
        select(Project.id, Project.name, Project.am_id, Project.monthly_posts)
        .where(Project.is_active.is_(True), Project.monthly_posts > 0)
        .order_by(Project.name))).all()
    pnames = await _names_for(session, [r[2] for r in plan_rows])
    pub_rows = (await session.execute(
        select(ContentItem.project_id, func.count())
        .where(ContentItem.pipeline_status == "published",
               ContentItem.updated_at >= month_start)
        .group_by(ContentItem.project_id))).all()
    pub = {pid: n for pid, n in pub_rows}
    publication_plan = [
        {"project_id": pid, "project": pn, "am_name": pnames.get(pam),
         "target": mp or 0, "published": pub.get(pid, 0)}
        for pid, pn, pam, mp in plan_rows]

    acts = (await session.execute(
        select(ActivityEvent)
        .order_by(ActivityEvent.happened_at.desc(), ActivityEvent.id.desc())
        .limit(10)
    )).scalars().all()
    anames = await _names_for(session, [a.actor_id for a in acts])
    recent_activity = [_activity_out(a, anames.get(a.actor_id)) for a in acts]

    return {
        "open_tasks": open_tasks,
        "active_blockers": active_blockers,
        "content_in_progress": content_in_progress,
        "active_ideas": active_ideas,
        "team_workload": workload,
        "pipeline_breakdown": pipeline_breakdown,
        "publication_plan": publication_plan,
        "recent_activity": recent_activity,
    }


# ── admin ───────────────────────────────────────────────────────

def _require_admin(request: Request):
    """Gate on the X-Admin-Secret header matching the ADMIN_SECRET env var."""
    if not ADMIN_SECRET:
        raise HTTPException(503, "ADMIN_SECRET is not configured on the server")
    provided = request.headers.get("X-Admin-Secret", "")
    if not hmac.compare_digest(provided, ADMIN_SECRET):
        raise HTTPException(403, "Invalid admin secret")


@router.patch("/users/{telegram_id}/role")
async def set_user_role(telegram_id: int, patch: RolePatch, request: Request,
                        session=Depends(get_session)):
    """Set (or create) a user's role. Admin-only via X-Admin-Secret header.

    Body: {"role": "admin", "full_name"?: "...", "username"?: "..."}
    Creates the users row if telegram_id is not registered yet — useful for
    bootstrapping the first admin before anyone exists in Postgres.
    """
    _require_admin(request)
    if patch.role not in USER_ROLES:
        raise HTTPException(422, f"role must be one of {sorted(USER_ROLES)}")

    user = (await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )).scalar_one_or_none()

    created = user is None
    if user:
        user.role = patch.role
        if patch.full_name:
            user.full_name = patch.full_name
        if patch.username:
            user.username = patch.username
        user.updated_at = _now()
    else:
        user = User(
            telegram_id=telegram_id,
            full_name=patch.full_name or f"User {telegram_id}",
            username=patch.username,
            role=patch.role,
            is_active=True,
        )
        session.add(user)

    await session.commit()
    await session.refresh(user)
    result = row_to_dict(user)
    result["created"] = created
    return result
