"""
WhyNot Agency — FastAPI (daemon thread) + Telegram bot (main thread)
ptb v21 run_polling() calls signal.signal() which requires the MAIN thread.
So bot must run in main thread; API runs in daemon thread.
"""
import os, threading, uvicorn, logging, importlib.util
from api import app, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_api():
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Starting API on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

if __name__ == "__main__":
    init_db()

    # Start API in daemon background thread
    t = threading.Thread(target=run_api, daemon=True)
    t.start()
    logger.info("API thread started")

    # Run bot in MAIN thread (required for signal.signal() inside run_polling())
    try:
        spec = importlib.util.spec_from_file_location("bot", "bot.py")
        bot_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot_mod)
        logger.info("Bot module loaded, starting polling...")
        if hasattr(bot_mod, "main"):
            bot_mod.main()   # blocks here — run_polling() in main thread
    except Exception as e:
        logger.error(f"Bot error: {e}", exc_info=True)
        # Keep alive so API stays up
        import time
        while True:
            time.sleep(60)
