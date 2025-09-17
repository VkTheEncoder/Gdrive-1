from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

# Required
GOOGLE_OAUTH_MODE = os.getenv("GOOGLE_OAUTH_MODE", "device").lower()  # "device" or "web"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8080/oauth/callback")
EDIT_THROTTLE_SECS = float(os.getenv("EDIT_THROTTLE_SECS", "5.0"))
# Optional
BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")  # public URL where FastAPI is served
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DOWNLOAD_DIR = DATA_DIR / "downloads"
DB_PATH = DATA_DIR / "bot.db"

# Web server
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

# Progress & uploads
CHUNK_SIZE = 10 * 1024 * 1024        # 10 MB Google Drive resumable chunk
DL_CHUNK = 1 * 1024 * 1024           # 1 MB for HTTP download
EDIT_THROTTLE_SECS = 5.0             # Telegram message edit throttle

# Create dirs
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
