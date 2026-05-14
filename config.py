"""
config.py — Centralized configuration loaded from environment variables.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ─── Telegram API ────────────────────────────────────────────────────────────
API_ID: int = int(os.getenv("API_ID", "0"))
API_HASH: str = os.getenv("API_HASH", "")

# ─── Device Identity (Spoofing Profile) ──────────────────────────────────────
# Emulates: Dell Latitude E4300 / Windows 10 / Telegram Desktop 5.1.4 x64
DEVICE_MODEL: str = "Dell Latitude E4300"
SYSTEM_VERSION: str = "Windows 10"
APP_VERSION: str = "5.1.4 x64"
LANG_CODE: str = "en"
SYSTEM_LANG_CODE: str = "en-US"

# ─── 2FA ─────────────────────────────────────────────────────────────────────
DEFAULT_2FA_PASSWORD: str = os.getenv("DEFAULT_2FA_PASSWORD", "Change_Me_123!")

# ─── Storage ─────────────────────────────────────────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "./data/sessions.db")
SESSIONS_DIR: str = os.getenv("SESSIONS_DIR", "./data/sessions")

# Ensure directories exist
Path(SESSIONS_DIR).mkdir(parents=True, exist_ok=True)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

# ─── Redis ───────────────────────────────────────────────────────────────────
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ─── Webhook / Forwarding ───────────────────────────────────────────────────
WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
FORWARD_BOT_TOKEN: str = os.getenv("FORWARD_BOT_TOKEN", "")
FORWARD_CHAT_ID: str = os.getenv("FORWARD_CHAT_ID", "")

# ─── Server ──────────────────────────────────────────────────────────────────
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))

# ─── Retry / Rate-Limit ─────────────────────────────────────────────────────
MAX_FLOOD_RETRIES: int = 3
CODE_EXPIRY_SECONDS: int = 300  # 5 minutes TTL in Redis for pending codes
