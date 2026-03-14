"""
Signal Bridge Remote — Server Configuration

All settings are loaded from environment variables with sensible defaults.
In production, set SB_SECRET_KEY to a random 64-char string.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # Load .env file before reading any env vars

# ── Server ──────────────────────────────────────────────────────────────
HOST = os.getenv("SB_HOST", "0.0.0.0")
PORT = int(os.getenv("SB_PORT", "8420"))
SECRET_KEY = os.getenv("SB_SECRET_KEY", "")  # MUST be set in production
CORS_ORIGINS = os.getenv("SB_CORS_ORIGINS", "*").split(",")

# ── Auth ────────────────────────────────────────────────────────────────
TOKEN_EXPIRY_HOURS = int(os.getenv("SB_TOKEN_EXPIRY_HOURS", "168"))  # 1 week
REGISTRATION_OPEN = os.getenv("SB_REGISTRATION_OPEN", "true").lower() == "true"

# ── Rate Limiting ───────────────────────────────────────────────────────
# Format: "count/period" — e.g. "5/minute", "100/hour"
RATE_LIMIT_AUTH = os.getenv("SB_RATE_LIMIT_AUTH", "5/minute")
RATE_LIMIT_COMMANDS = os.getenv("SB_RATE_LIMIT_COMMANDS", "120/minute")
RATE_LIMIT_GLOBAL = os.getenv("SB_RATE_LIMIT_GLOBAL", "300/minute")
MAX_WS_PER_IP = int(os.getenv("SB_MAX_WS_PER_IP", "3"))
BAN_THRESHOLD = int(os.getenv("SB_BAN_THRESHOLD", "20"))
BAN_DURATION_MINUTES = int(os.getenv("SB_BAN_DURATION_MINUTES", "30"))

# ── Safety ──────────────────────────────────────────────────────────────
HEARTBEAT_INTERVAL_S = float(os.getenv("SB_HEARTBEAT_INTERVAL", "2.0"))
HEARTBEAT_TIMEOUT_S = float(os.getenv("SB_HEARTBEAT_TIMEOUT", "6.0"))

# ── Database ────────────────────────────────────────────────────────────
DB_PATH = os.getenv("SB_DB_PATH", str(Path(__file__).parent / "signal_bridge.db"))


def validate():
    """Check that critical config is set. Call on startup."""
    if not SECRET_KEY:
        raise RuntimeError(
            "SB_SECRET_KEY is not set. Generate one with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )
