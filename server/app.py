"""
Signal Bridge Remote — Main Server Application

Single FastAPI app that serves three roles:
  1. OAuth-style auth (register, login, token refresh)
  2. MCP endpoint (Streamable HTTP — tool calls from Claude)
  3. WebSocket relay hub (persistent phone connections)

Plus rate limiting, IP banning, and the dead man's switch.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import config
from .auth import (
    init_db, create_user, verify_user, create_token, verify_token,
    extract_token, ip_tracker, rate_limiter,
)
from .mcp_tools import TOOLS, HANDLERS, current_user_id
from .relay_hub import check_ws_ip_limit, release_ws_ip_slot, get_ip_from_headers
from .session_registry import registry
from .safety import dead_man_switch

# ── Logging ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("signal_bridge")


# ── Lifespan ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate()
    init_db()
    await dead_man_switch.start()
    log.info(f"Signal Bridge Remote started on {config.HOST}:{config.PORT}")
    log.info(f"Registration {'OPEN' if config.REGISTRATION_OPEN else 'CLOSED'}")
    yield
    await dead_man_switch.stop()
    log.info("Signal Bridge Remote shutting down")


app = FastAPI(
    title="Signal Bridge Remote",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ════════════════════════════════════════════════════════════════════════
# Auth helpers
# ════════════════════════════════════════════════════════════════════════

def _get_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _require_auth(request: Request) -> dict | None:
    """Validate Bearer token. Returns user dict or None."""
    token = extract_token(request.headers.get("Authorization", ""))
    if not token:
        return None
    return verify_token(token)


# ════════════════════════════════════════════════════════════════════════
# Auth Endpoints
# ════════════════════════════════════════════════════════════════════════

@app.post("/auth/register")
async def register(request: Request):
    """Register a new user account."""
    ip = _get_ip(request)

    if await ip_tracker.is_banned(ip):
        return JSONResponse({"error": "Temporarily banned"}, status_code=429)

    if not await rate_limiter.check(f"auth:{ip}", config.RATE_LIMIT_AUTH):
        return JSONResponse({"error": "Too many attempts"}, status_code=429)

    if not config.REGISTRATION_OPEN:
        return JSONResponse({"error": "Registration is closed"}, status_code=403)

    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    try:
        user = await asyncio.to_thread(create_user, username, password)
    except ValueError as e:
        # Don't count validation errors (short username, weak password) toward IP ban.
        # Only actual auth failures (wrong credentials) should inflate the ban counter.
        return JSONResponse({"error": str(e)}, status_code=400)

    token = create_token(user["user_id"], user["username"])
    await ip_tracker.clear_failures(ip)

    return {"user_id": user["user_id"], "username": user["username"], "token": token}


@app.post("/auth/login")
async def login(request: Request):
    """Authenticate and receive a JWT."""
    ip = _get_ip(request)

    if await ip_tracker.is_banned(ip):
        return JSONResponse({"error": "Temporarily banned"}, status_code=429)

    if not await rate_limiter.check(f"auth:{ip}", config.RATE_LIMIT_AUTH):
        return JSONResponse({"error": "Too many attempts"}, status_code=429)

    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")

    user = await asyncio.to_thread(verify_user, username, password)
    if not user:
        await ip_tracker.record_failure(ip)
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    token = create_token(user["user_id"], user["username"])
    await ip_tracker.clear_failures(ip)

    return {"user_id": user["user_id"], "username": user["username"], "token": token}


# ════════════════════════════════════════════════════════════════════════
# MCP Endpoint — Streamable HTTP (JSON-RPC over POST + GET)
#
# Implements the MCP Streamable HTTP transport spec:
#   - POST: JSON-RPC requests from client
#   - GET: SSE stream for server-to-client notifications (kept open)
#   - Mcp-Session-Id header for session tracking
#   - Authless mode for claude.ai connector, Bearer token for Claude Desktop
# ════════════════════════════════════════════════════════════════════════

# In-memory MCP session tracking (maps session_id → user_id)
_mcp_sessions: dict[str, str] = {}


async def _resolve_mcp_user(request: Request) -> dict | None:
    """
    Resolve the user for an MCP request.
    Priority: Bearer token > Mcp-Session-Id lookup > sole active phone session.
    """
    # 1. Try Bearer token auth (Claude Desktop)
    user = await _require_auth(request)
    if user:
        return user

    # 2. Try Mcp-Session-Id (subsequent requests from claude.ai)
    session_id = request.headers.get("mcp-session-id", "")
    if session_id and session_id in _mcp_sessions:
        return {"user_id": _mcp_sessions[session_id]}

    # 3. Fall back to sole active phone session (authless / claude.ai init)
    fallback_user_id = await registry.get_sole_user_id()
    if fallback_user_id:
        log.info(f"MCP request without auth — using active session: {fallback_user_id}")
        return {"user_id": fallback_user_id}

    return None


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """
    MCP Streamable HTTP endpoint (POST).

    Accepts JSON-RPC requests, routes tool calls to the authenticated
    user's phone via the session registry.
    """
    ip = _get_ip(request)

    if await ip_tracker.is_banned(ip):
        return JSONResponse({"error": "Temporarily banned"}, status_code=429)

    if not await rate_limiter.check(f"global:{ip}", config.RATE_LIMIT_GLOBAL):
        return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)

    # Parse JSON-RPC first (we need to check if it's an initialize request)
    try:
        body = await request.json()
    except Exception:
        return _jsonrpc_error(None, -32700, "Parse error: invalid JSON")

    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")

    # Resolve user
    user = await _resolve_mcp_user(request)
    if not user:
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32000, "message": "No auth token and no active phone session"}},
            status_code=401,
        )

    # Rate limit per user for commands
    if not await rate_limiter.check(
        f"cmd:{user['user_id']}", config.RATE_LIMIT_COMMANDS
    ):
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Command rate limit exceeded"}},
            status_code=429,
        )

    # Set user context for tool handlers
    current_user_id.set(user["user_id"])

    # ── Route by method ─────────────────────────────────────────────

    if method == "initialize":
        # Generate a session ID and bind it to this user
        session_id = str(uuid.uuid4())
        _mcp_sessions[session_id] = user["user_id"]
        log.info(f"MCP session created: {session_id[:8]}... for user {user['user_id']}")

        result = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "Signal Bridge Remote", "version": "1.0.0"},
        }
        response = JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})
        response.headers["Mcp-Session-Id"] = session_id
        return response

    elif method == "tools/list":
        return _jsonrpc_result(req_id, {"tools": TOOLS})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})

        handler = HANDLERS.get(tool_name)
        if not handler:
            return _jsonrpc_error(req_id, -32601, f"Unknown tool: {tool_name}")

        try:
            result_text = await handler(**tool_args)
            return _jsonrpc_result(req_id, {
                "content": [{"type": "text", "text": result_text}],
            })
        except Exception as e:
            log.error(f"Tool {tool_name} error: {e}")
            return _jsonrpc_result(req_id, {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            })

    elif method == "ping":
        return _jsonrpc_result(req_id, {})

    elif method == "resources/list":
        return _jsonrpc_result(req_id, {"resources": []})

    elif method == "prompts/list":
        return _jsonrpc_result(req_id, {"prompts": []})

    elif method.startswith("notifications/"):
        # MCP notifications (e.g. notifications/initialized) are fire-and-forget.
        # Return empty success — no error, no noise.
        return _jsonrpc_result(req_id, {})

    else:
        return _jsonrpc_error(req_id, -32601, f"Unknown method: {method}")


@app.get("/mcp")
async def mcp_sse_endpoint(request: Request):
    """
    MCP Streamable HTTP endpoint (GET).

    Opens an SSE stream for server-to-client notifications.
    We don't currently use server-initiated notifications,
    so this just stays open to satisfy the spec.
    """
    from starlette.responses import StreamingResponse

    async def event_stream():
        # Send a keep-alive comment, then hold the connection open
        yield ": connected\n\n"
        try:
            while True:
                await asyncio.sleep(30)
                yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


def _jsonrpc_result(req_id, result):
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def _jsonrpc_error(req_id, code, message):
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    )


# ════════════════════════════════════════════════════════════════════════
# WebSocket Relay — Phone connections
# ════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/phone")
async def websocket_phone(websocket: WebSocket):
    """
    WebSocket endpoint for phone relay clients.
    The phone connects here, authenticates with its JWT,
    and maintains a persistent connection for receiving device commands.
    """
    await websocket.accept()
    await _handle_phone_ws(websocket)


async def _handle_phone_ws(ws: WebSocket):
    """
    Full phone WebSocket lifecycle: auth → register → message loop → cleanup.
    """
    from .models import CommandAck

    ip = get_ip_from_headers(
        ws.client.host if ws.client else None,
        dict(ws.headers) if ws.headers else None,
    )

    # IP-level rate limiting
    rejection = await check_ws_ip_limit(ip)
    if rejection:
        await ws.close(4003, rejection)
        return

    user_id = None
    try:
        # Wait for auth message
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        msg = json.loads(raw)

        if msg.get("type") != "phone_auth" or "token" not in msg:
            await ws.close(4001, "First message must be phone_auth")
            await ip_tracker.record_failure(ip)
            return

        user = verify_token(msg["token"])
        if not user:
            await ws.close(4001, "Invalid token")
            await ip_tracker.record_failure(ip)
            return

        user_id = user["user_id"]
        await ip_tracker.clear_failures(ip)
        await ws.send_json({
            "type": "auth_ok",
            "user_id": user_id,
            "message": "Connected to Signal Bridge relay",
        })
        log.info(f"Phone connected: user={user['username']} ip={ip}")

        # Create a wrapper that looks like a websockets ServerConnection
        wrapper = _FastAPIWSWrapper(ws)
        session = await registry.register(user_id, wrapper)

        # Request device list (phone also sends proactively, but this is a backup)
        log.info(f"Requesting device scan from phone: user={user_id}")
        await ws.send_json({"type": "scan"})

        # Message loop
        while True:
            try:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "heartbeat_pong":
                    await registry.update_heartbeat(user_id)
                elif msg_type == "command_ack":
                    ack = CommandAck(
                        success=msg.get("success", True),
                        message=msg.get("message", ""),
                        request_id=msg.get("request_id"),
                        data=msg.get("data"),
                    )
                    if ack.request_id:
                        session.resolve_ack(ack.request_id, ack)
                elif msg_type == "device_list":
                    await registry.update_devices(user_id, msg.get("devices", []))
                    log.info(f"Devices updated: user={user_id}, count={len(msg.get('devices', []))}")

            except WebSocketDisconnect:
                break
            except json.JSONDecodeError:
                continue

    except asyncio.TimeoutError:
        await ws.close(4001, "Auth timeout")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(f"Phone WS error: {e}")
    finally:
        if user_id:
            await registry.unregister(user_id)
            log.info(f"Phone disconnected: user={user_id}")
        await release_ws_ip_slot(ip)


class _FastAPIWSWrapper:
    """
    Minimal wrapper to make a FastAPI WebSocket look enough like a
    websockets ServerConnection for the session registry and safety module.
    """
    def __init__(self, ws: WebSocket):
        self._ws = ws

    async def send(self, data: str):
        await self._ws.send_text(data)

    async def close(self, code: int = 1000, reason: str = ""):
        await self._ws.close(code, reason)

    @property
    def transport(self):
        return self  # duck typing for _get_ip fallback

    def get_extra_info(self, key):
        if key == "peername" and self._ws.client:
            return (self._ws.client.host, self._ws.client.port)
        return None


# ════════════════════════════════════════════════════════════════════════
# Health & Status
# ════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "active_phones": registry.active_count,
        "banned_ips": ip_tracker.banned_count,
    }


# ════════════════════════════════════════════════════════════════════════
# Init module
# ════════════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {
        "service": "Signal Bridge Remote",
        "version": "1.0.0",
        "endpoints": {
            "auth": "/auth/register, /auth/login",
            "mcp": "/mcp (POST, JSON-RPC)",
            "phone_relay": "/ws/phone (WebSocket)",
            "health": "/health",
        },
    }
