"""
WhyNot Agency — FastAPI backend + Telegram Mini App server
"""
import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import httpx

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

app = FastAPI(title="WhyNot API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── WHY NOT? OS — Mini App static assets + REST API ─────────────
_WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
try:
    app.mount("/static", StaticFiles(directory=_WEBAPP_DIR, html=True), name="static")
except Exception as e:  # directory missing at build time — non-fatal
    print(f"⚠️ /static not mounted: {e}")

try:
    from routes_whynot import router as whynot_router, _require_admin
    app.include_router(whynot_router)
    print("✅ WHY NOT? OS routes mounted")
except Exception as e:
    print(f"⚠️ WHY NOT? OS routes not mounted: {e}")

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

@app.get("/api/bot-status")
async def bot_status(request: Request):
    """Check webhook info and polling status. Admin-only (X-Admin-Secret)."""
    _require_admin(request)
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
                "'project_chats','shoot_sessions','shoot_participants','content_assignees','status_events')"
            ))).scalar()
            cols = (await conn.execute(_sql_text(
                "SELECT count(*) FROM information_schema.columns WHERE "
                "(table_name='tasks' AND column_name IN ('overdue_notified_at','job_kind','file_id','submitted_at')) OR "
                "(table_name='reference_items' AND column_name IN ('project_id','tg_message_id')) OR "
                "(table_name='clients' AND column_name IN ('goals','audience','tone_of_voice','competitors'))"
            ))).scalar()
        return {"db": "ok", "new_tables": int(tbls), "sep_cols": int(cols)}
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
