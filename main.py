"""
WhyNot Agency — FastAPI (daemon thread) + Telegram bot (main thread)
Bot MUST run in main thread because ptb v21 calls signal.signal()
uvloop fix: must set event loop before calling bot.main()
"""
import os, threading, uvicorn, logging, importlib.util, time, asyncio
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
    
    # uvloop (used by uvicorn) does NOT auto-create an event loop.
    # We must explicitly create + set one in the main thread before ptb uses it.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    logger.info("✅ Event loop set in main thread")
    
    # Bot MUST run in main thread — ptb calls signal.signal() which requires main thread
    logger.info("🤖 Loading bot module...")
    try:
        bot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
        spec = importlib.util.spec_from_file_location("bot", bot_path)
        bot_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot_mod)
        logger.info("✅ Bot module loaded, calling main()...")
        if hasattr(bot_mod, "main"):
            bot_mod.main()  # Sync — ptb uses asyncio.run() internally
        else:
            logger.error("❌ bot.py has no main() function!")
    except Exception as e:
        logger.error(f"❌ Bot crashed: {e}", exc_info=True)
    
    # Bot exited — keep process alive so daemon API thread keeps running
    logger.warning("⚠️ Bot stopped — keeping process alive for API")
    while True:
        time.sleep(60)
