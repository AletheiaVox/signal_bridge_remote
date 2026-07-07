"""
Signal Bridge Remote — OAuth 2.0 Route Handlers

FastAPI routes for the OAuth authorization server.
Mounted in app.py via include_router().
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import config
from .auth import verify_user, create_token, ip_tracker, rate_limiter
from .oauth import (
    init_oauth_db,
    register_client,
    verify_client,
    create_auth_code,
    consume_auth_code,
    create_refresh_token,
    consume_refresh_token,
    cleanup_expired,
    render_login_page,
)

log = logging.getLogger("signal_bridge.oauth")

router = APIRouter()


# ════════════════════════════════════════════════════════════════════════
# Helper
# ════════════════════════════════════════════════════════════════════════

def _get_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _base_url(request: Request) -> str:
    """Derive the external base URL from the request."""
    # Respect X-Forwarded-Proto / X-Forwarded-Host if behind a reverse proxy
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.headers.get("Host", request.url.netloc))
    return f"{proto}://{host}"


# ════════════════════════════════════════════════════════════════════════
# RFC 8414 — OAuth Authorization Server Metadata
# ════════════════════════════════════════════════════════════════════════

@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    """
    Discovery endpoint. MCP clients fetch this to learn where to
    authorize, exchange tokens, and register.
    """
    base = _base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "scopes_supported": ["signal_bridge"],
    })


# ════════════════════════════════════════════════════════════════════════
# RFC 7591 — Dynamic Client Registration
# ════════════════════════════════════════════════════════════════════════

@router.post("/oauth/register")
async def oauth_register_client(request: Request):
    """
    Dynamic client registration. MCP clients call this once to obtain
    a client_id and client_secret before starting the auth flow.
    """
    ip = _get_ip(request)

    if await ip_tracker.is_banned(ip):
        return JSONResponse({"error": "temporarily_banned"}, status_code=429)

    if not await rate_limiter.check(f"oauth_reg:{ip}", "10/hour"):
        return JSONResponse({"error": "rate_limit_exceeded"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    client_name = body.get("client_name", "Unknown MCP Client")
    redirect_uris = body.get("redirect_uris", [])

    if not redirect_uris or not isinstance(redirect_uris, list):
        return JSONResponse(
            {"error": "invalid_client_metadata",
             "error_description": "redirect_uris is required and must be a non-empty array"},
            status_code=400,
        )

    # Validate redirect URIs (must be valid URLs)
    for uri in redirect_uris:
        parsed = urllib.parse.urlparse(uri)
        if not parsed.scheme or not parsed.netloc:
            # Allow localhost without scheme validation for dev
            if "localhost" not in uri and "127.0.0.1" not in uri:
                return JSONResponse(
                    {"error": "invalid_redirect_uri",
                     "error_description": f"Invalid redirect_uri: {uri}"},
                    status_code=400,
                )

    result = await asyncio.to_thread(register_client, client_name, redirect_uris)

    # RFC 7591 response format
    return JSONResponse({
        "client_id": result["client_id"],
        "client_secret": result["client_secret"],
        "client_name": result["client_name"],
        "redirect_uris": result["redirect_uris"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }, status_code=201)


# ════════════════════════════════════════════════════════════════════════
# Authorization Endpoint
# ════════════════════════════════════════════════════════════════════════

@router.get("/oauth/authorize")
async def oauth_authorize_get(request: Request):
    """
    Authorization endpoint (GET). Renders the login page.
    Query params: client_id, redirect_uri, response_type, state,
                  code_challenge, code_challenge_method
    """
    params = request.query_params
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    response_type = params.get("response_type", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "")

    # Validate
    if response_type != "code":
        return _authorize_error(
            redirect_uri, state, "unsupported_response_type",
            "Only response_type=code is supported"
        )

    if not client_id:
        return HTMLResponse(
            "<h1>Error</h1><p>Missing client_id parameter</p>", status_code=400
        )

    client = await asyncio.to_thread(verify_client, client_id)
    if not client:
        return HTMLResponse(
            "<h1>Error</h1><p>Unknown client_id</p>", status_code=400
        )

    # Verify redirect_uri is registered
    if redirect_uri and redirect_uri not in client["redirect_uris"]:
        return HTMLResponse(
            "<h1>Error</h1><p>redirect_uri not registered for this client</p>",
            status_code=400,
        )

    # Use first registered URI if none specified
    if not redirect_uri:
        redirect_uri = client["redirect_uris"][0]

    # Render login page
    html = render_login_page(
        client_name=_escape_html(client["client_name"]),
        client_id=_escape_html(client_id),
        redirect_uri=_escape_html(redirect_uri),
        state=_escape_html(state),
        code_challenge=_escape_html(code_challenge),
        code_challenge_method=_escape_html(code_challenge_method),
        error_html="",
    )
    return HTMLResponse(html)


@router.post("/oauth/authorize")
async def oauth_authorize_post(request: Request):
    """
    Authorization endpoint (POST). Handles login form submission.
    On success, redirects to redirect_uri with authorization code.
    """
    ip = _get_ip(request)

    if await ip_tracker.is_banned(ip):
        return HTMLResponse("<h1>Temporarily banned</h1>", status_code=429)

    if not await rate_limiter.check(f"auth:{ip}", config.RATE_LIMIT_AUTH):
        return HTMLResponse("<h1>Too many attempts</h1>", status_code=429)

    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    state = form.get("state", "")
    code_challenge = form.get("code_challenge", "")
    code_challenge_method = form.get("code_challenge_method", "")

    # Verify client
    client = await asyncio.to_thread(verify_client, client_id)
    if not client:
        return HTMLResponse("<h1>Error</h1><p>Invalid client</p>", status_code=400)

    if redirect_uri and redirect_uri not in client["redirect_uris"]:
        return HTMLResponse(
            "<h1>Error</h1><p>Invalid redirect_uri</p>", status_code=400
        )
    if not redirect_uri:
        redirect_uri = client["redirect_uris"][0]

    # Verify credentials
    user = await asyncio.to_thread(verify_user, username, password)
    if not user:
        await ip_tracker.record_failure(ip)
        html = render_login_page(
            client_name=_escape_html(client["client_name"]),
            client_id=_escape_html(client_id),
            redirect_uri=_escape_html(redirect_uri),
            state=_escape_html(state),
            code_challenge=_escape_html(code_challenge),
            code_challenge_method=_escape_html(code_challenge_method),
            error_html='<div class="error">Invalid username or password.</div>',
        )
        return HTMLResponse(html)

    await ip_tracker.clear_failures(ip)

    # Issue authorization code
    code = await asyncio.to_thread(
        create_auth_code,
        client_id,
        user["user_id"],
        redirect_uri,
        code_challenge or None,
        code_challenge_method or None,
    )

    # Redirect back to client
    params = {"code": code}
    if state:
        params["state"] = state

    separator = "&" if "?" in redirect_uri else "?"
    target = redirect_uri + separator + urllib.parse.urlencode(params)

    log.info(f"OAuth code issued for user={user['username']} client={client_id[:12]}...")
    return RedirectResponse(target, status_code=302)


# ════════════════════════════════════════════════════════════════════════
# Token Endpoint
# ════════════════════════════════════════════════════════════════════════

@router.post("/oauth/token")
async def oauth_token(request: Request):
    """
    Token endpoint. Supports:
      - grant_type=authorization_code  (exchange code for tokens)
      - grant_type=refresh_token       (rotate refresh token)

    Client authenticates via client_secret_post (credentials in body).
    """
    ip = _get_ip(request)

    if await ip_tracker.is_banned(ip):
        return JSONResponse({"error": "temporarily_banned"}, status_code=429)

    if not await rate_limiter.check(f"oauth_token:{ip}", "30/minute"):
        return JSONResponse({"error": "rate_limit_exceeded"}, status_code=429)

    # Accept both form-encoded and JSON
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        body = dict(form)
    else:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

    grant_type = body.get("grant_type", "")
    client_id = body.get("client_id", "")
    client_secret = body.get("client_secret", "")

    # Verify client credentials
    client = await asyncio.to_thread(verify_client, client_id, client_secret)
    if not client:
        await ip_tracker.record_failure(ip)
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if grant_type == "authorization_code":
        return await _handle_authorization_code(body, client_id, ip)
    elif grant_type == "refresh_token":
        return await _handle_refresh_token(body, client_id, ip)
    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


async def _handle_authorization_code(body: dict, client_id: str, ip: str):
    """Exchange an authorization code for access + refresh tokens."""
    code = body.get("code", "")
    code_verifier = body.get("code_verifier")

    if not code:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Missing code"},
            status_code=400,
        )

    result = await asyncio.to_thread(consume_auth_code, code, client_id, code_verifier)
    if not result:
        await ip_tracker.record_failure(ip)
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    # Look up user to get username for token
    from .auth import _get_conn as get_conn
    conn = get_conn()
    user_row = conn.execute(
        "SELECT username FROM users WHERE id = ?", (result["user_id"],)
    ).fetchone()
    conn.close()

    if not user_row:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    # Issue tokens — reuse the existing JWT infrastructure
    access_token = create_token(result["user_id"], user_row["username"])
    refresh_token = await asyncio.to_thread(
        create_refresh_token, client_id, result["user_id"]
    )

    log.info(f"OAuth tokens issued for user={result['user_id']} via auth code")

    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": config.TOKEN_EXPIRY_HOURS * 3600,
        "refresh_token": refresh_token,
    })


async def _handle_refresh_token(body: dict, client_id: str, ip: str):
    """Exchange a refresh token for a new access + refresh token pair."""
    token = body.get("refresh_token", "")
    if not token:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Missing refresh_token"},
            status_code=400,
        )

    result = await asyncio.to_thread(consume_refresh_token, token, client_id)
    if not result:
        await ip_tracker.record_failure(ip)
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    # Look up user
    from .auth import _get_conn as get_conn
    conn = get_conn()
    user_row = conn.execute(
        "SELECT username FROM users WHERE id = ?", (result["user_id"],)
    ).fetchone()
    conn.close()

    if not user_row:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    access_token = create_token(result["user_id"], user_row["username"])
    new_refresh = await asyncio.to_thread(
        create_refresh_token, client_id, result["user_id"]
    )

    log.info(f"OAuth tokens refreshed for user={result['user_id']}")

    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": config.TOKEN_EXPIRY_HOURS * 3600,
        "refresh_token": new_refresh,
    })


# ════════════════════════════════════════════════════════════════════════
# Error Helpers
# ════════════════════════════════════════════════════════════════════════

def _authorize_error(redirect_uri: str, state: str, error: str, description: str):
    """Redirect back with an OAuth error if we have a valid redirect_uri."""
    if not redirect_uri:
        return HTMLResponse(
            f"<h1>Error</h1><p>{description}</p>", status_code=400
        )
    params = {"error": error, "error_description": description}
    if state:
        params["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    target = redirect_uri + separator + urllib.parse.urlencode(params)
    return RedirectResponse(target, status_code=302)


def _escape_html(s: str) -> str:
    """Minimal HTML escaping for template interpolation."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
