"""
Signal Bridge Remote — Safety Systems

Dead Man's Switch: Monitors phone connections via heartbeat.
If a phone stops responding, all its devices are stopped immediately.

This is non-negotiable safety infrastructure. Hardware must NEVER
be left running unattended after a connection failure.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time

from . import config
from .session_registry import registry

log = logging.getLogger("signal_bridge.safety")


class DeadManSwitch:
    """
    Periodic heartbeat monitor for all active phone sessions.

    Every HEARTBEAT_INTERVAL_S seconds:
      1. Send a heartbeat_ping to each phone
      2. Check if any phones missed their last heartbeat by > HEARTBEAT_TIMEOUT_S
      3. If so, send emergency stop and disconnect

    The phone relay client responds to pings with pongs.
    The session registry tracks last_heartbeat timestamps.
    """

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        """Start the heartbeat monitor loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        log.info(
            f"Dead man's switch active: ping every {config.HEARTBEAT_INTERVAL_S}s, "
            f"timeout after {config.HEARTBEAT_TIMEOUT_S}s"
        )

    async def stop(self):
        """Stop the monitor."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self):
        while self._running:
            try:
                await self._check_all()
            except Exception as e:
                log.error(f"Heartbeat monitor error: {e}")
            await asyncio.sleep(config.HEARTBEAT_INTERVAL_S)

    async def _check_all(self):
        now = time.time()
        sessions = await registry.get_all_sessions()

        for user_id, session in sessions.items():
            # Send ping
            ping = {"type": "heartbeat_ping", "timestamp": now}
            try:
                await session.websocket.send(json.dumps(ping))
            except Exception:
                # Can't even send — connection dead
                log.warning(f"DEAD MAN'S SWITCH: Cannot reach phone for user {user_id}")
                await self._emergency_stop(user_id, session)
                continue

            # Check if last pong is too old
            elapsed = now - session.last_heartbeat
            if elapsed > config.HEARTBEAT_TIMEOUT_S:
                log.warning(
                    f"DEAD MAN'S SWITCH: Phone heartbeat timeout for user {user_id} "
                    f"({elapsed:.1f}s since last pong)"
                )
                await self._emergency_stop(user_id, session)

    async def _emergency_stop(self, user_id: str, session):
        """Send stop-all and disconnect the session."""
        log.critical(f"EMERGENCY STOP for user {user_id} — all devices halted")
        try:
            stop_cmd = {"type": "stop", "device": "all", "emergency": True}
            await session.websocket.send(json.dumps(stop_cmd))
        except Exception:
            pass  # best effort — the phone client also has its own local failsafe

        try:
            await session.websocket.close(1001, "Heartbeat timeout — emergency stop")
        except Exception:
            pass

        await registry.unregister(user_id)


# Singleton
dead_man_switch = DeadManSwitch()
