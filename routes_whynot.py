"""
WHY NOT? OS — REST API for the Telegram Mini App.

All routes are mounted under /api. Backed by the async SQLAlchemy models in
db/models.py (Postgres). Auth mirrors the legacy behaviour in api.py:

  * no init-data header            -> dev user (first row in `users`, else id=0)
  * X-Init-Data / X-Telegram-Init-Data present -> validated against BOT_TOKEN
"""
import asyncio, os, json, hmac, hashlib
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from urllib.parse import unquote

import httpx
from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, Request, UploadFile)
from fastapi.responses import Response, RedirectResponse
from pydantic import BaseModel

from link_preview import fetch_preview
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
INIT_DATA_MAX_AGE = 24 * 3600  # seconds; Telegram initData older than this is rejected

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
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if _now().timestamp() - auth_date > INIT_DATA_MAX_AGE:
        raise HTTPException(403, "Сессия Telegram устарела — закройте и откройте приложение заново")
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

TASK_STATUSES = {"pending", "in_progress", "review", "revision", "done", "published", "cancelled", "overdue",
                 "queued"}   # queued = a production-chain step whose turn hasn't come yet
OPEN_TASK_STATUSES = ("pending", "in_progress", "overdue", "revision")
CLOSED_TASK_STATUSES = ("done", "published", "cancelled")
USER_ROLES = {"admin", "am", "director", "editor", "designer",
              "videographer", "mobilographer", "driver", "intern"}
MANAGER_ROLES = ("admin", "am", "director")   # full access: team, clients, dashboard, /bind
TASK_PRIORITIES = set(task_priority_enum.enums)          # low, normal, high, urgent
CONTENT_FORMATS = set(content_format_enum.enums)
# tolerate the labels the Mini App form uses
PRIORITY_ALIASES = {"critical": "urgent", "medium": "normal"}
FORMAT_ALIASES = {"reel": "reels", "video": "youtube_long", "article": "other"}
STATUS_RU = {"pending": "Ожидает", "in_progress": "В работе", "done": "Готово",
             "overdue": "Просрочено", "cancelled": "Отменено",
             "review": "На проверке", "revision": "На доработке", "published": "Опубликовано",
             "queued": "В очереди"}


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
    goals: str | None = None
    audience: str | None = None
    tone_of_voice: str | None = None
    competitors: str | None = None


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
    title: str | None = None   # optional: a pasted link's preview title fills it in
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
    stale = _now() - timedelta(days=2)
    missing = []
    for r in rows:
        key = getattr(r, key_attr)
        s = out.setdefault(key, {"count": 0, "thumbs": []})
        s["count"] += 1
        if r.kind == "file" and (r.mime or "").startswith("image/") and len(s["thumbs"]) < 4:
            s["thumbs"].append({"id": r.id, "download": f"/api/references/{r.id}/file"})
        elif r.kind == "link":
            # link cover (video / photo) as a small thumb; missing or expired covers are
            # fetched in the background so the list itself never waits for them
            if r.preview_image and len(s["thumbs"]) < 4:
                s["thumbs"].append({"id": r.id, "image": r.preview_image, "url": r.url})
            if r.preview_fetched_at is None or (r.preview_image and r.preview_fetched_at < stale):
                missing.append(r.id)
    _spawn_preview_fill(missing)
    return out


_FILLING: set = set()


def _spawn_preview_fill(ref_ids):
    """Fill link previews in the background (own DB session), at most 6 per call."""
    ids = [i for i in ref_ids if i not in _FILLING][:6]
    if not ids:
        return
    _FILLING.update(ids)

    async def run():
        try:
            async with AsyncSessionLocal() as s:
                refs = (await s.execute(
                    select(ReferenceItem).where(ReferenceItem.id.in_(ids)))).scalars().all()
                await _fill_previews(s, refs)
        except Exception as e:  # noqa: BLE001
            print(f"preview fill failed: {e}")
        finally:
            _FILLING.difference_update(ids)
    asyncio.get_running_loop().create_task(run())


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
    all_uids |= {t.created_by for t in tasks}
    names = await _names_for(session, all_uids)
    refs = await _ref_summaries(session, task_ids=ids)
    revs = await _revision_counts(session, ids)
    out = []
    for t in tasks:
        d = row_to_dict(t)
        aids = by_task.get(t.id) or ([t.assignee_id] if t.assignee_id else [])
        d["assignees"] = [{"id": i, "name": names.get(i)} for i in aids]
        d["assignee_name"] = names.get(t.assignee_id) or (d["assignees"][0]["name"] if d["assignees"] else None)
        d["created_by_name"] = names.get(t.created_by)
        s = refs.get(t.id, {})
        d["refs_count"] = s.get("count", 0)
        d["ref_thumbs"] = s.get("thumbs", [])
        d["revisions"] = revs.get(t.id, 0)
        out.append(d)
    return out


async def _revision_counts(session, task_ids) -> dict:
    """task_id -> how many times it was sent back for a revision (from status_events;
    the AM's «🔄 Правка» in the group and in the app both land there)."""
    ids = [i for i in task_ids if i]
    if not ids:
        return {}
    rows = (await session.execute(
        select(StatusEvent.entity_id, func.count())
        .where(StatusEvent.entity == "task", StatusEvent.status == "revision",
               StatusEvent.entity_id.in_(ids))
        .group_by(StatusEvent.entity_id))).all()
    return {tid: n for tid, n in rows}


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
    q = q.where(Task.status != "queued")      # chain steps whose turn hasn't come
    q = q.order_by(Task.status.in_(CLOSED_TASK_STATUSES),
                   Task.deadline.is_(None), Task.deadline, Task.id.desc()).limit(200)
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
        # a chain step is handed over, not reviewed: «Сдать» closes it and starts the next one
        if task.step_no is not None and patch.status == "review":
            task.status = "done"
        if task.status == "done" and task.actual_completion is None:
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
        await _log_status(session, "task", task.id, task.status, user["id"])
        if task.status == "done":
            await _advance_chain(session, bg, task)

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


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int,
                      user: dict = Depends(member), session=Depends(get_session)):
    """Delete a task — a regular one from Главная, or a content-plan job
    (shoot/design/edit, see job_kind). Manager or whoever created it."""
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        return {"ok": True}
    if user["role"] not in MANAGER_ROLES and task.created_by != user["id"]:
        raise HTTPException(403, "удалить задачу может создатель или admin/am/director")
    await session.execute(sa_delete(Task).where(Task.id == task_id))
    await session.commit()
    await _log(session, "task", "deleted", task_id, user["id"], task.title)
    return {"ok": True}


class TaskRevisionBody(BaseModel):
    comment: str | None = None


async def _task_assignee_ids(session, task_id, fallback_assignee_id=None):
    ids = list((await session.execute(
        select(TaskAssignee.user_id).where(TaskAssignee.task_id == task_id))).scalars().all())
    return ids or ([fallback_assignee_id] if fallback_assignee_id else [])


@router.post("/tasks/{task_id}/approve")
async def approve_task(task_id: int, bg: BackgroundTasks,
                       user: dict = Depends(member), session=Depends(get_session)):
    """Manager accepts a submitted file — status -> done."""
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "принимать задачи может только admin/am/director")
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")
    task.status = "done"
    if task.actual_completion is None:
        task.actual_completion = _now()
    task.updated_at = _now()
    await session.commit()
    await _log_status(session, "task", task.id, "done", user["id"])
    await _advance_chain(session, bg, task)
    for a in await _task_assignee_ids(session, task.id, task.assignee_id):
        tg = await _telegram_id_for(session, a)
        if tg:
            bg.add_task(_tg_send, tg, f"✅ Принято! Задача «{task.title}»")
    result = await _attach_assignees(session, [task])
    return result[0]


@router.post("/tasks/{task_id}/revision")
async def revision_task(task_id: int, body: TaskRevisionBody, bg: BackgroundTasks,
                        user: dict = Depends(member), session=Depends(get_session)):
    """Manager sends a submitted file back — status -> revision, with an optional comment."""
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "отправлять на доработку может только admin/am/director")
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")
    comment = _clean(body.comment)
    task.status = "revision"
    task.review_comment = comment
    task.updated_at = _now()
    await session.commit()
    await _log_status(session, "task", task.id, "revision", user["id"])
    text = f"🔄 На доработку: {comment}" if comment else "🔄 Задача возвращена на доработку"
    for a in await _task_assignee_ids(session, task.id, task.assignee_id):
        tg = await _telegram_id_for(session, a)
        if tg:
            bg.add_task(_tg_send, tg, f"{text}\nЗадача: «{task.title}»")
    result = await _attach_assignees(session, [task])
    return result[0]


@router.post("/tasks/{task_id}/publish")
async def publish_task(task_id: int, bg: BackgroundTasks,
                       user: dict = Depends(member), session=Depends(get_session)):
    """Manager marks an accepted task as published."""
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "публиковать может только admin/am/director")
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")
    task.status = "published"
    task.updated_at = _now()
    await session.commit()
    await _log_status(session, "task", task.id, "published", user["id"])
    for a in await _task_assignee_ids(session, task.id, task.assignee_id):
        tg = await _telegram_id_for(session, a)
        if tg:
            bg.add_task(_tg_send, tg, f"🚀 Опубликовано! Задача «{task.title}»")
    result = await _attach_assignees(session, [task])
    return result[0]


@router.get("/tasks/{task_id}/file")
async def task_file(task_id: int, request: Request, ia: str | None = None,
                    session=Depends(get_session)):
    """Proxy a file submitted via a bound Telegram topic (see bot.py submit_file_prompt)."""
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

    t = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not t or not t.file_id:
        raise HTTPException(404, "файл не найден")
    async with httpx.AsyncClient(timeout=60) as c:
        gf = (await c.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                          params={"file_id": t.file_id})).json()
        path = gf.get("result", {}).get("file_path")
        if not path:
            raise HTTPException(410, "файл больше недоступен")
        fr = await c.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}")
    mime = ("video/mp4" if t.file_type == "video"
            else "image/jpeg" if t.file_type == "photo" else "application/octet-stream")
    return Response(
        content=fr.content, media_type=mime,
        headers={
            "Content-Disposition": f'inline; filename="task-{task_id}"',
            "Cache-Control": "private, max-age=300",
        },
    )


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
TASHKENT = timezone(timedelta(hours=5))
CJOB_KINDS = ("shoot", "design", "edit", "ai")
CJOB_RU = {"shoot": "Съёмка", "design": "Дизайн", "edit": "Монтаж", "ai": "ИИ-вставка"}
CJOB_EMOJI = {"shoot": "🎥", "design": "🎨", "edit": "✂️", "ai": "🤖"}
# default production chain per content format (steps run one after another;
# after the last one the content goes to the AM for approval)
CHAIN_TEMPLATES = {
    "reels": ("shoot", "edit"), "tiktok": ("shoot", "edit"), "youtube_short": ("shoot", "edit"),
    "youtube_long": ("shoot", "edit"), "story": ("shoot", "edit"), "podcast": ("shoot", "edit"),
    "post": ("design",), "carousel": ("design",), "banner": ("design",), "other": ("design",),
}


async def _content_jobs(session, cids):
    """content_id -> [job dicts] (linked tasks with a job_kind)."""
    if not cids:
        return {}
    rows = (await session.execute(
        select(Task).where(Task.content_id.in_(cids), Task.job_kind.isnot(None))
        .order_by(Task.step_no.is_(None), Task.step_no, Task.id))).scalars().all()
    by_task = {}
    for tid, uid in (await session.execute(
            select(TaskAssignee.task_id, TaskAssignee.user_id)
            .where(TaskAssignee.task_id.in_([t.id for t in rows])))).all():
        by_task.setdefault(tid, []).append(uid)
    names = await _names_for(session, [t.assignee_id for t in rows]
                             + [u for lst in by_task.values() for u in lst])
    revs = await _revision_counts(session, [t.id for t in rows])
    out = {}
    for t in rows:
        aids = by_task.get(t.id) or ([t.assignee_id] if t.assignee_id else [])
        out.setdefault(t.content_id, []).append({
            "id": t.id, "kind": t.job_kind, "status": t.status, "title": t.title,
            "assignee_id": t.assignee_id, "assignee_name": names.get(t.assignee_id),
            "assignee_ids": aids, "assignee_names": [names.get(a) for a in aids if names.get(a)],
            "revisions": revs.get(t.id, 0),
            "deadline": t.deadline.isoformat() if t.deadline else None,
            "location": t.location, "notes": t.description, "step_no": t.step_no,
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
    d["launched_at"] = c.launched_at.isoformat() if getattr(c, "launched_at", None) else None
    d["archived"] = bool(getattr(c, "archived_at", None))
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
                       scope: str | None = None, archived: bool = False,
                       user: dict = Depends(member), session=Depends(get_session)):
    q = select(ContentItem).where(
        ContentItem.archived_at.isnot(None) if archived else ContentItem.archived_at.is_(None))
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


@router.post("/content/{content_id}/dispatch-jobs", status_code=201)
async def dispatch_content_jobs(content_id: int, bg: BackgroundTasks,
                                user: dict = Depends(member), session=Depends(get_session)):
    """One click: create all 3 production tasks (shoot/design/edit) for a content
    item, unassigned. Assignment then happens like any other task, in Главная."""
    item = (await session.execute(
        select(ContentItem).where(ContentItem.id == content_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Content not found")
    assg = (await _content_assignees(session, [item.id])).get(item.id, [])
    my_pids = await _my_project_ids(session, user["id"])
    if not _can_edit_content(user, item, my_pids, assg):
        raise HTTPException(403, "можно ставить задачи только по своему контенту")
    existing = (await session.execute(
        select(func.count()).select_from(Task)
        .where(Task.content_id == content_id, Task.job_kind.isnot(None))
    )).scalar_one()
    if existing:
        raise HTTPException(400, "задачи уже созданы")

    uid = user["id"] or None
    topic = item.topic or f"контент #{item.id}"
    # carry the content brief over so the executor actually sees the script/hook/caption
    # the AM wrote for the content — they land on the task, not just on the content item
    brief = []
    if item.hook:
        brief.append(f"Хук: {item.hook}")
    if item.script:
        brief.append(item.script)
    if item.caption:
        brief.append(f"Подпись: {item.caption}")
    job_description = "\n\n".join(brief) or None
    objs = []
    for kind in CJOB_KINDS:
        obj = Task(
            title=f"{CJOB_EMOJI[kind]} {CJOB_RU[kind]}: {topic}"[:120],
            description=job_description,
            job_kind=kind, type="content_pipeline", priority="normal",
            status="pending", created_by=uid,
            content_id=item.id, client_id=item.client_id, project_id=item.project_id,
        )
        session.add(obj)
        objs.append(obj)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "ссылка на несуществующий id")
    for obj in objs:
        await session.refresh(obj)
        await _log(session, "task", "created", obj.id, uid, obj.title)
        await _log_status(session, "task", obj.id, "pending", uid)
    await _notify_project(
        bg, session, item.project_id,
        f"🗂 Задачи по контенту «{topic}» созданы: съёмка, дизайн, монтаж — "
        f"назначь исполнителей в приложении.")
    return await _one_content(session, item, user)


# ── content production chain ─────────────────────────────────────
# A content item gets ordered steps (tasks with step_no). «Запустить» makes the first
# step pending; when a step is handed in the next one starts (assignee notified);
# after the last one the content moves to «На одобрении» and the AM is pinged.

def _brief(item):
    parts = []
    if item.hook:
        parts.append(f"Хук: {item.hook}")
    if item.script:
        parts.append(item.script)
    if item.caption:
        parts.append(f"Подпись: {item.caption}")
    return "\n\n".join(parts) or None


def _step_deadline(item, i, n):
    """Default deadline: one day per step, counted back from the publish date (18:00 Tashkent)."""
    pub = item.publish_at or (datetime.combine(item.publish_date, datetime.min.time(), timezone.utc)
                              if item.publish_date else None)
    if not pub:
        return None
    d = (pub - timedelta(days=n - i + 1)).replace(hour=13, minute=0, second=0, microsecond=0)
    return d if d > _now() else None


async def _chain_steps(session, content_id):
    return (await session.execute(select(Task).where(
        Task.content_id == content_id, Task.step_no.isnot(None)).order_by(Task.step_no))).scalars().all()


async def _create_chain(session, item, uid, kinds=None):
    """Planned (queued) steps from the format template; default assignee — the content's
    first assignee (usually one person does every step)."""
    kinds = list(kinds or CHAIN_TEMPLATES.get(item.format or "", ("design",)))
    assg = (await _content_assignees(session, [item.id])).get(item.id, [])
    who = assg[0] if assg else None
    topic = item.topic or f"контент #{item.id}"
    steps = []
    for i, kind in enumerate(kinds, 1):
        t = Task(title=f"{CJOB_EMOJI[kind]} {CJOB_RU[kind]}: {topic}"[:120], description=_brief(item),
                 job_kind=kind, type="content_pipeline", priority="normal", status="queued",
                 step_no=i, created_by=uid, assignee_id=who, deadline=_step_deadline(item, i, len(kinds)),
                 content_id=item.id, client_id=item.client_id, project_id=item.project_id)
        session.add(t)
        steps.append(t)
    await session.flush()
    if who:
        for t in steps:
            session.add(TaskAssignee(task_id=t.id, user_id=who))
    return steps


async def _start_step(session, bg, item, step):
    step.status = "pending"
    step.updated_at = _now()
    topic = item.topic or f"контент #{item.id}"
    dl = f" · до {step.deadline.astimezone(TASHKENT).strftime('%d.%m %H:%M')}" if step.deadline else ""
    for a in await _task_assignee_ids(session, step.id, step.assignee_id):
        tg = await _telegram_id_for(session, a)
        if tg:
            bg.add_task(_tg_send, tg, f"{CJOB_EMOJI.get(step.job_kind, '▶')} Твой шаг: "
                                      f"{CJOB_RU.get(step.job_kind, '')} — «{topic}»{dl}")
    await _notify_project(bg, session, item.project_id,
                          f"▶ «{topic}»: {CJOB_RU.get(step.job_kind, 'шаг')} — в работе")


async def _advance_chain(session, bg, done_step):
    """Called when a content job task becomes done. Old chain steps: start the next
    queued one. Then, once every job of the content is finished (jobs run in parallel —
    e.g. design and edit by two people), send the content to the AM for approval."""
    if not done_step.content_id or not (done_step.job_kind or done_step.step_no is not None):
        return
    item = (await session.execute(select(ContentItem).where(
        ContentItem.id == done_step.content_id))).scalar_one_or_none()
    if not item:
        return
    steps = await _chain_steps(session, item.id)
    nxt = next((s for s in steps if done_step.step_no is not None
                and s.step_no > done_step.step_no and s.status == "queued"), None)
    jobs = (await session.execute(select(Task).where(
        Task.content_id == item.id, Task.job_kind.isnot(None)))).scalars().all()
    topic = item.topic or f"контент #{item.id}"
    if nxt:
        await _start_step(session, bg, item, nxt)
    elif (item.pipeline_status or "script") in ("script", "revisions") and \
            all(s.status in ("done", "published", "cancelled") for s in jobs):
        item.pipeline_status = "approval"
        item.updated_at = _now()
        am = item.am_id or item.created_by
        if am:
            tg = await _telegram_id_for(session, am)
            if tg:
                bg.add_task(_tg_send, tg, f"✅ Все задачи по «{topic}» сданы — утверди публикацию")
        await _notify_project(bg, session, item.project_id, f"✅ «{topic}» готов — на одобрении у AM")
    await session.commit()


async def _content_for_edit(session, content_id, user):
    item = (await session.execute(
        select(ContentItem).where(ContentItem.id == content_id))).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Content not found")
    assg = (await _content_assignees(session, [item.id])).get(item.id, [])
    my_pids = await _my_project_ids(session, user["id"])
    if not _can_edit_content(user, item, my_pids, assg):
        raise HTTPException(403, "можно только по своему контенту")
    return item


class ChainStepAdd(BaseModel):
    kind: str


@router.post("/content/{content_id}/chain", status_code=201)
async def plan_chain(content_id: int, user: dict = Depends(member), session=Depends(get_session)):
    """Create the planned steps (not started) so the AM can adjust people / deadlines first."""
    item = await _content_for_edit(session, content_id, user)
    if await _chain_steps(session, item.id):
        raise HTTPException(400, "шаги уже есть")
    await _create_chain(session, item, user["id"] or None)
    await session.commit()
    return await _one_content(session, item, user)


@router.post("/content/{content_id}/chain/step", status_code=201)
async def add_chain_step(content_id: int, body: ChainStepAdd,
                         user: dict = Depends(member), session=Depends(get_session)):
    """Append a step (e.g. «ИИ-вставка») to the end of the chain."""
    if body.kind not in CJOB_KINDS:
        raise HTTPException(422, f"kind must be one of {list(CJOB_KINDS)}")
    item = await _content_for_edit(session, content_id, user)
    steps = await _chain_steps(session, item.id) or await _create_chain(session, item, user["id"] or None, kinds=[])
    last = steps[-1] if steps else None
    if item.pipeline_status not in ("script", "revisions"):
        raise HTTPException(400, "публикация уже на одобрении — шаги не добавить")
    t = Task(title=f"{CJOB_EMOJI[body.kind]} {CJOB_RU[body.kind]}: {item.topic or f'контент #{item.id}'}"[:120],
             description=_brief(item), job_kind=body.kind, type="content_pipeline", priority="normal",
             status="queued", step_no=(last.step_no + 1) if last else 1, created_by=user["id"] or None,
             assignee_id=last.assignee_id if last else None,
             content_id=item.id, client_id=item.client_id, project_id=item.project_id)
    session.add(t)
    await session.flush()
    if t.assignee_id:
        session.add(TaskAssignee(task_id=t.id, user_id=t.assignee_id))
    await session.commit()
    return await _one_content(session, item, user)


@router.delete("/content/{content_id}/chain/step/{task_id}")
async def remove_chain_step(content_id: int, task_id: int, bg: BackgroundTasks,
                            user: dict = Depends(member), session=Depends(get_session)):
    """Drop a step that isn't needed (not every post needs a shoot or a design).
    Only steps that aren't finished; if the current step is removed the next one starts."""
    item = await _content_for_edit(session, content_id, user)
    step = (await session.execute(select(Task).where(
        Task.id == task_id, Task.content_id == item.id, Task.step_no.isnot(None)))).scalar_one_or_none()
    if not step:
        raise HTTPException(404, "шаг не найден")
    if step.status in ("done", "published"):
        raise HTTPException(400, "шаг уже сдан")
    was_active = step.status != "queued"
    await session.execute(sa_delete(Task).where(Task.id == step.id))
    await session.flush()
    left = await _chain_steps(session, item.id)
    for i, t in enumerate(left, 1):          # keep 1..n without gaps
        t.step_no = i
    if was_active and item.launched_at and not any(t.status not in ("queued", "done", "published", "cancelled") for t in left):
        nxt = next((t for t in left if t.status == "queued"), None)
        if nxt:
            await _start_step(session, bg, item, nxt)
        elif left and all(t.status in ("done", "published", "cancelled") for t in left):
            item.pipeline_status = "approval"
    await session.commit()
    return await _one_content(session, item, user)


async def _launch(session, bg, item, uid):
    steps = await _chain_steps(session, item.id) or await _create_chain(session, item, uid)
    item.launched_at = _now()
    first = next((s for s in steps if s.status == "queued"), None)
    if first:
        await _start_step(session, bg, item, first)
    await session.commit()


@router.post("/content/{content_id}/launch")
async def launch_content(content_id: int, bg: BackgroundTasks,
                         user: dict = Depends(member), session=Depends(get_session)):
    """«🚀 Запустить в работу»: first step goes to its executor."""
    item = await _content_for_edit(session, content_id, user)
    if item.launched_at:
        raise HTTPException(400, "уже запущено")
    await _launch(session, bg, item, user["id"] or None)
    return await _one_content(session, item, user)


class LaunchMonth(BaseModel):
    project_id: int


@router.post("/content/launch-month")
async def launch_month(body: LaunchMonth, bg: BackgroundTasks,
                       user: dict = Depends(member), session=Depends(get_session)):
    """«🚀 Запустить месяц»: launch every planned, not yet launched item of the project."""
    my_pids = await _my_project_ids(session, user["id"])
    if user["role"] not in MANAGER_ROLES and body.project_id not in my_pids:
        raise HTTPException(403, "можно только по своим проектам")
    items = (await session.execute(select(ContentItem).where(
        ContentItem.project_id == body.project_id, ContentItem.launched_at.is_(None),
        ContentItem.archived_at.is_(None), ContentItem.pipeline_status == "script"))).scalars().all()
    legacy = {cid for (cid,) in (await session.execute(select(Task.content_id).where(
        Task.content_id.in_([i.id for i in items]), Task.job_kind.isnot(None),
        Task.step_no.is_(None)))).all()} if items else set()
    n = 0
    for item in items:
        if item.id in legacy:          # old parallel jobs — leave as they are
            continue
        await _launch(session, bg, item, user["id"] or None)
        n += 1
    return {"launched": n}


@router.post("/content/{content_id}/archive")
async def archive_content(content_id: int, restore: bool = False,
                          user: dict = Depends(member), session=Depends(get_session)):
    item = await _content_for_edit(session, content_id, user)
    item.archived_at = None if restore else _now()
    await session.commit()
    return {"id": item.id, "archived": item.archived_at is not None}


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


@router.delete("/content/{content_id}")
async def delete_content(content_id: int,
                         user: dict = Depends(member), session=Depends(get_session)):
    item = (await session.execute(
        select(ContentItem).where(ContentItem.id == content_id))).scalar_one_or_none()
    if not item:
        return {"ok": True}
    assg = (await _content_assignees(session, [item.id])).get(item.id, [])
    my_pids = await _my_project_ids(session, user["id"])
    if not _can_edit_content(user, item, my_pids, assg):
        raise HTTPException(403, "можно удалять только свой контент")
    # drop dispatched production jobs (shoot/design/edit) and other soft references
    # first — content_items has no CASCADE from Task.content_id / Blocker.content_id
    await session.execute(sa_delete(Task).where(Task.content_id == content_id))
    await session.execute(sa_delete(Blocker).where(Blocker.content_id == content_id))
    await session.execute(sa_update(Idea).where(Idea.implemented_content_id == content_id)
                          .values(implemented_content_id=None))
    topic = item.topic
    try:
        await session.execute(sa_delete(ContentItem).where(ContentItem.id == content_id))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(400, "не удалось удалить — есть связанные данные")
    await _log(session, "content", "deleted", content_id, user["id"], topic)
    return {"ok": True}


@router.get("/ideas")
async def list_ideas(scope: str | None = None,
                     user: dict = Depends(member), session=Depends(get_session)):
    q = select(Idea)
    if scope == "mine":
        mine = await _my_project_ids(session, user["id"])
        q = q.where(or_(Idea.proposed_by == user["id"],
                        Idea.project_id.in_(mine) if mine else False))
    # open ideas first (new / in work), then rejected / already in the content plan
    rows = (await session.execute(
        q.order_by(Idea.status.in_(("rejected", "implemented")),
                   Idea.votes_count.desc(), Idea.created_at.desc()).limit(200)
    )).scalars().all()
    voted = set()
    if user["id"]:
        voted = set((await session.execute(
            select(IdeaVote.idea_id).where(IdeaVote.user_id == user["id"])
        )).scalars().all())
    proposers = await _names_for(session, [r.proposed_by for r in rows])
    refs = await _ref_summaries(session, idea_ids=[r.id for r in rows])
    pids = {r.project_id for r in rows if r.project_id}
    proj = {pid: (pn, cn) for pid, pn, cn in (await session.execute(
        select(Project.id, Project.name, Client.name)
        .join(Client, Client.id == Project.client_id, isouter=True)
        .where(Project.id.in_(pids)))).all()} if pids else {}
    media = await _idea_media(session, [r.id for r in rows])
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["voted"] = r.id in voted
        d["proposed_by_name"] = proposers.get(r.proposed_by)
        pn, cn = proj.get(r.project_id, (None, None))
        d["project_name"], d["client_name"] = pn, cn
        d["media"] = media.get(r.id)
        s = refs.get(r.id, {})
        d["refs_count"] = s.get("count", 0)
        d["ref_thumbs"] = s.get("thumbs", [])
        out.append(d)
    return out


async def _idea_media(session, idea_ids) -> dict:
    """idea_id -> {"url", "image", "site", "title"}: the first link reference, shown as a
    playable cover right on the idea card. Missing / expired previews are fetched a few
    per request (see _fill_previews), so the list stays fast and fills in over time."""
    if not idea_ids:
        return {}
    links = (await session.execute(
        select(ReferenceItem).where(ReferenceItem.idea_id.in_(idea_ids), ReferenceItem.kind == "link")
        .order_by(ReferenceItem.id))).scalars().all()
    await _fill_previews(session, links)
    out = {}
    for r in links:
        cur = out.get(r.idea_id)
        if cur is None or (not cur["image"] and r.preview_image):   # prefer one with a cover
            out[r.idea_id] = {"url": r.url, "image": r.preview_image,
                              "site": r.preview_site, "title": r.preview_title}
    return out


IDEA_STATUSES_EDITABLE = ("new", "under_review", "rejected")


class IdeaPatch(BaseModel):
    status: str


async def _idea_for_edit(session, idea_id, user, managers_only=False):
    idea = (await session.execute(select(Idea).where(Idea.id == idea_id))).scalar_one_or_none()
    if not idea:
        raise HTTPException(404, "Idea not found")
    is_mgr = user["role"] in MANAGER_ROLES
    if not is_mgr and (managers_only or idea.proposed_by != user["id"]):
        raise HTTPException(403, "Только автор идеи или admin/am/director")
    return idea


@router.patch("/ideas/{idea_id}")
async def update_idea(idea_id: int, patch: IdeaPatch,
                      user: dict = Depends(member), session=Depends(get_session)):
    """Status: new (новая) / under_review (в работе) / rejected (отклонена)."""
    if patch.status not in IDEA_STATUSES_EDITABLE:
        raise HTTPException(422, f"status must be one of {IDEA_STATUSES_EDITABLE}")
    idea = await _idea_for_edit(session, idea_id, user)
    idea.status = patch.status
    idea.updated_at = _now()
    await session.commit()
    await _log(session, "idea", "updated", idea.id, user["id"], idea.title)
    return {"id": idea.id, "status": idea.status}


@router.post("/ideas/{idea_id}/to-content", status_code=201)
async def idea_to_content(idea_id: int,
                          user: dict = Depends(member), session=Depends(get_session)):
    """Create a draft content item from the idea (topic, script, refs) and mark it implemented."""
    idea = await _idea_for_edit(session, idea_id, user, managers_only=True)
    if idea.implemented_content_id:
        raise HTTPException(409, "Идея уже в контент-плане")
    client_id = idea.client_id
    if idea.project_id and not client_id:
        client_id = (await session.execute(
            select(Project.client_id).where(Project.id == idea.project_id))).scalar_one_or_none()
    uid = user["id"] or None
    obj = ContentItem(format=idea.format or "post", topic=idea.title, script=idea.description,
                      project_id=idea.project_id, client_id=client_id,
                      pipeline_status=CONTENT_START, created_by=uid, author_id=uid)
    session.add(obj)
    await session.flush()
    for r in (await session.execute(
            select(ReferenceItem).where(ReferenceItem.idea_id == idea.id))).scalars().all():
        session.add(ReferenceItem(
            content_id=obj.id, kind=r.kind, url=r.url, title=r.title, tg_file_id=r.tg_file_id,
            file_name=r.file_name, mime=r.mime, added_by=r.added_by,
            preview_title=r.preview_title, preview_image=r.preview_image,
            preview_site=r.preview_site, preview_fetched_at=r.preview_fetched_at))
    idea.status = "implemented"
    idea.implemented_content_id = obj.id
    idea.updated_at = _now()
    await session.commit()
    await _log(session, "content", "created", obj.id, uid, obj.topic)
    return {"content_id": obj.id, "idea_id": idea.id}


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


@router.delete("/blockers/{blocker_id}")
async def delete_blocker(blocker_id: int,
                         user: dict = Depends(member), session=Depends(get_session)):
    b = (await session.execute(
        select(Blocker).where(Blocker.id == blocker_id))).scalar_one_or_none()
    if not b:
        return {"ok": True}
    if (user["role"] not in MANAGER_ROLES
            and user["id"] not in (b.reported_by, b.assigned_to)):
        raise HTTPException(403, "удалить блокер может автор, назначенный или admin/am/director")
    await session.execute(sa_delete(Blocker).where(Blocker.id == blocker_id))
    await session.commit()
    await _log(session, "blocker", "deleted", blocker_id, user["id"], b.title)
    return {"ok": True}


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
    # title is optional: the Mini App posts the link right after creating the idea,
    # and an empty title is filled from that link's preview (add_link_reference)
    title = _clean(body.title) or ""
    ref_url = _clean(body.reference_url)
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
    if ref_url:
        if not ref_url.startswith(("http://", "https://")):
            ref_url = "https://" + ref_url
        pv = await fetch_preview(ref_url)
        session.add(ReferenceItem(idea_id=obj.id, kind="link", url=ref_url, title=ref_url, added_by=uid,
                                  preview_title=pv["title"], preview_image=pv["image"],
                                  preview_site=pv["site"], preview_fetched_at=_now()))
        if not obj.title:
            obj.title = _idea_title_from(pv)
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
            "am_id": c.am_id, "am_name": am_name, "is_active": c.is_active,
            "goals": c.goals, "audience": c.audience,
            "tone_of_voice": c.tone_of_voice, "competitors": c.competitors}


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
                   Project.monthly_posts, Project.description)
            .where(Project.client_id.in_([c.id for c in rows]),
                   Project.is_active.is_(True))
            .order_by(Project.name))).all()
    names = await _names_for(
        session, [c.am_id for c in rows] + [r[3] for r in prows])
    prefs = await _ref_summaries(session, project_ids=[r[0] for r in prows])
    # published this month — same rule as the dashboard's publication plan
    month_start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    pub = dict((await session.execute(
        select(ContentItem.project_id, func.count())
        .where(ContentItem.pipeline_status == "published",
               ContentItem.updated_at >= month_start,
               ContentItem.project_id.in_([r[0] for r in prows]))
        .group_by(ContentItem.project_id))).all()) if prows else {}
    for pid, cid, pn, pam, mp, pdesc in prows:
        projs.setdefault(cid, []).append(
            {"id": pid, "name": pn, "am_id": pam, "am_name": names.get(pam),
             "monthly_posts": mp, "description": pdesc, "published_month": pub.get(pid, 0),
             "refs_count": prefs.get(pid, {}).get("count", 0)})
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
        select(Project.id, Project.name, Project.am_id, Project.monthly_posts, Project.description)
        .where(Project.client_id == c.id, Project.is_active.is_(True))
        .order_by(Project.name))).all()
    names = await _names_for(session, [c.am_id] + [r[2] for r in rows])
    refs = await _ref_summaries(session, project_ids=[r[0] for r in rows])
    d = _client_out(c, names.get(c.am_id))
    d["projects"] = [{"id": pid, "name": pn, "am_id": pam, "am_name": names.get(pam),
                      "monthly_posts": mp, "description": pdesc,
                      "refs_count": refs.get(pid, {}).get("count", 0)}
                     for pid, pn, pam, mp, pdesc in rows]
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
    for f in ("contact", "notes", "goals", "audience", "tone_of_voice", "competitors"):
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


@router.delete("/clients/{client_id}")
async def delete_client(client_id: int,
                        user: dict = Depends(member), session=Depends(get_session)):
    """Delete a client — only if none of its projects have real data yet
    (deleting the client cascades to its projects, which would silently
    wipe content/tasks otherwise)."""
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "only admin / am can delete clients")
    c = (await session.execute(
        select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if not c:
        return {"ok": True}
    pids = (await session.execute(
        select(Project.id).where(Project.client_id == client_id))).scalars().all()
    used = []
    if pids:
        for model, label in [(ContentItem, "контент"), (Task, "задачи"),
                             (ShootSession, "съёмки"), (Idea, "идеи"),
                             (Blocker, "блокеры"), (ReferenceItem, "референсы")]:
            n = (await session.execute(
                select(func.count()).select_from(model).where(model.project_id.in_(pids))
            )).scalar_one()
            if n:
                used.append(f"{label}: {n}")
    if used:
        raise HTTPException(
            400, "нельзя удалить — есть данные (" + ", ".join(used) +
                 "). Удали их или деактивируй клиента вместо удаления.")
    name = c.name
    await session.execute(sa_delete(Client).where(Client.id == client_id))
    await session.commit()
    await _log(session, "project", "deleted", client_id, user["id"], name)
    return {"ok": True}


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


@router.delete("/projects/{project_id}")
async def delete_project(project_id: int,
                         user: dict = Depends(member), session=Depends(get_session)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "only admin / am can delete projects")
    p = (await session.execute(
        select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "project not found")

    # refuse to silently cascade-delete real work — only empty/unused projects go
    checks = [(ContentItem, "контент"), (Task, "задачи"),
              (ShootSession, "съёмки"), (Idea, "идеи"), (Blocker, "блокеры"),
              (ReferenceItem, "референсы")]
    used = []
    for model, label in checks:
        n = (await session.execute(
            select(func.count()).select_from(model).where(model.project_id == project_id)
        )).scalar_one()
        if n:
            used.append(f"{label}: {n}")
    if used:
        raise HTTPException(
            400, "нельзя удалить — есть данные (" + ", ".join(used) +
                 "). Можно деактивировать проект вместо удаления.")

    name = p.name
    await session.execute(sa_delete(Project).where(Project.id == project_id))
    await session.commit()
    await _log(session, "project", "deleted", project_id, user["id"], name)
    return {"ok": True}


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
        "preview": ({"title": r.preview_title, "image": r.preview_image, "site": r.preview_site}
                    if r.kind == "link" and r.preview_fetched_at else None),
    }


async def _fill_previews(session, refs):
    """Fetch link previews never tried yet (old links, links added via forms), and refresh
    image previews older than 2 days — Instagram/TikTok CDN image URLs are signed and expire.
    Max 6 per call so opening the list stays fast."""
    stale = _now() - timedelta(days=2)
    todo = [r for r in refs if r.kind == "link" and r.url and (
        r.preview_fetched_at is None or (r.preview_image and r.preview_fetched_at < stale))][:6]
    if not todo:
        return
    results = await asyncio.gather(*(fetch_preview(r.url) for r in todo))
    for r, pv in zip(todo, results):
        r.preview_title, r.preview_image, r.preview_site = pv["title"], pv["image"], pv["site"]
        r.preview_fetched_at = _now()
    await session.commit()


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
    await _fill_previews(session, rows)
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
    pv = await fetch_preview(url)
    pv_title = pv["title"] if pv["title"] != pv["site"] else None   # «Instagram» alone isn't a title
    obj = ReferenceItem(kind="link", url=url, title=_clean(body.title) or pv_title or url,
                        task_id=body.task_id, content_id=body.content_id,
                        idea_id=body.idea_id, project_id=body.project_id,
                        added_by=user["id"] or None,
                        preview_title=pv["title"], preview_image=pv["image"],
                        preview_site=pv["site"], preview_fetched_at=_now())
    session.add(obj)
    if body.idea_id:   # idea created with just a link -> name it after the link
        idea = (await session.execute(select(Idea).where(Idea.id == body.idea_id))).scalar_one_or_none()
        if idea and not (idea.title or "").strip():
            idea.title = _idea_title_from(pv)
    await session.commit()
    await session.refresh(obj)
    return _ref_out(obj, user.get("full_name"))


def _idea_title_from(pv: dict) -> str:
    """Short idea title from a link preview (Instagram: the caption part of «Name в Instagram : "…"»)."""
    title = (pv.get("title") or "").split("\n")[0]
    if " : " in title:
        title = title.split(" : ", 1)[1].strip(' "«»')
    if len(title) > 80:
        title = title[:80].rstrip() + "…"
    return title or (f"Идея из {pv['site']}" if pv.get("site") else "Новая идея")


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
    open_by_user = dict((await session.execute(
        select(TaskAssignee.user_id, func.count())
        .join(Task, Task.id == TaskAssignee.task_id)
        .where(Task.status.in_(OPEN_TASK_STATUSES))
        .group_by(TaskAssignee.user_id))).all())
    reach = await _bot_reachable([u.telegram_id for u in rows])
    out = []
    for u in rows:
        d = _user_out(u)
        d["open_tasks"] = open_by_user.get(u.id, 0)
        d["bot_ok"] = reach.get(u.telegram_id)   # False = never pressed Start -> DMs can't reach them
        out.append(d)
    return out


@router.get("/team/load")
async def team_load(user: dict = Depends(member), session=Depends(get_session)):
    """Who works on what. people: per active person — open tasks (with project, days in
    work, revisions), upcoming shoots (14 days), when they're free, revisions over 30 days.
    projects: per project — who is on it right now and how many revisions its open tasks had."""
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(403, "только для admin / am / director")
    now = _now()
    horizon = now + timedelta(days=14)
    people = (await session.execute(select(User).where(User.is_active.is_(True))
                                    .order_by(User.role, User.full_name))).scalars().all()
    rows = (await session.execute(
        select(TaskAssignee.user_id, Task)
        .join(Task, Task.id == TaskAssignee.task_id)
        .where(Task.status.in_(OPEN_TASK_STATUSES + ("review", "queued"))))).all()
    shoots = (await session.execute(
        select(ShootParticipant.user_id, ShootSession)
        .join(ShootSession, ShootSession.id == ShootParticipant.shoot_id)
        .where(ShootSession.shoot_at >= now - timedelta(hours=12), ShootSession.shoot_at <= horizon,
               ShootSession.status != "cancelled"))).all()
    revs = await _revision_counts(session, list({t.id for _, t in rows}))
    # revisions in the last 30 days, credited to whoever is on the task
    rev30 = dict((await session.execute(
        select(TaskAssignee.user_id, func.count())
        .join(StatusEvent, (StatusEvent.entity == "task") & (StatusEvent.entity_id == TaskAssignee.task_id))
        .where(StatusEvent.status == "revision", StatusEvent.created_at >= now - timedelta(days=30))
        .group_by(TaskAssignee.user_id))).all())
    pnames = dict((await session.execute(select(Project.id, Project.name))).all())
    by = {u.id: {"tasks": [], "shoots": []} for u in people}
    for uid, t in rows:
        if uid in by:
            by[uid]["tasks"].append(t)
    for uid, sh in shoots:
        if uid in by:
            by[uid]["shoots"].append(sh)
    rank = {"in_progress": 0, "revision": 1, "overdue": 2, "pending": 3, "review": 4, "queued": 5}

    def task_out(t):
        return {"id": t.id, "title": t.title, "status": t.status, "kind": t.job_kind,
                "project_id": t.project_id, "project": pnames.get(t.project_id),
                "revisions": revs.get(t.id, 0),
                "days": (now - t.created_at).days if t.created_at else None,
                "deadline": t.deadline.isoformat() if t.deadline else None}

    out, projects = [], {}
    for u in people:
        ts = sorted(by[u.id]["tasks"], key=lambda t: (rank.get(t.status, 9), t.deadline is None, t.deadline or now))
        active = [t for t in ts if t.status != "queued"]
        ends = [t.deadline for t in ts if t.deadline] + [sh.shoot_at for sh in by[u.id]["shoots"]]
        for t in active:
            if not t.project_id:
                continue
            pr = projects.setdefault(t.project_id, {"id": t.project_id, "name": pnames.get(t.project_id),
                                                    "people": {}, "tasks": set(), "revisions": 0})
            who = pr["people"].setdefault(u.id, {"id": u.id, "name": u.full_name, "role": u.role, "kinds": []})
            k = t.job_kind or "task"
            if k not in who["kinds"]:
                who["kinds"].append(k)
            if t.id not in pr["tasks"]:
                pr["tasks"].add(t.id)
                pr["revisions"] += revs.get(t.id, 0)
        out.append({
            "id": u.id, "name": u.full_name, "role": u.role,
            "open": len([t for t in active if t.status != "review"]),
            "review": len([t for t in active if t.status == "review"]),
            "queued": len(ts) - len(active),
            "overdue": len([t for t in active if t.deadline and t.deadline < now and t.status != "review"]),
            "revisions": sum(revs.get(t.id, 0) for t in active),
            "revisions_30d": rev30.get(u.id, 0),
            "busy_until": max(ends).isoformat() if ends else None,
            "tasks": [task_out(t) for t in ts[:12]],
            "shoots": [{"id": sh.id, "title": sh.title, "at": sh.shoot_at.isoformat()}
                       for sh in sorted(by[u.id]["shoots"], key=lambda x: x.shoot_at)],
        })
    proj_out = sorted(({"id": p["id"], "name": p["name"], "people": list(p["people"].values()),
                        "tasks": len(p["tasks"]), "revisions": p["revisions"]} for p in projects.values()),
                      key=lambda p: (-len(p["people"]), -p["tasks"]))
    return {"people": out, "projects": proj_out}


# telegram_id -> (reachable, checked_at). True is cached longer than False so a fresh
# «Start» shows up within a couple of minutes.
_REACH_CACHE: dict = {}


async def _bot_reachable(tg_ids) -> dict:
    """{telegram_id: True/False/None} — can the bot DM this person? getChat on a user id
    only succeeds if they have a private chat with the bot (pressed Start). Read-only,
    sends nothing. None = unknown (network error / no token)."""
    if not BOT_TOKEN:
        return {}
    now = _now().timestamp()
    out, todo = {}, []
    for tg in {t for t in tg_ids if t}:
        hit = _REACH_CACHE.get(tg)
        if hit and now - hit[1] < (600 if hit[0] else 120):
            out[tg] = hit[0]
        else:
            todo.append(tg)
    if todo:
        sem = asyncio.Semaphore(8)

        async def check(client, tg):
            async with sem:
                try:
                    r = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getChat",
                                         params={"chat_id": tg})
                    if r.status_code == 200:
                        return tg, True
                    if r.status_code in (400, 403):
                        return tg, False
                except Exception:  # noqa: BLE001
                    pass
                return tg, None
        async with httpx.AsyncClient(timeout=5) as client:
            for tg, ok in await asyncio.gather(*(check(client, tg) for tg in todo)):
                out[tg] = ok
                if ok is not None:
                    _REACH_CACHE[tg] = (ok, now)
    return out


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
    # One grouped query over task_assignees (co-assignees count for everyone on the task).
    this_month = Task.created_at >= month_start
    load = {uid: (open_n, total_n, done_n) for uid, open_n, total_n, done_n in (await session.execute(
        select(TaskAssignee.user_id,
               func.count().filter(Task.status.in_(OPEN_TASK_STATUSES)),
               func.count().filter(this_month),
               func.count().filter(this_month, Task.status.in_(("done", "published"))))
        .join(Task, Task.id == TaskAssignee.task_id)
        .group_by(TaskAssignee.user_id)
    )).all()}
    workload = []
    for u in users:
        open_n, total_n, done_n = load.get(u.id, (0, 0, 0))
        workload.append({
            "user_id": u.id,
            "name": u.full_name,
            "open_tasks": open_n,
            "total_this_month": total_n,
            "done_this_month": done_n,
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
