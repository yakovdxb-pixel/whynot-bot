"""
WhyNot Agency â€” FastAPI backend + Telegram Mini App server
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH   = os.getenv("DB_PATH", "agency.db")

app = FastAPI(title="WhyNot API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# â”€â”€ DB helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

# â”€â”€ Telegram WebApp auth â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def validate_tg_data(init_data: str) -> dict:
    """Validate Telegram WebApp initData and return user dict."""
    if not BOT_TOKEN:
        # Dev mode: return mock user
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
        # Allow in dev mode
        return {"id": 0, "first_name": "Dev", "username": "dev", "role": "am"}
    user = validate_tg_data(init_data)
    # Fetch role from DB
    conn = get_db()
    row = conn.execute("SELECT role FROM users WHERE user_id=?", (user["id"],)).fetchone()
    conn.close()
    user["role"] = row["role"] if row else "executor"
    return user

# â”€â”€ Models â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class TaskCreate(BaseModel):
    group_id: int
    type: str
    description: str
    refs: Optional[str] = ""
    deadline: Optional[str] = ""
    assignee_id: Optional[int] = None

class TaskUpdate(BaseModel):
    status: str

# â”€â”€ Routes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/", response_class=HTMLResponse)
@app.get("/webapp/index.html", response_class=HTMLResponse)
async def serve_app():
    path = os.path.join(os.path.dirname(__file__), "webapp", "index.html")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>WhyNot Agency</h1>", status_code=200)

@app.get("/api/companies")
async def list_companies():
    conn = get_db()
    rows = conn.execute("SELECT * FROM groups ORDER BY title").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/tasks")
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

@app.post("/api/tasks")
async def create_task(task: TaskCreate, user: dict = Depends(get_current_user)):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO tasks (group_id, type, description, refs, deadline, assignee_id, created_by) VALUES (?,?,?,?,?,?,?)",
        (task.group_id, task.type, task.description, task.refs, task.deadline, task.assignee_id, user.get("id", 0))
    )
    task_id = cur.lastrowid
    conn.commit()

    # Get group info
    group = conn.execute("SELECT * FROM groups WHERE group_id=?", (task.group_id,)).fetchone()
    assignee = conn.execute("SELECT * FROM users WHERE user_id=?", (task.assignee_id,)).fetchone() if task.assignee_id else None
    conn.close()

    # Post card to Telegram group
    if BOT_TOKEN and group:
        asyncio.create_task(_post_task_card(task_id, task, group, assignee, user))

    return {"id": task_id, "status": "active"}

async def _post_task_card(task_id, task, group, assignee, creator):
    try:
        bot = telegram.Bot(token=BOT_TOKEN)
        TYPE_LABELS = {
            "shoot": "ðŸŽ¬ Ð¡ÑŠÐµÐ¼ÐºÐ°", "publish": "ðŸ“¢ ÐŸÑƒÐ±Ð»Ð¸ÐºÐ°Ñ†Ð¸Ñ",
            "design": "ðŸŽ¨ Ð”Ð¸Ð·Ð°Ð¹Ð½", "edit": "âœ‚ï¸ ÐœÐ¾Ð½Ñ‚Ð°Ð¶", "other": "ðŸ“Œ ÑÑ€ÑƒÐ³Ð¾Ðµ",
            "post": "ðŸ“¸ ÐŸÐ¾ÑÑ‚", "stories": "ðŸ“± Ð¡Ñ‚Ð¾Ñ€Ð¸Ñ",
            "reels": "ðŸŽ¬ Ð Ð¸Ð»Ñ", "actual": "ðŸŽ¯ ÐÐºÑ‚ÑƒÐ°Ð»ÑŒÐ½Ð¾Ðµ",
        }
        type_label = TYPE_LABELS.get(task.type, task.type)
        assignee_name = assignee["first_name"] if assignee else "â€”"
        deadline_str = task.deadline or "Ð±ÐµÐ· Ð´Ð°Ñ‚Ñ‹"
        creator_name = creator.get("first_name", "AM")

        text = (
            f"ðŸ“‹ *ÐÐ¾Ð²Ð°Ñ Ð·Ð°Ð´Ð°Ñ‡Ð° #{task_id}*\n\n"
            f"*Ð¢Ð¸Ð¿:* {type_label}\n"
            f"*ÐžÐ¿Ð¸ÑÐ°Ð½Ð¸Ðµ:* {task.description}\n"
        )
        if task.refs:
            text += f"*Ð ÐµÑ„ÐµÑ€ÐµÐ½ÑÑ‹:* {task.refs}\n"
        text += (
            f"*Ð”ÐµÐ´Ð»Ð°Ð¹Ð½:* {deadline_str}\n"
            f"*Ð˜ÑÐ¿Ð¾Ð»Ð½Ð¸Ñ‚ÐµÐ»ÑŒ:* {assignee_name}\n"
            f"*ÐŸÐ¾ÑÑ‚Ð°Ð²Ð¸Ð»:* {creator_name}\n"
            f"\nâšª ÐžÐ¶Ð¸Ð´Ð°ÐµÑ‚"
        )
        await bot.send_message(
            chat_id=group["group_id"],
            text=text,
            parse_mode="Markdown",
            message_thread_id=group["thread_id"] if group["thread_id"] else None,
        )
    except Exception as e:
        print(f"Error posting task card: {e}")

@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: int, update: TaskUpdate, user: dict = Depends(get_current_user)):
    conn = get_db()
    conn.execute(
        "UPDATE tasks SET status=?, updated_at=datetime('now') WHERE id=?",
        (update.status, task_id)
    )
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/team")
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
async def get_stats(group_id: Optional[int] = None):
    conn = get_db()
    q_filter = f"WHERE group_id={group_id}" if group_id else ""
    active   = conn.execute(f"SELECT COUNT(*) FROM tasks {q_filter} {'AND' if q_filter else 'WHERE'} status='active'").fetchone()[0] if not q_filter else conn.execute(f"SELECT COUNT(*) FROM tasks WHERE group_id=? AND status='active'", (group_id,)).fetchone()[0]
    submitted = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE {'group_id=? AND ' if group_id else ''}status='submitted'", ([group_id] if group_id else [])).fetchone()[0]
    companies = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    avg_rating = conn.execute("SELECT AVG(rating) FROM ratings").fetchone()[0]
    conn.close()
    return {
        "active_tasks": active,
        "submitted": submitted,
        "companies": companies,
        "avg_rating": round(avg_rating, 1) if avg_rating else 0,
    }

@app.get("/api/stats")
async def get_stats_simple():
    conn = get_db()
    active    = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='active'").fetchone()[0]
    submitted = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='submitted'").fetchone()[0]
    companies = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    avg_r     = conn.execute("SELECT AVG(rating) FROM ratings").fetchone()[0]
    conn.close()
    return {"active_tasks": active, "submitted": submitted, "companies": companies,
            "avg_rating": round(avg_r, 1) if avg_r else 0}

# â”€â”€ Startup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.on_event("startup")
async def startup():
    init_db()
    print("âœ… DB initialized")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
