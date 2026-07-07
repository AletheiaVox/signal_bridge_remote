"""
Signal Bridge Remote — Authentication & Rate Limiting

JWT-based auth with bcrypt password hashing, SQLite user store,
progressive IP banning, and per-endpoint rate limiting.
"""
from __future__ import annotations
import asyncio
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt

from . import config


# ════════════════════════════════════════════════════════════════════════
# Database
# ════════════════════════════════════════════════════════════════════════

def init_db():
    """Create user and safety_config tables if they don't exist."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS safety_config (
            user_id TEXT PRIMARY KEY REFERENCES users(id),
            governor_enabled INTEGER DEFAULT 1,
            heat_rate REAL DEFAULT NULL,
            cool_rate REAL DEFAULT NULL,
            cooldown_threshold REAL DEFAULT NULL,
            cooldown_exit REAL DEFAULT NULL,
            cooldown_duration REAL DEFAULT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def get_safety_config(user_id: str) -> dict:
    """Get per-user safety config. Returns overrides only (NULLs omitted)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM safety_config WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()

    if not row:
        return {}

    result = {}
    for key in ("governor_enabled", "heat_rate", "cool_rate",
                "cooldown_threshold", "cooldown_exit", "cooldown_duration"):
        if row[key] is not None:
            result[key] = row[key]
    # SQLite has no bool — coerce the int back to bool so the JSON response
    # uses true/false. Strict Kotlin clients refuse to parse 0/1 as Boolean.
    if "governor_enabled" in result:
        result["governor_enabled"] = bool(result["governor_enabled"])
    return result


def set_safety_config(user_id: str, overrides: dict) -> dict:
    """Set per-user safety config overrides. Returns the merged config."""
    allowed_keys = {
        "governor_enabled", "heat_rate", "cool_rate",
        "cooldown_threshold", "cooldown_exit", "cooldown_duration",
    }
    filtered = {k: v for k, v in overrides.items() if k in allowed_keys}
    # Normalise governor_enabled to a real 0/1 int — kotlinx-serialization on
    # the Android side will reject a String "true" / "false" or a bare bool
    # round-tripped through SQLite as anything other than int.
    if "governor_enabled" in filtered:
        filtered["governor_enabled"] = int(bool(filtered["governor_enabled"]))

    conn = _get_conn()
    existing = conn.execute(
        "SELECT user_id FROM safety_config WHERE user_id = ?", (user_id,)
    ).fetchone()

    now = datetime.now(timezone.utc).isoformat()

    if existing:
        # Update existing overrides
        sets = ", ".join(f"{k} = ?" for k in filtered)
        if sets:
            conn.execute(
                f"UPDATE safety_config SET {sets}, updated_at = ? WHERE user_id = ?",
                (*filtered.values(), now, user_id),
            )
    else:
        # Insert new row
        cols = ", ".join(["user_id", "updated_at"] + list(filtered.keys()))
        placeholders = ", ".join(["?"] * (2 + len(filtered)))
        conn.execute(
            f"INSERT INTO safety_config ({cols}) VALUES ({placeholders})",
            (user_id, now, *filtered.values()),
        )

    conn.commit()
    conn.close()

    return get_safety_config(user_id)


def _get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ════════════════════════════════════════════════════════════════════════
# User Management
# ════════════════════════════════════════════════════════════════════════

def create_user(username: str, password: str) -> dict:
    """Register a new user. Returns user dict or raises ValueError."""
    if len(username) < 3 or len(username) > 32:
        raise ValueError("Username must be 3-32 characters")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")

    user_id = str(uuid.uuid4())
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, username.lower().strip(), password_hash,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError("Username already taken")
    finally:
        conn.close()

    return {"user_id": user_id, "username": username.lower().strip()}


def verify_user(username: str, password: str) -> Optional[dict]:
    """Check credentials. Returns user dict or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? AND is_active = 1",
        (username.lower().strip(),),
    ).fetchone()
    conn.close()

    if not row:
        return None
    if not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return None

    return {"user_id": row["id"], "username": row["username"]}


# ════════════════════════════════════════════════════════════════════════
# JWT Tokens
# ════════════════════════════════════════════════════════════════════════

def create_token(user_id: str, username: str) -> str:
    """Issue a JWT token."""
    payload = {
        "sub": user_id,
        "username": username,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=config.TOKEN_EXPIRY_HOURS),
    }
    return jwt.encode(payload, config.SECRET_KEY, algorithm="HS256")


def verify_token(token: str) -> Optional[dict]:
    """Validate a JWT. Returns payload dict or None."""
    try:
        payload = jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])
        return {"user_id": payload["sub"], "username": payload["username"]}
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def extract_token(authorization: str) -> Optional[str]:
    """Pull the token from an Authorization header value."""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


# ════════════════════════════════════════════════════════════════════════
# IP Ban Tracker
# ════════════════════════════════════════════════════════════════════════

class IPBanTracker:
    """
    Tracks failed auth attempts per IP and issues temporary bans
    after threshold is exceeded. Designed to frustrate targeted
    harassment without affecting legitimate users.
    """

    def __init__(self):
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._bans: dict[str, float] = {}  # ip → ban_expires_at timestamp
        self._lock = asyncio.Lock()

    async def record_failure(self, ip: str):
        """Record a failed auth attempt. May trigger a ban."""
        async with self._lock:
            now = time.time()
            window = now - 3600  # 1-hour sliding window
            self._failures[ip] = [t for t in self._failures[ip] if t > window]
            self._failures[ip].append(now)

            if len(self._failures[ip]) >= config.BAN_THRESHOLD:
                self._bans[ip] = now + (config.BAN_DURATION_MINUTES * 60)
                self._failures[ip] = []

    async def is_banned(self, ip: str) -> bool:
        """Check if an IP is currently banned."""
        async with self._lock:
            if ip not in self._bans:
                return False
            if time.time() > self._bans[ip]:
                del self._bans[ip]
                return False
            return True

    async def clear_failures(self, ip: str):
        """Reset failure counter on successful auth."""
        async with self._lock:
            self._failures.pop(ip, None)

    @property
    def banned_count(self) -> int:
        """Number of currently banned IPs."""
        now = time.time()
        return sum(1 for exp in self._bans.values() if exp > now)


# Singleton
ip_tracker = IPBanTracker()


# ════════════════════════════════════════════════════════════════════════
# Rate Limiter (simple token bucket per key)
# ════════════════════════════════════════════════════════════════════════

class RateLimiter:
    """
    Simple in-memory rate limiter using sliding window counters.
    Parse rate strings like "5/minute", "100/hour".
    """

    PERIODS = {
        "second": 1, "minute": 60, "hour": 3600, "day": 86400,
    }

    def __init__(self):
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    @staticmethod
    def _parse_rate(rate_str: str) -> tuple[int, int]:
        """Parse '5/minute' → (5, 60)."""
        count_str, period_str = rate_str.split("/")
        return int(count_str), RateLimiter.PERIODS[period_str]

    async def check(self, key: str, rate_str: str) -> bool:
        """
        Check if request is allowed. Returns True if allowed.
        Automatically records the attempt if allowed.
        """
        max_count, period = self._parse_rate(rate_str)
        async with self._lock:
            now = time.time()
            window_start = now - period
            self._windows[key] = [t for t in self._windows[key] if t > window_start]

            if len(self._windows[key]) >= max_count:
                return False

            self._windows[key].append(now)
            return True


# Singleton
rate_limiter = RateLimiter()
