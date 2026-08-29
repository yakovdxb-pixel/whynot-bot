"""
WHY NOT? OS — SQLAlchemy 2.0 async models
Schema v3.1 (audit fixes applied)
"""

from sqlalchemy import (
    Column, Integer, BigInteger, SmallInteger, Text, Boolean, Date,
    DateTime, Numeric, ForeignKey, Index, UniqueConstraint, CheckConstraint,
    text, event
)
from sqlalchemy.dialects.postgresql import JSONB, ENUM
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://whynot:whynot_secret_2026@postgres.railway.internal:5432/whynot_os")
# asyncpg requires postgresql+asyncpg://
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────

user_role_enum = ENUM(
    'admin', 'am', 'producer', 'smm', 'photographer', 'editor',
    'designer', 'copywriter', 'intern',
    name='user_role', create_type=True
)

content_format_enum = ENUM(
    'reels', 'carousel', 'story', 'post', 'tiktok', 'youtube_short',
    'youtube_long', 'podcast', 'banner', 'other',
    name='content_format', create_type=True
)

pipeline_step_enum = ENUM(
    'idea', 'approval', 'script', 'shoot', 'edit',
    'review', 'client', 'revisions', 'done', 'published', 'analytics',
    name='pipeline_step', create_type=True
)

task_type_enum = ENUM(
    'content_pipeline', 'client_request', 'internal', 'general', 'blocker_resolution',
    name='task_type', create_type=True
)

task_status_enum = ENUM(
    'pending', 'in_progress', 'done', 'cancelled', 'overdue',
    name='task_status', create_type=True
)

task_priority_enum = ENUM(
    'low', 'normal', 'high', 'urgent',
    name='task_priority', create_type=True
)

overdue_reason_enum = ENUM(
    'client_delay', 'internal_delay', 'scope_change', 'technical_issue',
    'resource_unavailable', 'force_majeure', 'other',
    name='overdue_reason', create_type=True
)

rating_context_enum = ENUM(
    'content', 'project_overall', 'communication',
    name='rating_context', create_type=True
)

event_entity_enum = ENUM(
    'task', 'content', 'pipeline_step', 'idea', 'blocker',
    'approval', 'rating', 'help', 'notification', 'user',
    'project', 'checkin',
    name='event_entity', create_type=True
)

event_action_enum = ENUM(
    'created', 'updated', 'deleted', 'completed', 'approved',
    'rejected', 'moved', 'assigned', 'commented', 'requested',
    'resolved', 'escalated', 'voted', 'vote_changed',
    name='event_action', create_type=True
)

idea_status_enum = ENUM(
    'new', 'under_review', 'approved', 'rejected', 'implemented',
    name='idea_status', create_type=True
)

blocker_status_enum = ENUM(
    'active', 'resolved', 'escalated',
    name='blocker_status', create_type=True
)

approval_status_enum = ENUM(
    'pending', 'approved', 'rejected', 'revision_requested',
    name='approval_status', create_type=True
)


# ─────────────────────────────────────────────
# PIPELINE SEQUENCE (Python config, not table)
# ─────────────────────────────────────────────

PIPELINE_SEQUENCE = {
    'idea':      {'next': 'approval',  'assignee_role': 'am'},
    'approval':  {'next': 'script',    'assignee_role': 'producer'},
    'script':    {'next': 'shoot',     'assignee_role': 'photographer'},
    'shoot':     {'next': 'edit',      'assignee_role': 'editor'},
    'edit':      {'next': 'review',    'assignee_role': 'am'},
    'review':    {'next': 'client',    'assignee_role': None},
    'client':    {'next': 'done',      'assignee_role': None},   # or 'revisions'
    'revisions': {'next': 'review',    'assignee_role': 'editor'},
    'done':      {'next': 'published', 'assignee_role': 'am'},
    'published': {'next': 'analytics', 'assignee_role': None},
    'analytics': {'next': None,        'assignee_role': None},
}


# ─────────────────────────────────────────────
# TABLES
# ─────────────────────────────────────────────

class User(Base):
    __tablename__ = 'users'

    id          = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True)
    username    = Column(Text)
    full_name   = Column(Text, nullable=False)
    role        = Column(user_role_enum, nullable=False)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime(timezone=True), server_default=text('NOW()'))
    updated_at  = Column(DateTime(timezone=True), server_default=text('NOW()'))


class Client(Base):
    __tablename__ = 'clients'

    id          = Column(Integer, primary_key=True)
    name        = Column(Text, nullable=False)
    contact     = Column(Text)
    telegram_id = Column(BigInteger)
    am_id       = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'))
    is_active   = Column(Boolean, default=True)
    notes       = Column(Text)
    created_at  = Column(DateTime(timezone=True), server_default=text('NOW()'))
    updated_at  = Column(DateTime(timezone=True), server_default=text('NOW()'))


class Project(Base):
    __tablename__ = 'projects'

    id          = Column(Integer, primary_key=True)
    client_id   = Column(Integer, ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    name        = Column(Text, nullable=False)
    description = Column(Text)
    am_id       = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'))
    is_active   = Column(Boolean, default=True)
    start_date  = Column(Date)
    end_date    = Column(Date)
    created_at  = Column(DateTime(timezone=True), server_default=text('NOW()'))
    updated_at  = Column(DateTime(timezone=True), server_default=text('NOW()'))


class ContentItem(Base):
    __tablename__ = 'content_items'

    id               = Column(Integer, primary_key=True)
    project_id       = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'))
    client_id        = Column(Integer, ForeignKey('clients.id'))   # denorm, set on create
    number           = Column(Text)
    format           = Column(content_format_enum, nullable=False)
    topic            = Column(Text)
    description      = Column(Text)
    # idea_id removed (circular FK with ideas.implemented_content_id)
    author_id        = Column(Integer, ForeignKey('users.id'))
    producer_id      = Column(Integer, ForeignKey('users.id'))
    photographer_id  = Column(Integer, ForeignKey('users.id'))
    editor_id        = Column(Integer, ForeignKey('users.id'))
    designer_id      = Column(Integer, ForeignKey('users.id'))
    am_id            = Column(Integer, ForeignKey('users.id'))
    publish_date     = Column(Date)
    pipeline_status  = Column(pipeline_step_enum, default='idea')
    client_approved  = Column(Boolean, default=False)
    created_by       = Column(Integer, ForeignKey('users.id'))
    created_at       = Column(DateTime(timezone=True), server_default=text('NOW()'))
    updated_at       = Column(DateTime(timezone=True), server_default=text('NOW()'))

    # content-plan fields (added 2026-08-30 via _MIGRATIONS — ADD COLUMN IF NOT EXISTS)
    rubric           = Column(Text)
    platform         = Column(Text)                 # instagram | tiktok | youtube | telegram | ...
    publish_at       = Column(DateTime(timezone=True))   # date + time; publish_date kept for back-compat
    hook             = Column(Text)
    script           = Column(Text)
    caption          = Column(Text)
    hashtags         = Column(Text)
    smm_id           = Column(Integer, ForeignKey('users.id'))
    copywriter_id    = Column(Integer, ForeignKey('users.id'))

    __table_args__ = (
        UniqueConstraint('client_id', 'number', name='uq_content_client_number'),
    )


class ContentPipelineStep(Base):
    __tablename__ = 'content_pipeline_steps'

    id           = Column(Integer, primary_key=True)
    content_id   = Column(Integer, ForeignKey('content_items.id', ondelete='CASCADE'), nullable=False)
    step         = Column(pipeline_step_enum, nullable=False)
    assignee_id  = Column(Integer, ForeignKey('users.id'))
    # task_id removed (circular FK). Get via: SELECT * FROM tasks WHERE pipeline_step_id = id
    deadline     = Column(DateTime(timezone=True))
    status       = Column(Text, default='pending')
    notes        = Column(Text)
    links        = Column(JSONB, default=list)
    started_at   = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    created_at   = Column(DateTime(timezone=True), server_default=text('NOW()'))
    # UNIQUE(content_id, step) removed — 'revisions' can repeat

    __table_args__ = (
        Index('idx_pipeline_content_step', 'content_id', 'step'),
    )


class Task(Base):
    __tablename__ = 'tasks'

    id               = Column(Integer, primary_key=True)
    project_id       = Column(Integer, ForeignKey('projects.id', ondelete='SET NULL'))   # not CASCADE
    client_id        = Column(Integer, ForeignKey('clients.id'))
    content_id       = Column(Integer, ForeignKey('content_items.id'))
    pipeline_step_id = Column(Integer, ForeignKey('content_pipeline_steps.id'))   # one direction only
    title            = Column(Text, nullable=False)
    description      = Column(Text)
    type             = Column(task_type_enum, default='general')
    assignee_id      = Column(Integer, ForeignKey('users.id'))
    created_by       = Column(Integer, ForeignKey('users.id'))
    priority         = Column(task_priority_enum, default='normal')
    deadline         = Column(DateTime(timezone=True))
    status           = Column(task_status_enum, default='pending')
    actual_completion = Column(DateTime(timezone=True))
    overdue_reason   = Column(overdue_reason_enum)
    overdue_comment  = Column(Text)
    related_links    = Column(JSONB, default=list)
    created_at       = Column(DateTime(timezone=True), server_default=text('NOW()'))
    updated_at       = Column(DateTime(timezone=True), server_default=text('NOW()'))

    __table_args__ = (
        Index('idx_tasks_assignee_status', 'assignee_id', 'status'),
        Index('idx_tasks_project', 'project_id'),
    )


class Idea(Base):
    __tablename__ = 'ideas'

    id                    = Column(Integer, primary_key=True)
    project_id            = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'))
    client_id             = Column(Integer, ForeignKey('clients.id'))
    title                 = Column(Text, nullable=False)
    description           = Column(Text)
    format                = Column(content_format_enum)
    proposed_by           = Column(Integer, ForeignKey('users.id'))
    status                = Column(idea_status_enum, default='new')
    implemented_content_id = Column(Integer, ForeignKey('content_items.id'))   # one direction
    votes_count           = Column(Integer, default=0)
    created_at            = Column(DateTime(timezone=True), server_default=text('NOW()'))
    updated_at            = Column(DateTime(timezone=True), server_default=text('NOW()'))


class IdeaVote(Base):
    __tablename__ = 'idea_votes'

    id         = Column(Integer, primary_key=True)
    idea_id    = Column(Integer, ForeignKey('ideas.id', ondelete='CASCADE'), nullable=False)
    user_id    = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    emoji      = Column(Text, default='👍')
    created_at = Column(DateTime(timezone=True), server_default=text('NOW()'))

    __table_args__ = (
        UniqueConstraint('idea_id', 'user_id', name='uq_idea_vote'),
    )


class Blocker(Base):
    __tablename__ = 'blockers'

    id           = Column(Integer, primary_key=True)
    task_id      = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'))
    content_id   = Column(Integer, ForeignKey('content_items.id'))
    project_id   = Column(Integer, ForeignKey('projects.id'))
    reported_by  = Column(Integer, ForeignKey('users.id'))
    assigned_to  = Column(Integer, ForeignKey('users.id'))
    title        = Column(Text, nullable=False)
    description  = Column(Text)
    status       = Column(blocker_status_enum, default='active')
    resolved_by  = Column(Integer, ForeignKey('users.id'))
    resolved_at  = Column(DateTime(timezone=True))
    resolution   = Column(Text)
    created_at   = Column(DateTime(timezone=True), server_default=text('NOW()'))
    updated_at   = Column(DateTime(timezone=True), server_default=text('NOW()'))

    __table_args__ = (
        Index('idx_blockers_status', 'status'),
    )


class Approval(Base):
    __tablename__ = 'approvals'

    id           = Column(Integer, primary_key=True)
    content_id   = Column(Integer, ForeignKey('content_items.id', ondelete='CASCADE'))
    step         = Column(pipeline_step_enum)
    requested_by = Column(Integer, ForeignKey('users.id'))
    approver_id  = Column(Integer, ForeignKey('users.id'))
    status       = Column(approval_status_enum, default='pending')
    comment      = Column(Text)
    links        = Column(JSONB, default=list)
    created_at   = Column(DateTime(timezone=True), server_default=text('NOW()'))
    updated_at   = Column(DateTime(timezone=True), server_default=text('NOW()'))


class ClientRating(Base):
    __tablename__ = 'client_ratings'

    id             = Column(Integer, primary_key=True)
    client_id      = Column(Integer, ForeignKey('clients.id', ondelete='CASCADE'), nullable=False)
    project_id     = Column(Integer, ForeignKey('projects.id'))
    content_id     = Column(Integer, ForeignKey('content_items.id'))
    rating_context = Column(rating_context_enum, nullable=False)
    score          = Column(SmallInteger, nullable=False)
    comment        = Column(Text)
    rated_by       = Column(Integer, ForeignKey('users.id'))
    created_at     = Column(DateTime(timezone=True), server_default=text('NOW()'))

    __table_args__ = (
        CheckConstraint('score BETWEEN 1 AND 10', name='chk_rating_score'),
        CheckConstraint(
            "(rating_context = 'content' AND content_id IS NOT NULL) OR "
            "(rating_context IN ('project_overall', 'communication') AND project_id IS NOT NULL)",
            name='chk_rating_context'
        ),
    )


class ActivityEvent(Base):
    __tablename__ = 'activity_events'

    id          = Column(BigInteger, primary_key=True)
    entity      = Column(event_entity_enum, nullable=False)
    entity_id   = Column(Integer, nullable=False)
    action      = Column(event_action_enum, nullable=False)
    actor_id    = Column(Integer, ForeignKey('users.id'))
    action_id   = Column(Text)   # UUID linking events from same user action
    payload     = Column(JSONB, default=dict)
    happened_at = Column(DateTime(timezone=True), server_default=text('NOW()'))

    __table_args__ = (
        Index('idx_events_entity', 'entity', 'entity_id'),
        Index('idx_events_actor', 'actor_id'),
        Index('idx_events_happened', 'happened_at'),
        Index('idx_events_action_id', 'action_id'),
    )


class EmployeeRawStats(Base):
    __tablename__ = 'employee_raw_stats'

    id                      = Column(Integer, primary_key=True)
    user_id                 = Column(Integer, ForeignKey('users.id'), nullable=False)
    period_start            = Column(Date, nullable=False)   # first day of month
    tasks_total             = Column(Integer, default=0)
    tasks_done              = Column(Integer, default=0)
    tasks_overdue           = Column(Integer, default=0)
    content_produced        = Column(Integer, default=0)
    avg_completion_hours    = Column(Numeric(6, 2))
    blockers_reported       = Column(Integer, default=0)
    blockers_resolved       = Column(Integer, default=0)
    ideas_proposed          = Column(Integer, default=0)
    ideas_implemented       = Column(Integer, default=0)
    help_requested          = Column(Integer, default=0)
    help_provided           = Column(Integer, default=0)
    checkins_morning        = Column(Integer, default=0)
    checkins_evening        = Column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint('user_id', 'period_start', name='uq_stats_user_period'),
    )


class Setting(Base):
    __tablename__ = 'settings'

    id         = Column(Integer, primary_key=True)
    scope      = Column(Text, nullable=False)   # 'global', 'project:<id>', 'user:<id>'
    key        = Column(Text, nullable=False)
    value      = Column(Text)
    updated_by = Column(Integer, ForeignKey('users.id'))
    updated_at = Column(DateTime(timezone=True), server_default=text('NOW()'))

    __table_args__ = (
        UniqueConstraint('scope', 'key', name='uq_settings_scope_key'),
    )


class TaskAssignee(Base):
    __tablename__ = 'task_assignees'

    id      = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)

    __table_args__ = (
        UniqueConstraint('task_id', 'user_id', name='uq_task_assignee'),
        Index('idx_task_assignee_user', 'user_id'),
    )


class ReferenceItem(Base):
    __tablename__ = 'reference_items'

    id         = Column(Integer, primary_key=True)
    task_id    = Column(Integer, ForeignKey('tasks.id', ondelete='CASCADE'))
    content_id = Column(Integer, ForeignKey('content_items.id', ondelete='CASCADE'))
    kind       = Column(Text, nullable=False)          # 'link' | 'file'
    url        = Column(Text)                          # links
    title      = Column(Text)
    tg_file_id = Column(Text)                          # files (stored on Telegram)
    file_name  = Column(Text)
    mime       = Column(Text)
    added_by   = Column(Integer, ForeignKey('users.id'))
    created_at = Column(DateTime(timezone=True), server_default=text('NOW()'))

    __table_args__ = (
        Index('idx_ref_task', 'task_id'),
        Index('idx_ref_content', 'content_id'),
    )


class ProjectChat(Base):
    """Binds a project to a Telegram chat (and optional forum topic) for notifications."""
    __tablename__ = 'project_chats'

    id         = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='CASCADE'), nullable=False)
    chat_id    = Column(BigInteger, nullable=False)
    thread_id  = Column(Integer)                       # forum topic message_thread_id, NULL = whole chat
    title      = Column(Text)                          # topic / chat name, for display
    bound_by   = Column(Integer, ForeignKey('users.id'))
    created_at = Column(DateTime(timezone=True), server_default=text('NOW()'))

    __table_args__ = (
        UniqueConstraint('chat_id', 'thread_id', name='uq_project_chat_location'),
        Index('idx_project_chat_project', 'project_id'),
    )


class ShootSession(Base):
    __tablename__ = 'shoot_sessions'

    id         = Column(Integer, primary_key=True)
    title      = Column(Text, nullable=False)
    shoot_at   = Column(DateTime(timezone=True))       # date + time
    location   = Column(Text)
    client_id  = Column(Integer, ForeignKey('clients.id', ondelete='SET NULL'))
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='SET NULL'))
    status     = Column(Text, default='planned')       # planned | done | cancelled
    notes      = Column(Text)
    created_by = Column(Integer, ForeignKey('users.id'))
    created_at = Column(DateTime(timezone=True), server_default=text('NOW()'))
    updated_at = Column(DateTime(timezone=True), server_default=text('NOW()'))

    __table_args__ = (
        Index('idx_shoot_at', 'shoot_at'),
    )


class ShootParticipant(Base):
    __tablename__ = 'shoot_participants'

    id       = Column(Integer, primary_key=True)
    shoot_id = Column(Integer, ForeignKey('shoot_sessions.id', ondelete='CASCADE'), nullable=False)
    user_id  = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    role     = Column(Text)                            # free-text on-set role, optional

    __table_args__ = (
        UniqueConstraint('shoot_id', 'user_id', name='uq_shoot_participant'),
        Index('idx_shoot_participant_user', 'user_id'),
    )


# ─────────────────────────────────────────────
# DB INIT
# ─────────────────────────────────────────────

# Idempotent column additions for existing tables (create_all only makes new tables).
_MIGRATIONS = [
    "ALTER TABLE content_items ADD COLUMN IF NOT EXISTS rubric TEXT",
    "ALTER TABLE content_items ADD COLUMN IF NOT EXISTS platform TEXT",
    "ALTER TABLE content_items ADD COLUMN IF NOT EXISTS publish_at TIMESTAMPTZ",
    "ALTER TABLE content_items ADD COLUMN IF NOT EXISTS hook TEXT",
    "ALTER TABLE content_items ADD COLUMN IF NOT EXISTS script TEXT",
    "ALTER TABLE content_items ADD COLUMN IF NOT EXISTS caption TEXT",
    "ALTER TABLE content_items ADD COLUMN IF NOT EXISTS hashtags TEXT",
    "ALTER TABLE content_items ADD COLUMN IF NOT EXISTS smm_id INTEGER REFERENCES users(id)",
    "ALTER TABLE content_items ADD COLUMN IF NOT EXISTS copywriter_id INTEGER REFERENCES users(id)",
]


async def init_db():
    """Create all tables. Run once on startup."""
    async with engine.begin() as conn:
        # Create enums first
        for enum in [
            user_role_enum, content_format_enum, pipeline_step_enum,
            task_type_enum, task_status_enum, task_priority_enum,
            overdue_reason_enum, rating_context_enum, event_entity_enum,
            event_action_enum, idea_status_enum, blocker_status_enum,
            approval_status_enum,
        ]:
            await conn.execute(text(
                f"DO $$ BEGIN CREATE TYPE {enum.name} AS ENUM ({', '.join(repr(e) for e in enum.enums)}); "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
            ))
        await conn.run_sync(Base.metadata.create_all)
        for ddl in _MIGRATIONS:
            try:
                await conn.execute(text(ddl))
            except Exception as e:  # noqa: BLE001 — never let a migration crash startup
                print(f"⚠️ migration skipped: {ddl!r} -> {e}")


async def get_db():
    """FastAPI dependency."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
