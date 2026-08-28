"""
WHY NOT? OS entrypoint (Railway `web` process — Procfile: `web: python main.py`).

Layout that keeps python-telegram-bot happy:

  * THIS process / main thread -> the Telegram bot long-polling.
      run_polling() installs SIGINT/SIGTERM handlers and therefore must own the
      real main thread. This process never imports uvicorn, so uvloop is never
      installed as the asyncio policy here — no event-loop conflict for PTB.

  * subprocess -> `python -m uvicorn api:app`.
      uvicorn[standard] installs uvloop process-wide; isolating it in its own
      process keeps that away from the bot. Supervised + restarted by a daemon
      thread below.
"""
import os, sys, time, logging, threading, subprocess

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("entry")

# main.py owns the bot; api.py must NOT also spawn one (double getUpdates -> 409).
os.environ["RUN_BOT"] = "0"

PORT = os.getenv("PORT", "8000")
_stop = threading.Event()
_uvicorn = {"proc": None}


def _uvicorn_supervisor():
    while not _stop.is_set():
        log.info("Starting uvicorn subprocess on 0.0.0.0:%s", PORT)
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api:app",
             "--host", "0.0.0.0", "--port", str(PORT), "--log-level", "info"]
        )
        _uvicorn["proc"] = proc
        proc.wait()
        if _stop.is_set():
            break
        log.warning("uvicorn exited (code %s) — restarting in 3s", proc.returncode)
        time.sleep(3)


def _kill_uvicorn():
    proc = _uvicorn.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    threading.Thread(target=_uvicorn_supervisor, daemon=True).start()
    time.sleep(2)  # let uvicorn bind the port before the bot blocks the thread

    log.info("Starting Telegram bot polling (main thread)")
    import bot
    try:
        bot.main()  # blocks on Application.run_polling()
    finally:
        _stop.set()
        _kill_uvicorn()
        log.info("Shutdown complete")
