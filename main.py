"""
WhyNot Agency — запускает FastAPI (веб) + Telegram бот в одном процессе
"""
import asyncio, os, threading, uvicorn
from api import app, init_db

def run_api():
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

async def run_bot():
    import importlib.util, sys
    # Запускаем bot.py как модуль
    spec = importlib.util.spec_from_file_location("bot", "bot.py")
    bot_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bot_mod)
    if hasattr(bot_mod, "main"):
        await bot_mod.main()

if __name__ == "__main__":
    init_db()
    # API в отдельном потоке
    t = threading.Thread(target=run_api, daemon=True)
    t.start()
    print(f"✅ API started on port {os.getenv('PORT', 8000)}")
    # Бот в главном asyncio loop
    asyncio.run(run_bot())
