"""
WhyNot Agency — FastAPI (daemon thread) + Telegram bot (main thread)
Bot MUST run in main thread because ptb v21 calls signal.signal()
"""
import os, threading, uvicorn, logging, importlib.util, time
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
    
    # Start API in a daemon thread (it stays alive as long as main thread lives)
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    logger.info("✅ API thread started")
    
    # Give API a moment to bind port
    time.sleep(2)
    
    # Bot MUST run in main thread — ptb calls signal.signal() which requires main thread
    logger.info("🤖 Loading bot module...")
    try:
        spec = importlib.util.spec_from_file_location("bot", "bot.py")
        bot_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot_mod)
        logger.info("✅ Bot module loaded, calling main()...")
        if hasattr(bot_mod, "main"):
            bot_mod.main()  # Sync call — bot.py uses asyncio.run() internally
        else:
            logger.error("❌ bot.py has no main() function!")
    except Exception as e:
        logger.error(f"❌ Bot crashed: {e}", exc_info=True)
    
    # Bot exited — keep process alive so daemon API thread keeps running
    logger.warning("⚠️ Bot stopped — keeping process alive for API")
    while True:
        time.sleep(60)
