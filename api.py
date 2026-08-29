"""
WhyNot Agency — FastAPI backend + Telegram Mini App server
"""
import os, sqlite3, hashlib, hmac, json, asyncio
from datetime import datetime
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import telegram
import httpx

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH   = os.getenv("DB_PATH", "agency.db")

app = FastAPI(title="WhyNot API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── WHY NOT? OS — Mini App static assets + REST API ─────────────
_WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
try:
    app.mount("/static", StaticFiles(directory=_WEBAPP_DIR, html=True), name="static")
except Exception as e:  # directory missing at build time — non-fatal
    print(f"⚠️ /static not mounted: {e}")

try:
    from routes_whynot import router as whynot_router
    app.include_router(whynot_router)
    print("✅ WHY NOT? OS routes mounted")
except Exception as e:
    print(f"⚠️ WHY NOT? OS routes not mounted: {e}")

# ── DB helpers ───────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT, first_name TEXT, last_name TEXT,
        role TEXT DEFAULT 'executor',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS groups (
        group_id INTEGER PRIMARY KEY,
        title TEXT, thread_id INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER, thread_id INTEGER DEFAULT 0,
        type TEXT, description TEXT, refs TEXT,
        deadline TEXT, assignee_id INTEGER,
        status TEXT DEFAULT 'active',
        created_by INTEGER,
        msg_id INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS content_plan (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER, thread_id INTEGER DEFAULT 0,
        cp_type TEXT, description TEXT, refs TEXT,
        deadline TEXT, status TEXT DEFAULT 'active',
        created_by INTEGER,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS ratings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER, executor_id INTEGER,
        rating INTEGER, comment TEXT,
        rated_by INTEGER,
        created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    conn.commit()
    conn.close()

# ── Telegram WebApp auth ────────────────────────────────────────

def validate_tg_data(init_data: str) -> dict:
    """Validate Telegram WebApp initData and return user dict."""
    if not BOT_TOKEN:
        return {"id": 0, "first_name": "Dev", "username": "dev"}
    try:
        parsed = {}
        for chunk in init_data.split("&"):
            k, _, v = chunk.partition("=")
            parsed[k] = unquote(v)
        received_hash = parsed.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            raise HTTPException(403, "Invalid auth")
        user = json.loads(parsed.get("user", "{}"))
        return user
    except Exception:
        raise HTTPException(403, "Auth failed")

async def get_current_user(request: Request) -> dict:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        return {"id": 0, "first_name": "Dev", "username": "dev", "role": "am"}
    user = validate_tg_data(init_data)
    conn = get_db()
    row = conn.execute("SELECT role FROM users WHERE user_id=?", (user["id"],)).fetchone()
    conn.close()
    user["role"] = row["role"] if row else "executor"
    return user

# ── Models ───────────────────────────────────────────────────────

class TaskCreate(BaseModel):
    group_id: int
    type: str
    description: str
    refs: Optional[str] = ""
    deadline: Optional[str] = ""
    assignee_id: Optional[int] = None

class TaskUpdate(BaseModel):
    status: str

# ── Routes ───────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
@app.get("/webapp", response_class=HTMLResponse)
@app.get("/webapp/index.html", response_class=HTMLResponse)
async def serve_app():
    from fastapi.responses import Response
    paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "index.html"),
        "/app/webapp/index.html",
        "webapp/index.html",
    ]
    for path in paths:
        if os.path.exists(path):
            with open(path, "rb") as f:
                return Response(content=f.read(), media_type="text/html; charset=utf-8")
    return HTMLResponse("<h1>WhyNot Agency</h1><p>App file not found</p>")


@app.get("/sw.js")
async def service_worker():
    """Serve the PWA service worker from the root so its scope covers /webapp."""
    from fastapi.responses import Response
    path = os.path.join(_WEBAPP_DIR, "sw.js")
    body = open(path, "rb").read() if os.path.exists(path) else b"/* no sw */"
    return Response(content=body, media_type="text/javascript", headers={
        "Service-Worker-Allowed": "/",
        "Cache-Control": "no-cache",
    })

@app.get("/api/companies")
async def list_companies():
    conn = get_db()
    rows = conn.execute("SELECT * FROM groups ORDER BY title").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/legacy/tasks")
async def list_tasks(group_id: Optional[int] = None, status: Optional[str] = None):
    conn = get_db()
    q = "SELECT t.*, u.first_name as assignee_name FROM tasks t LEFT JOIN users u ON t.assignee_id=u.user_id WHERE 1=1"
    params = []
    if group_id:
        q += " AND t.group_id=?"; params.append(group_id)
    if status:
        q += " AND t.status=?"; params.append(status)
    q += " ORDER BY t.created_at DESC LIMIT 100"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/legacy/tasks")
async def create_task(task: TaskCreate, user: dict = Depends(get_current_user)):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO tasks (group_id, type, description, refs, deadline, assignee_id, created_by) VALUES (?,?,?,?,?,?,?)",
        (task.group_id, task.type, task.description, task.refs, task.deadline, task.assignee_id, user.get("id", 0))
    )
    task_id = cur.lastrowid
    conn.commit()
    group = conn.execute("SELECT * FROM groups WHERE group_id=?", (task.group_id,)).fetchone()
    assignee = conn.execute("SELECT * FROM users WHERE user_id=?", (task.assignee_id,)).fetchone() if task.assignee_id else None
    conn.close()
    if BOT_TOKEN and group:
        asyncio.create_task(_post_task_card(task_id, task, group, assignee, user))
    return {"id": task_id, "status": "active"}

async def _post_task_card(task_id, task, group, assignee, creator):
    try:
        bot = telegram.Bot(token=BOT_TOKEN)
        TYPE_LABELS = {
            "shoot": "🎬 Съемка", "publish": "📢 Публикация",
            "design": "🎨 Дизайн", "edit": "✂️ Монтаж", "other": "📌 Другое",
            "post": "📸 Пост", "stories": "📱 Сторис",
            "reels": "🎬 Рилс", "actual": "🎯 Актуальное",
        }
        type_label = TYPE_LABELS.get(task.type, task.type)
        assignee_name = assignee["first_name"] if assignee else "—"
        deadline_str = task.deadline or "без даты"
        creator_name = creator.get("first_name", "AM")
        text = (
            f"📋 *Новая задача #{task_id}*\n\n"
            f"*Тип:* {type_label}\n"
            f"*Описание:* {task.description}\n"
        )
        if task.refs:
            text += f"*Референсы:* {task.refs}\n"
        text += (
            f"*Дедлайн:* {deadline_str}\n"
            f"*Исполнитель:* {assignee_name}\n"
            f"*Поставил:* {creator_name}\n"
            f"\n⚪ Ожидает"
        )
        await bot.send_message(
            chat_id=group["group_id"],
            text=text,
            parse_mode="Markdown",
            message_thread_id=group["thread_id"] if group["thread_id"] else None,
        )
    except Exception as e:
        print(f"Error posting task card: {e}")

@app.patch("/api/legacy/tasks/{task_id}")
async def update_task(task_id: int, update: TaskUpdate, user: dict = Depends(get_current_user)):
    conn = get_db()
    conn.execute(
        "UPDATE tasks SET status=?, updated_at=datetime('now') WHERE id=?",
        (update.status, task_id)
    )
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/legacy/team")
async def list_team():
    conn = get_db()
    rows = conn.execute(
        "SELECT u.*, COUNT(t.id) as task_count, AVG(r.rating) as avg_rating "
        "FROM users u "
        "LEFT JOIN tasks t ON t.assignee_id=u.user_id AND t.status NOT IN ('approved','published') "
        "LEFT JOIN ratings r ON r.executor_id=u.user_id "
        "WHERE u.role IN ('executor','am') "
        "GROUP BY u.user_id ORDER BY avg_rating DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/stats")
async def get_stats():
    conn = get_db()
    active    = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='active'").fetchone()[0]
    submitted = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='submitted'").fetchone()[0]
    companies = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    avg_r     = conn.execute("SELECT AVG(rating) FROM ratings").fetchone()[0]
    conn.close()
    return {"active_tasks": active, "submitted": submitted, "companies": companies,
            "avg_rating": round(avg_r, 1) if avg_r else 0}

@app.get("/api/debug")
async def debug_info():
    token = BOT_TOKEN
    tg_ok = False
    tg_info = ""
    if token:
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
                tg_info = res.json()
                tg_ok = res.status_code == 200
        except Exception as e:
            tg_info = str(e)
    import sys
    return {
        "token_set": bool(token),
        "telegram_ok": tg_ok,
        "telegram_info": tg_info,
        "python_version": sys.version,
        "cwd": os.getcwd(),
        "webapp_url": os.getenv("WEBAPP_URL", ""),
    }

@app.get("/api/bot-status")
async def bot_status():
    """Check webhook info and polling status."""
    if not BOT_TOKEN:
        return {"error": "BOT_TOKEN not set"}
    async with httpx.AsyncClient() as client:
        wh_res = await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo",
            timeout=10
        )
        wh_data = wh_res.json()
        upd_res = await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"limit": 1, "timeout": 0},
            timeout=10
        )
        upd_data = upd_res.json()
    webhook_url = wh_data.get("result", {}).get("url", "")
    polling_conflict = upd_data.get("error_code") == 409
    return {
        "webhook_url": webhook_url,
        "webhook_set": bool(webhook_url),
        "polling_active_conflict": polling_conflict,
        "webhook_info": wh_data.get("result", {}),
        "updates_response": upd_data,
    }

@app.post("/api/delete-webhook")
async def delete_webhook_endpoint():
    """Delete any set webhook so polling can work."""
    if not BOT_TOKEN:
        return {"error": "BOT_TOKEN not set"}
    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
            json={"drop_pending_updates": False},
            timeout=10
        )
    return res.json()

# ── WHY NOT? OS — Postgres layer ────────────────────────────────

@app.get("/health")
async def health():
    """Liveness + Postgres connectivity check for WHY NOT? OS."""
    from sqlalchemy import text as _sql_text
    try:
        from db.models import engine as _pg_engine
        async with _pg_engine.connect() as conn:
            await conn.execute(_sql_text("SELECT 1"))
            tbls = (await conn.execute(_sql_text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name IN ('task_assignees','reference_items',"
                "'project_chats','shoot_sessions','shoot_participants')"
            ))).scalar()
            cols = (await conn.execute(_sql_text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='content_items' "
                "AND column_name IN ('rubric','platform','publish_at','hook',"
                "'script','caption','hashtags','smm_id','copywriter_id')"
            ))).scalar()
        return {"db": "ok", "new_tables": int(tbls), "content_cols": int(cols)}
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"db": "error", "detail": str(e)})


def _bot_supervisor():
    """Run bot.py as an isolated subprocess (uvloop-safe), auto-restart on exit.

    Skipped when RUN_BOT=0 — set that on the web service once bot.py runs
    as its own Railway 'worker' process (see Procfile).
    """
    import subprocess, sys, time
    bot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
    while True:
        try:
            result = subprocess.run([sys.executable, bot_path], check=False)
            print(f"⚠️ bot subprocess exited ({result.returncode}), restart in 5s")
        except Exception as e:
            print(f"❌ bot subprocess failed: {e}")
        time.sleep(5)


# ── Startup ─────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    # Legacy sqlite tables (old Agency endpoints)
    init_db()
    print("✅ sqlite DB initialized")

    # WHY NOT? OS Postgres schema (idempotent)
    try:
        from db.models import init_db as init_pg_db
        await init_pg_db()
        print("✅ Postgres schema ensured")
    except Exception as e:
        print(f"⚠️ Postgres init skipped: {e}")

    # Keep the Telegram bot alive unless it runs as its own process
    if os.getenv("RUN_BOT", "1") != "0":
        import threading
        threading.Thread(target=_bot_supervisor, daemon=True).start()
        print("🤖 bot subprocess supervisor started (RUN_BOT=1)")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
