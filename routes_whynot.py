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
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select, or_, func, delete as sa_delete
from sqlalchemy.exc import IntegrityError

from db.models import (
    AsyncSessionLocal, User, Task, ContentItem, Idea, IdeaVote, Blocker,
    Client, Project, ActivityEvent, TaskAssignee, ReferenceItem,
    PIPELINE_SEQUENCE, task_status_enum, user_role_enum,
    content_format_enum, task_priority_enum,
)

PIPELINE_ORDER = ["idea", "approval", "script", "shoot", "edit", "review",
                  "client", "revisions", "done", "published", "analytics"]

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
        tg_id = int(tg_user["id"])
        row = (await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )).scalar_one_or_none()
        if row and row.is_active:
            return {"id": row.id, "telegram_id": tg_id,
                    "full_name": row.full_name, "role": row.role, "registered": True}
        name = " ".join(filter(None, [tg_user.get("first_name"), tg_user.get("last_name")]))
        return {"id": 0, "telegram_id": tg_id,
                "full_name": name or "Guest", "role": "guest", "registered": False}

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


async def member(user: dict = Depends(current_user)) -> dict:
    """Closed access: only users present in the `users` table get through."""
    if not user.get("registered"):
        raise HTTPException(403, {
            "code": "not_registered",
            "message": "Доступ закрыт. Передай свой Telegram ID администратору.",
            "telegram_id": user.get("telegram_id") or 0,
        })
    return user


# ── models ──────────────────────────────────────────────────────

TASK_STATUSES = set(task_status_enum.enums)
OPEN_TASK_STATUSES = ("pending", "in_progress", "overdue")
USER_ROLES = set(user_role_enum.enums)
TASK_PRIORITIES = set(task_priority_enum.enums)          # low, normal, high, urgent
CONTENT_FORMATS = set(content_format_enum.enums)
# tolerate the labels the Mini App form uses
PRIORITY_ALIASES = {"critical": "urgent", "medium": "normal"}
FORMAT_ALIASES = {"reel": "reels", "video": "youtube_long", "article": "other"}


class TaskPatch(BaseModel):
    status: str


class RolePatch(BaseModel):
    role: str
    full_name: str | None = None
    username: str | None = None


class TaskCreate(BaseModel):
    title: str
    description: str | None = None
    priority: str | None = "normal"
    deadline: str | None = None
    assignee_id: int | None = None
    assignee_ids: list[int] | None = None
    client_id: int | None = None
    project_id: int | None = None


class LinkRef(BaseModel):
    task_id: int | None = None
    content_id: int | None = None
    url: str
    title: str | None = None


class TeamRolePatch(BaseModel):
    role: str
    full_name: str | None = None


class ClientCreate(BaseModel):
    name: str
    contact: str | None = None
    notes: str | None = None


class ProjectCreate(BaseModel):
    client_id: int
    name: str
    description: str | None = None


class ContentCreate(BaseModel):
    format: str
    topic: str | None = None
    publish_date: str | None = None
    project_id: int | None = None
    client_id: int | None = None


class IdeaCreate(BaseModel):
    title: str
    description: str | None = None
    format: str | None = None


class BlockerCreate(BaseModel):
    title: str
    description: str | None = None
    assigned_to: int | None = None


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
        return datetime.fromisoformat(v + "T00:00:00+00:00") if len(v) == 10 \
            else datetime.fromisoformat(v)
    except ValueError:
        raise HTTPException(422, f"bad datetime: {v!r}")


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


async def _ref_counts(session, *, task_ids=None, content_ids=None):
    col = ReferenceItem.task_id if task_ids is not None else ReferenceItem.content_id
    ids = task_ids if task_ids is not None else content_ids
    if not ids:
        return {}
    rows = (await session.execute(
        select(col, func.count()).where(col.in_(ids)).group_by(col)
    )).all()
    return {k: v for k, v in rows}


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
    refc = await _ref_counts(session, task_ids=ids)
    out = []
    for t in tasks:
        d = row_to_dict(t)
        aids = by_task.get(t.id) or ([t.assignee_id] if t.assignee_id else [])
        d["assignees"] = [{"id": i, "name": names.get(i)} for i in aids]
        d["assignee_name"] = names.get(t.assignee_id) or (d["assignees"][0]["name"] if d["assignees"] else None)
        d["refs_count"] = refc.get(t.id, 0)
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
async def update_task(task_id: int, patch: TaskPatch,
                      user: dict = Depends(member), session=Depends(get_session)):
    if patch.status not in TASK_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(TASK_STATUSES)}")
    task = (await session.execute(
        select(Task).where(Task.id == task_id)
    )).scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")
    task.status = patch.status
    task.updated_at = _now()
    if patch.status == "done" and task.actual_completion is None:
        task.actual_completion = _now()
    await session.commit()
    await session.refresh(task)
    await _log(session, "task", "completed" if patch.status == "done" else "updated",
               task.id, user["id"], task.title)
    return row_to_dict(task)


@router.get("/content")
async def list_content(client_id: int | None = None,
                       user: dict = Depends(member), session=Depends(get_session)):
    q = select(ContentItem)
    if client_id:
        q = q.where(ContentItem.client_id == client_id)
    q = q.order_by(ContentItem.pipeline_status, ContentItem.publish_date.is_(None),
                   ContentItem.publish_date, ContentItem.id.desc()).limit(300)
    rows = (await session.execute(q)).scalars().all()
    refc = await _ref_counts(session, content_ids=[c.id for c in rows])
    out = []
    for c in rows:
        d = row_to_dict(c)
        d["refs_count"] = refc.get(c.id, 0)
        out.append(d)
    return out


@router.post("/content/{content_id}/advance")
async def advance_content(content_id: int,
                          user: dict = Depends(member), session=Depends(get_session)):
    item = (await session.execute(
        select(ContentItem).where(ContentItem.id == content_id)
    )).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Content not found")
    cur = item.pipeline_status
    nxt = PIPELINE_SEQUENCE.get(cur, {}).get("next")
    if not nxt:
        raise HTTPException(400, f"'{cur}' is the final pipeline step")
    item.pipeline_status = nxt
    item.updated_at = _now()
    await session.commit()
    await session.refresh(item)
    await _log(session, "content", "moved", item.id, user["id"], item.topic)
    return row_to_dict(item)


@router.get("/ideas")
async def list_ideas(user: dict = Depends(member), session=Depends(get_session)):
    rows = (await session.execute(
        select(Idea).order_by(Idea.votes_count.desc(), Idea.created_at.desc()).limit(200)
    )).scalars().all()
    voted = set()
    if user["id"]:
        voted = set((await session.execute(
            select(IdeaVote.idea_id).where(IdeaVote.user_id == user["id"])
        )).scalars().all())
    proposers = await _names_for(session, [r.proposed_by for r in rows])
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["voted"] = r.id in voted
        d["proposed_by_name"] = proposers.get(r.proposed_by)
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


async def _tg_send(telegram_id: int, text: str):
    """Fire a Telegram DM. Best-effort — never raises into the request."""
    if not (BOT_TOKEN and telegram_id):
        return
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": telegram_id, "text": text},
            )
        if r.status_code != 200:
            print(f"notify {telegram_id}: telegram {r.status_code} {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"notify {telegram_id} failed: {e}")


async def _telegram_id_for(session, user_id):
    if not user_id:
        return None
    return (await session.execute(
        select(User.telegram_id).where(User.id == user_id)
    )).scalar_one_or_none()


@router.post("/tasks", status_code=201)
async def create_task(body: TaskCreate, bg: BackgroundTasks,
                      user: dict = Depends(member), session=Depends(get_session)):
    title = _clean(body.title)
    if not title:
        raise HTTPException(422, "title is required")
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
    obj = Task(
        title=title,
        description=_clean(body.description),
        priority=prio,
        deadline=deadline,
        status="pending",
        created_by=uid,
        assignee_id=aids[0] if aids else None,   # keep the single field = first assignee
        client_id=body.client_id,
        project_id=body.project_id,
    )
    result = await _commit_new(session, obj)
    for a in aids:
        session.add(TaskAssignee(task_id=obj.id, user_id=a))
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
    obj = ContentItem(
        format=fmt,
        topic=_clean(body.topic),
        publish_date=_parse_date(body.publish_date),
        project_id=body.project_id,
        client_id=body.client_id,
        pipeline_status="idea",
        created_by=uid,
        author_id=uid,
    )
    result = await _commit_new(session, obj)
    await _log(session, "content", "created", obj.id, uid, obj.topic)
    return result


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
        votes_count=0,
    )
    result = await _commit_new(session, obj)
    await _log(session, "idea", "created", obj.id, uid, title)
    return result


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


def _project_out(p):
    return {"id": p.id, "client_id": p.client_id, "name": p.name,
            "description": p.description, "is_active": p.is_active}


@router.get("/clients")
async def list_clients(user: dict = Depends(member), session=Depends(get_session)):
    rows = (await session.execute(
        select(Client).order_by(Client.is_active.desc(), Client.name)
    )).scalars().all()
    names = await _names_for(session, [c.am_id for c in rows])
    return [_client_out(c, names.get(c.am_id)) for c in rows]


@router.post("/clients", status_code=201)
async def create_client(body: ClientCreate,
                        user: dict = Depends(member), session=Depends(get_session)):
    if user["role"] not in ("admin", "am"):
        raise HTTPException(403, "only admin / am can add clients")
    name = _clean(body.name)
    if not name:
        raise HTTPException(422, "name is required")
    return await _commit_new(session, Client(
        name=name,
        contact=_clean(body.contact),
        notes=_clean(body.notes),
        am_id=user["id"] or None,
        is_active=True,
    ))


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
    return [_project_out(p) for p in rows]


@router.post("/projects", status_code=201)
async def create_project(body: ProjectCreate,
                         user: dict = Depends(member), session=Depends(get_session)):
    if user["role"] not in ("admin", "am"):
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


# ── members (lightweight list for assignee pickers — any member) ─

@router.get("/members")
async def list_members(user: dict = Depends(member), session=Depends(get_session)):
    rows = (await session.execute(
        select(User).where(User.is_active.is_(True)).order_by(User.full_name)
    )).scalars().all()
    return [{"id": u.id, "full_name": u.full_name, "role": u.role} for u in rows]


# ── references (links + files) ──────────────────────────────────

def _ref_out(r, name=None):
    return {
        "id": r.id, "kind": r.kind, "url": r.url, "title": r.title,
        "file_name": r.file_name, "mime": r.mime, "added_by_name": name,
        "task_id": r.task_id, "content_id": r.content_id,
        "download": f"/api/references/{r.id}/file" if r.kind == "file" else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def _ref_scope(body_task, body_content):
    if not (body_task or body_content):
        raise HTTPException(422, "task_id или content_id обязателен")


@router.get("/references")
async def list_references(task_id: int | None = None, content_id: int | None = None,
                          user: dict = Depends(member), session=Depends(get_session)):
    await _ref_scope(task_id, content_id)
    q = select(ReferenceItem)
    if task_id:
        q = q.where(ReferenceItem.task_id == task_id)
    if content_id:
        q = q.where(ReferenceItem.content_id == content_id)
    rows = (await session.execute(q.order_by(ReferenceItem.id.desc()))).scalars().all()
    names = await _names_for(session, [r.added_by for r in rows])
    return [_ref_out(r, names.get(r.added_by)) for r in rows]


@router.post("/references", status_code=201)
async def add_link_reference(body: LinkRef,
                             user: dict = Depends(member), session=Depends(get_session)):
    await _ref_scope(body.task_id, body.content_id)
    url = _clean(body.url)
    if not url:
        raise HTTPException(422, "url обязателен")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    obj = ReferenceItem(kind="link", url=url, title=_clean(body.title) or url,
                        task_id=body.task_id, content_id=body.content_id,
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
        user: dict = Depends(member), session=Depends(get_session)):
    await _ref_scope(task_id, content_id)
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
        task_id=task_id, content_id=content_id, added_by=user["id"] or None,
    )
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return _ref_out(obj, user.get("full_name"))


@router.get("/references/{ref_id}/file")
async def reference_file(ref_id: int, request: Request, ia: str | None = None,
                         session=Depends(get_session)):
    # auth via header OR ?ia= query (so plain <a>/<img> links work inside the app)
    init_data = (request.headers.get("X-Init-Data")
                 or request.headers.get("X-Telegram-Init-Data") or ia or "")
    ok = not BOT_TOKEN
    if init_data and BOT_TOKEN:
        try:
            ok = bool(_validate_init_data(init_data).get("id"))
        except HTTPException:
            ok = False
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
    if not (user["role"] in ("admin", "am") or r.added_by == user["id"]):
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
    if user["role"] not in ("admin", "am"):
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
    if user["role"] not in ("admin", "am"):
        raise HTTPException(403, "only admin / am can change roles")
    if body.role not in USER_ROLES:
        raise HTTPException(422, f"role must be one of {sorted(USER_ROLES)}")
    target = (await session.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if not target:
        raise HTTPException(404, "user not found")
    is_self = target.id == user["id"]
    if is_self and body.role not in ("admin", "am"):
        raise HTTPException(400, "нельзя понизить свою роль ниже управляющей")
    if user["role"] == "am" and (target.role == "admin" or body.role == "admin") and not is_self:
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
    if user["role"] not in ("admin", "am"):
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
    if user["role"] not in ("admin", "am"):
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
    if user["role"] not in ("admin", "am"):
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
