from __future__ import annotations
import logging
import threading
import asyncio  # needed for sleeping on RetryAfter

# Move dotenv to config.py (preferred). If not, uncomment next two lines and place BEFORE config imports:
# from dotenv import load_dotenv
# load_dotenv()

from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import RetryAfter

from .config import TELEGRAM_BOT_TOKEN, WEB_HOST, WEB_PORT, GOOGLE_OAUTH_MODE
from .db import init_db
from .handlers import start, help_cmd, login, logout, me, setfolder_cmd, handle_document, handle_text, queue_cmd
from .config import EDIT_THROTTLE_SECS

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("gdrive_bot")
log.info("EDIT_THROTTLE_SECS = %s", EDIT_THROTTLE_SECS)


# ---------- error handler ----------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, RetryAfter):
        # Respect Telegram’s backoff; prevents crashes on flood-wait
        await asyncio.sleep(err.retry_after + 1)
        return

    logging.exception("Unhandled error while processing update: %s", update)
    try:
        if hasattr(update, "effective_message") and update.effective_message:
            await update.effective_message.reply_text("⚠️ Something went wrong, but I’m still here.")
    except Exception:
        pass


def run_web():
    import uvicorn
    uvicorn.run("app.web:app", host=WEB_HOST, port=WEB_PORT, log_level="info", reload=False)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing")

    init_db()

    # Start FastAPI only in web OAuth mode
    if GOOGLE_OAUTH_MODE == "web":
        th = threading.Thread(target=run_web, daemon=True)
        th.start()

    # No AIORateLimiter here (no extra install required)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("login", login))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("me", me))
    app.add_handler(CommandHandler("setfolder", setfolder_cmd))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.ANIMATION, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(on_error)

    log.info("Bot started. Web server mode: %s | %s:%s", GOOGLE_OAUTH_MODE, WEB_HOST, WEB_PORT)
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
