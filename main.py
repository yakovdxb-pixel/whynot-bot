"""
WhyNot Agency — FastAPI + Telegram bot in one process
"""
import os, threading, uvicorn, logging, importlib.util
from api import app, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_api():
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

def run_bot_thread():
    """bot.py's main() is sync and calls asyncio.run() internally.
    Must be called directly (not via await) in its own thread."""
    try:
        spec = importlib.util.spec_from_file_location("bot", "bot.py")
        bot_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot_mod)
        if hasattr(bot_mod, "main"):
            logger.info("Starting bot...")
            bot_mod.main()  # sync call — bot.py uses asyncio.run() internally
    except Exception as e:
        logger.error(f"Bot crashed: {e}", exc_info=True)

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=run_bot_thread, daemon=True)
    t.start()
    logger.info("Bot thread started, launching API...")
    run_api()
