"""
WhyNot Agency — запускает FastAPI (веб) + Telegram бот в одном процессе
API всегда доступен, бот запускается в фоне
"""
import asyncio, os, threading, uvicorn, logging
from api import app, init_db

logger = logging.getLogger(__name__)

def run_api():
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

async def run_bot_safe():
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("bot", "bot.py")
        bot_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot_mod)
        if hasattr(bot_mod, "main"):
            await bot_mod.main()
    except Exception as e:
        logger.error(f"Bot error: {e}", exc_info=True)

def run_bot_thread():
    asyncio.run(run_bot_safe())

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=run_bot_thread, daemon=True)
    t.start()
    print(f"\u2705 Bot thread started")
    run_api()
