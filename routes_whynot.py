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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, or_

from db.models import (
    AsyncSessionLocal, User, Task, ContentItem, Idea, IdeaVote, Blocker,
    PIPELINE_SEQUENCE, task_status_enum,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

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
        if row:
            return {"id": row.id, "telegram_id": tg_id,
                    "full_name": row.full_name, "role": row.role, "registered": True}
        name = " ".join(filter(None, [tg_user.get("first_name"), tg_user.get("last_name")]))
        return {"id": 0, "telegram_id": tg_id,
                "full_name": name or "Guest", "role": "intern", "registered": False}

    # ── dev fallback: no/again-invalid init data ──
    row = (await session.execute(
        select(User).order_by(User.id).limit(1)
    )).scalar_one_or_none()
    if row:
        return {"id": row.id, "telegram_id": row.telegram_id or 0,
                "full_name": row.full_name, "role": row.role, "registered": True}
    return {"id": 0, "telegram_id": 0, "full_name": "Dev",
            "role": "admin", "registered": False}


# ── models ──────────────────────────────────────────────────────

TASK_STATUSES = set(task_status_enum.enums)
OPEN_TASK_STATUSES = ("pending", "in_progress", "overdue")


class TaskPatch(BaseModel):
    status: str


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

@router.get("/home")
async def home(user: dict = Depends(current_user), session=Depends(get_session)):
    uid = user["id"]

    tasks = (await session.execute(
        select(Task)
        .where(Task.assignee_id == uid, Task.status.in_(OPEN_TASK_STATUSES))
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
        "my_tasks": [row_to_dict(t) for t in tasks],
        "my_blockers": [row_to_dict(b) for b in blockers],
        "my_content": [row_to_dict(c) for c in content],
    }


@router.get("/tasks")
async def list_tasks(my: bool = False, status: str | None = None,
                     user: dict = Depends(current_user), session=Depends(get_session)):
    q = select(Task)
    if my:
        q = q.where(Task.assignee_id == user["id"])
    if status:
        q = q.where(Task.status == status)
    q = q.order_by(Task.deadline.is_(None), Task.deadline, Task.id.desc()).limit(200)
    rows = (await session.execute(q)).scalars().all()
    names = await _names_for(session, [r.assignee_id for r in rows])
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["assignee_name"] = names.get(r.assignee_id)
        out.append(d)
    return out


@router.patch("/tasks/{task_id}")
async def update_task(task_id: int, patch: TaskPatch,
                      user: dict = Depends(current_user), session=Depends(get_session)):
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
    return row_to_dict(task)


@router.get("/content")
async def list_content(user: dict = Depends(current_user), session=Depends(get_session)):
    rows = (await session.execute(
        select(ContentItem)
        .order_by(ContentItem.pipeline_status, ContentItem.publish_date.is_(None),
                  ContentItem.publish_date, ContentItem.id.desc())
        .limit(300)
    )).scalars().all()
    return [row_to_dict(c) for c in rows]


@router.post("/content/{content_id}/advance")
async def advance_content(content_id: int,
                          user: dict = Depends(current_user), session=Depends(get_session)):
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
    return row_to_dict(item)


@router.get("/ideas")
async def list_ideas(user: dict = Depends(current_user), session=Depends(get_session)):
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
                    user: dict = Depends(current_user), session=Depends(get_session)):
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
    return {"idea_id": idea_id, "votes_count": idea.votes_count, "voted": voted}


@router.get("/blockers")
async def list_blockers(user: dict = Depends(current_user), session=Depends(get_session)):
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
