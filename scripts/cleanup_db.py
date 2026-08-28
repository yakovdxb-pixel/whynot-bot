"""
One-time WHY NOT? OS database cleanup — wipes all operational rows created
during development/testing, keeps the `users` table intact.

Run it on Railway (so it can reach postgres.railway.internal):

    railway run python scripts/cleanup_db.py

Everything runs inside a single transaction: it either clears all listed
tables or changes nothing.
"""
import asyncio
import os

import asyncpg

# Prefer the service's own DATABASE_URL; fall back to the internal default.
DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://whynot:whynot_secret_2026@postgres.railway.internal:5432/whynot_os",
)
# asyncpg wants a plain postg:// scheme, not SQLAlchemy's postgresql+asyncpg://
DB_URL = DB_URL.replace("postgresql+asyncpg://", "postgresql://", 1)

# FK-safe delete order (children before parents).
TABLES = [
    "content_pipeline_steps",
    "idea_votes",
    "approvals",
    "activity_events",
    "blockers",
    "ideas",
    "tasks",
    "content_items",
    "projects",
    "clients",
]


async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        async with conn.transaction():
            for table in TABLES:
                before = await conn.fetchval(f"SELECT count(*) FROM {table}")
                await conn.execute(f"DELETE FROM {table}")
                print(f"  {table:<24} -{before}")
        kept = await conn.fetchval("SELECT count(*) FROM users")
        print(f"Done. users kept: {kept}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
