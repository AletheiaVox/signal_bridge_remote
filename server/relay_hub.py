"""
Signal Bridge Remote — WebSocket Relay Hub (utilities)

IP tracking and rate limiting for phone WebSocket connections.
The actual WebSocket handling lives in app.py using FastAPI's native WebSocket.

This module provides the shared state and helpers used by the app endpoint.
"""
from __future__ import annotations
import asyncio
import logging
from collections import defaultdict

from . import config
from .auth import ip_tracker

log = logging.getLogger("signal_bridge.relay")

# Track WebSocket connections per IP for rate limiting
ws_count_by_ip: dict[str, int] = defaultdict(int)
ws_lock = asyncio.Lock()


async def check_ws_ip_limit(ip: str) -> str | None:
    """
    Check if an IP is allowed to open a new WebSocket connection.
    Returns an error reason string if rejected, None if allowed.
    Automatically increments the counter if allowed.
    """
    if await ip_tracker.is_banned(ip):
        return "Temporarily banned"

    async with ws_lock:
        if ws_count_by_ip[ip] >= config.MAX_WS_PER_IP:
            return "Too many connections from this IP"
        ws_count_by_ip[ip] += 1

    return None


async def release_ws_ip_slot(ip: str):
    """Decrement the connection counter for an IP when a WebSocket disconnects."""
    async with ws_lock:
        ws_count_by_ip[ip] = max(0, ws_count_by_ip[ip] - 1)


def get_ip_from_headers(host: str | None, headers: dict | None = None) -> str:
    """Extract real IP, checking X-Forwarded-For for reverse proxy setups."""
    if headers:
        forwarded = headers.get("X-Forwarded-For", headers.get("x-forwarded-for", ""))
        if forwarded:
            return forwarded.split(",")[0].strip()
    return host or "unknown"
