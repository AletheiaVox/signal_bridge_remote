"""
Signal Bridge Remote — OAuth 2.0 Authorization Server

Implements the OAuth flow required by MCP Streamable HTTP transport so
that claude.ai custom connectors can authenticate users without a
pre-shared Bearer token.

Endpoints:
  GET  /.well-known/oauth-authorization-server   RFC 8414 metadata
  POST /oauth/register                           RFC 7591 dynamic client registration
  GET  /oauth/authorize                          Authorization endpoint (login page)
  POST /oauth/authorize                          Login form submission
  POST /oauth/token                              Token endpoint (code + refresh)

Supports PKCE (RFC 7636) with S256 method.
"""
from __future__ import annotations

import asyncio
import hashlib
import base64
import json
import logging
import secrets
import sqlite3
import string
import time
import urllib.parse
from datetime import datetime, timezone

import bcrypt

from . import config
from .auth import verify_user, create_token, verify_token, _get_conn

log = logging.getLogger("signal_bridge.oauth")

# ════════════════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════════════════

AUTH_CODE_EXPIRY_S = 300          # 5 minutes — per OAuth spec recommendation
REFRESH_TOKEN_EXPIRY_S = 86400 * 30  # 30 days


# ════════════════════════════════════════════════════════════════════════
# Database — OAuth tables
# ════════════════════════════════════════════════════════════════════════

def init_oauth_db():
    """Create OAuth tables if they don't exist. Called from app lifespan."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_secret_hash TEXT,
            client_name TEXT NOT NULL,
            redirect_uris TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_codes (
            code TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT,
            code_challenge_method TEXT,
            expires_at REAL NOT NULL,
            used INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
            token TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            expires_at REAL NOT NULL,
            revoked INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    log.info("OAuth database tables ready")


# ════════════════════════════════════════════════════════════════════════
# Client Registration (RFC 7591)
# ════════════════════════════════════════════════════════════════════════

def register_client(client_name: str, redirect_uris: list[str]) -> dict:
    """
    Register a new OAuth client. Returns client_id and client_secret.
    The secret is returned in plaintext exactly once; we store the hash.
    """
    client_id = secrets.token_urlsafe(24)
    client_secret = secrets.token_urlsafe(48)
    client_secret_hash = bcrypt.hashpw(
        client_secret.encode(), bcrypt.gensalt()
    ).decode()

    conn = _get_conn()
    conn.execute(
        "INSERT INTO oauth_clients (client_id, client_secret_hash, client_name, redirect_uris, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            client_id,
            client_secret_hash,
            client_name,
            json.dumps(redirect_uris),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    log.info(f"OAuth client registered: {client_name} ({client_id[:12]}...)")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
    }


def verify_client(client_id: str, client_secret: str | None = None) -> dict | None:
    """
    Look up a client by ID. If client_secret is provided, verify it.
    Returns client dict or None.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
    ).fetchone()
    conn.close()

    if not row:
        return None

    if client_secret is not None:
        if not row["client_secret_hash"]:
            return None
        if not bcrypt.checkpw(client_secret.encode(), row["client_secret_hash"].encode()):
            return None

    return {
        "client_id": row["client_id"],
        "client_name": row["client_name"],
        "redirect_uris": json.loads(row["redirect_uris"]),
    }


# ════════════════════════════════════════════════════════════════════════
# Authorization Codes
# ════════════════════════════════════════════════════════════════════════

def create_auth_code(
    client_id: str,
    user_id: str,
    redirect_uri: str,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
) -> str:
    """Issue a short-lived authorization code."""
    code = secrets.token_urlsafe(48)

    conn = _get_conn()
    conn.execute(
        "INSERT INTO oauth_codes "
        "(code, client_id, user_id, redirect_uri, code_challenge, code_challenge_method, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            code,
            client_id,
            user_id,
            redirect_uri,
            code_challenge,
            code_challenge_method,
            time.time() + AUTH_CODE_EXPIRY_S,
        ),
    )
    conn.commit()
    conn.close()
    return code


def consume_auth_code(code: str, client_id: str, code_verifier: str | None = None) -> dict | None:
    """
    Validate and consume an authorization code. Returns user info or None.
    Each code can only be used once.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM oauth_codes WHERE code = ? AND client_id = ? AND used = 0",
        (code, client_id),
    ).fetchone()

    if not row:
        conn.close()
        return None

    # Check expiry
    if time.time() > row["expires_at"]:
        conn.execute("DELETE FROM oauth_codes WHERE code = ?", (code,))
        conn.commit()
        conn.close()
        return None

    # PKCE verification
    if row["code_challenge"]:
        if not code_verifier:
            conn.close()
            return None

        method = row["code_challenge_method"] or "plain"
        if method == "S256":
            digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
            computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        else:
            computed = code_verifier

        if computed != row["code_challenge"]:
            conn.close()
            return None

    # Mark as used
    conn.execute("UPDATE oauth_codes SET used = 1 WHERE code = ?", (code,))
    conn.commit()
    conn.close()

    return {"user_id": row["user_id"], "redirect_uri": row["redirect_uri"]}


# ════════════════════════════════════════════════════════════════════════
# Refresh Tokens
# ════════════════════════════════════════════════════════════════════════

def create_refresh_token(client_id: str, user_id: str) -> str:
    """Issue a long-lived refresh token."""
    token = secrets.token_urlsafe(64)

    conn = _get_conn()
    conn.execute(
        "INSERT INTO oauth_refresh_tokens (token, client_id, user_id, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (token, client_id, user_id, time.time() + REFRESH_TOKEN_EXPIRY_S),
    )
    conn.commit()
    conn.close()
    return token


def consume_refresh_token(token: str, client_id: str) -> dict | None:
    """
    Validate a refresh token. Revokes the old one (rotation).
    Returns user info or None.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM oauth_refresh_tokens "
        "WHERE token = ? AND client_id = ? AND revoked = 0",
        (token, client_id),
    ).fetchone()

    if not row:
        conn.close()
        return None

    if time.time() > row["expires_at"]:
        conn.execute("DELETE FROM oauth_refresh_tokens WHERE token = ?", (token,))
        conn.commit()
        conn.close()
        return None

    # Revoke old token (rotation)
    conn.execute("UPDATE oauth_refresh_tokens SET revoked = 1 WHERE token = ?", (token,))
    conn.commit()
    conn.close()

    return {"user_id": row["user_id"]}


def cleanup_expired():
    """Purge expired codes and revoked/expired refresh tokens."""
    now = time.time()
    conn = _get_conn()
    conn.execute("DELETE FROM oauth_codes WHERE expires_at < ? OR used = 1", (now,))
    conn.execute(
        "DELETE FROM oauth_refresh_tokens WHERE expires_at < ? OR revoked = 1",
        (now,),
    )
    conn.commit()
    conn.close()


# ════════════════════════════════════════════════════════════════════════
# PKCE Helpers
# ════════════════════════════════════════════════════════════════════════

def _verify_pkce(challenge: str, method: str, verifier: str) -> bool:
    """Verify a PKCE code_verifier against the stored challenge."""
    if method == "S256":
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    else:
        computed = verifier
    return computed == challenge


# ════════════════════════════════════════════════════════════════════════
# HTML Login Page
# ════════════════════════════════════════════════════════════════════════

_LOGIN_PAGE_TEMPLATE = string.Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Bridge — Sign In</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #1a1a2e;
    color: #e0e0e0;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    padding: 1rem;
  }
  .card {
    background: #16213e;
    border: 1px solid #0f3460;
    border-radius: 12px;
    padding: 2rem;
    width: 100%;
    max-width: 380px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  }
  h1 {
    font-size: 1.3rem;
    font-weight: 600;
    margin-bottom: 0.3rem;
    color: #e94560;
  }
  .subtitle {
    font-size: 0.85rem;
    color: #8892a4;
    margin-bottom: 1.5rem;
  }
  .client-name {
    color: #53c0f0;
    font-weight: 500;
  }
  label {
    display: block;
    font-size: 0.8rem;
    color: #8892a4;
    margin-bottom: 0.3rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  input[type="text"], input[type="password"] {
    width: 100%;
    padding: 0.7rem 0.9rem;
    border: 1px solid #0f3460;
    border-radius: 8px;
    background: #1a1a2e;
    color: #e0e0e0;
    font-size: 0.95rem;
    margin-bottom: 1rem;
    outline: none;
    transition: border-color 0.2s;
  }
  input:focus { border-color: #e94560; }
  button {
    width: 100%;
    padding: 0.75rem;
    border: none;
    border-radius: 8px;
    background: #e94560;
    color: #fff;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s;
  }
  button:hover { background: #c73652; }
  .error {
    background: rgba(233,69,96,0.15);
    border: 1px solid #e94560;
    border-radius: 8px;
    padding: 0.7rem 0.9rem;
    margin-bottom: 1rem;
    font-size: 0.85rem;
    color: #e94560;
  }
  .footer {
    text-align: center;
    margin-top: 1.2rem;
    font-size: 0.75rem;
    color: #555;
  }
</style>
</head>
<body>
<div class="card">
  <h1>Signal Bridge</h1>
  <p class="subtitle">
    Sign in to connect
    <span class="client-name">$client_name</span>
  </p>
  $error_html
  <form method="POST" action="/oauth/authorize">
    <label for="username">Username</label>
    <input type="text" id="username" name="username" required autocomplete="username" autofocus>
    <label for="password">Password</label>
    <input type="password" id="password" name="password" required autocomplete="current-password">
    <input type="hidden" name="client_id" value="$client_id">
    <input type="hidden" name="redirect_uri" value="$redirect_uri">
    <input type="hidden" name="state" value="$state">
    <input type="hidden" name="code_challenge" value="$code_challenge">
    <input type="hidden" name="code_challenge_method" value="$code_challenge_method">
    <input type="hidden" name="response_type" value="code">
    <button type="submit">Sign In</button>
  </form>
  <p class="footer">Your credentials are verified by this server only.</p>
</div>
</body>
</html>
""")


def render_login_page(**kwargs) -> str:
    """Render the login page with safe $-substitution (no CSS brace conflicts)."""
    return _LOGIN_PAGE_TEMPLATE.safe_substitute(**kwargs)
