"""
WhyNot Agency — FastAPI (daemon thread) + Telegram bot (subprocess)
Running bot as a subprocess avoids all asyncio/uvloop event loop conflicts.
The bot gets a clean Python process with no uvloop policy installed.
"""
import os, threading, uvicorn, logging, time, subprocess, sys
from api import app, init_db

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def run_api():
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Starting FastAPI on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

if __name__ == "__main__":
    init_db()
    
    # Start API in a daemon thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    logger.info("✅ API thread started")
    
    # Give API a moment to bind port
    time.sleep(2)
    
    # Run bot as a completely separate subprocess.
    # This isolates it from uvloop which uvicorn installs as the asyncio policy.
    bot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
    logger.info(f"🤖 Starting bot subprocess: {bot_path}")
    
    while True:
        try:
            result = subprocess.run([sys.executable, bot_path], check=False)
            logger.warning(f"⚠️ Bot subprocess exited with code {result.returncode}, restarting in 5s...")
        except Exception as e:
            logger.error(f"❌ Failed to start bot: {e}")
        time.sleep(5)
