#!/usr/bin/env python3
"""
Signal Bridge Remote — Phone Relay Client

Runs alongside Intiface Central on the device host (phone or desktop).
Maintains two connections:
  1. Outbound WebSocket to the VPS relay server
  2. Local WebSocket to Intiface Central (Buttplug protocol)

Receives commands from the server and executes them locally through Intiface.
Includes local dead man's switch: if the server connection drops,
all devices are immediately stopped.

Usage:
  python relay_client.py --server wss://your-server.com/ws/phone --token YOUR_JWT

For testing (desktop with Intiface running locally):
  python relay_client.py --server ws://localhost:8420/ws/phone --token YOUR_JWT
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Buttplug imports — verified against buttplug-py v1.0.0
try:
    from buttplug import (
        ButtplugClient,
        ButtplugDevice,
        DeviceOutputCommand,
        OutputType,
    )
except ImportError:
    print("ERROR: buttplug package not installed. Run: pip install buttplug")
    sys.exit(1)

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("signal_bridge.phone")


# ════════════════════════════════════════════════════════════════════════
# Device Profile System
# ════════════════════════════════════════════════════════════════════════

@dataclass
class DeviceProfile:
    short_name: str
    match_strings: list[str]
    capabilities: dict[str, str] = field(default_factory=dict)
    intensity_floor: float = 0.0
    notes: str = ""


@dataclass
class ConnectedDevice:
    buttplug_id: int
    buttplug_device: ButtplugDevice
    profile: DeviceProfile
    available_outputs: list[str] = field(default_factory=list)


def load_profiles(path: str = None) -> list[DeviceProfile]:
    """Load device profiles from devices.json."""
    if path is None:
        path = str(Path(__file__).parent / "devices.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return [DeviceProfile(**d) for d in data]
    except FileNotFoundError:
        log.warning(f"No devices.json found at {path}, using empty profiles")
        return []


# ════════════════════════════════════════════════════════════════════════
# Device Controller — Local Buttplug Integration
# ════════════════════════════════════════════════════════════════════════

# Map string output types to Buttplug OutputType enum.
# Verified against buttplug-py v1.0.0 OutputType members:
#   VIBRATE, ROTATE, OSCILLATE, CONSTRICT, SPRAY,
#   TEMPERATURE, LED, POSITION, POSITION_WITH_DURATION
OUTPUT_TYPE_MAP: dict[str, OutputType] = {}
for _name, _member in OutputType.__members__.items():
    OUTPUT_TYPE_MAP[_name.lower()] = _member
# Also add a friendly alias
OUTPUT_TYPE_MAP["position_with_duration"] = OutputType.POSITION_WITH_DURATION


class DeviceController:
    """Manages local Buttplug connection and device control."""

    def __init__(self, intiface_url: str = "ws://127.0.0.1:12345", profiles: list[DeviceProfile] = None):
        self.intiface_url = intiface_url
        self.profiles = profiles or []
        self.client: Optional[ButtplugClient] = None
        self.devices: dict[str, ConnectedDevice] = {}  # short_name → device
        self._pattern_tasks: dict[str, asyncio.Task] = {}
        self._connected = False

    async def connect(self):
        """Connect to local Intiface Central."""
        self.client = ButtplugClient("Signal Bridge Phone")
        try:
            await self.client.connect(self.intiface_url)
            self._connected = True
            log.info(f"Connected to Intiface at {self.intiface_url}")
            await self.scan()
        except Exception as e:
            log.error(f"Failed to connect to Intiface: {e}")
            raise

    async def disconnect(self):
        """Disconnect from Intiface."""
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self._connected = False

    async def scan(self):
        """Scan for devices and register them."""
        if not self.client:
            return

        await self.client.start_scanning()
        await asyncio.sleep(3)  # give devices time to be found
        try:
            await self.client.stop_scanning()
        except Exception:
            pass

        self.devices = {}
        for dev in self.client.devices.values():
            profile = self._match_profile(dev.name)
            available = self._detect_outputs(dev)
            cd = ConnectedDevice(
                buttplug_id=dev.index,
                buttplug_device=dev,
                profile=profile,
                available_outputs=available,
            )
            self.devices[profile.short_name] = cd
            log.info(
                f"Found device: {profile.short_name} ({dev.name}) "
                f"outputs={available}"
            )

    def _match_profile(self, device_name: str) -> DeviceProfile:
        """Match a Buttplug device name to a known profile."""
        for p in self.profiles:
            for match_str in p.match_strings:
                if match_str.lower() in device_name.lower():
                    return p
        # Generic profile
        short = device_name.split()[0].lower()[:12]
        return DeviceProfile(
            short_name=short,
            match_strings=[device_name],
            capabilities={"vibrate": "unknown"},
            notes=f"Auto-detected: {device_name}",
        )

    def _detect_outputs(self, dev: ButtplugDevice) -> list[str]:
        """Detect which output types a device supports."""
        outputs = []
        for name, otype in OUTPUT_TYPE_MAP.items():
            try:
                if dev.has_output(otype):
                    outputs.append(name)
            except Exception:
                pass
        return outputs or ["vibrate"]  # fallback

    def get_device_list(self) -> list[dict[str, Any]]:
        """Get device info for reporting to the server."""
        result = []
        for name, cd in self.devices.items():
            result.append({
                "short_name": cd.profile.short_name,
                "device_name": cd.buttplug_device.name,
                "capabilities": cd.profile.capabilities,
                "available_outputs": cd.available_outputs,
                "intensity_floor": cd.profile.intensity_floor,
                "notes": cd.profile.notes,
            })
        return result

    # ── Command Execution ───────────────────────────────────────────

    async def execute_command(self, cmd: dict) -> dict:
        """Execute a command from the server. Returns ack dict."""
        cmd_type = cmd.get("type")
        request_id = cmd.get("request_id")

        try:
            if cmd_type == "command":
                return await self._handle_output(cmd, request_id)
            elif cmd_type == "pattern":
                return await self._handle_pattern(cmd, request_id)
            elif cmd_type == "stop":
                return await self._handle_stop(cmd, request_id)
            elif cmd_type == "scan":
                await self.scan()
                return self._ack(True, "Scan complete", request_id)
            elif cmd_type == "read_sensor":
                return await self._handle_sensor(cmd, request_id)
            else:
                return self._ack(False, f"Unknown command type: {cmd_type}", request_id)
        except Exception as e:
            log.error(f"Command execution error: {e}")
            return self._ack(False, str(e), request_id)

    async def _handle_output(self, cmd: dict, request_id: str) -> dict:
        """Handle direct output command (vibrate, rotate, etc.)."""
        action = cmd.get("action", "vibrate")
        device_name = cmd.get("device", "all")
        intensity = cmd.get("intensity", 0.5)
        duration = cmd.get("duration", 0)

        targets = self._resolve_targets(device_name)
        if not targets:
            return self._ack(False, f"No device found: {device_name}", request_id)

        otype = OUTPUT_TYPE_MAP.get(action)
        if not otype:
            return self._ack(False, f"Unsupported output type: {action}", request_id)

        for cd in targets:
            adj_intensity = self._apply_floor(intensity, cd.profile.intensity_floor)
            try:
                await cd.buttplug_device.run_output(
                    DeviceOutputCommand(otype, adj_intensity)
                )
            except Exception as e:
                return self._ack(False, f"Device error ({cd.profile.short_name}): {e}", request_id)

            # Auto-stop after duration
            if duration > 0:
                asyncio.create_task(self._timed_stop(cd, otype, duration))

        names = ", ".join(cd.profile.short_name for cd in targets)
        return self._ack(
            True,
            f"{action} at {intensity:.0%} on {names}"
            + (f" for {duration}s" if duration > 0 else ""),
            request_id,
        )

    async def _handle_pattern(self, cmd: dict, request_id: str) -> dict:
        """Handle pattern command (pulse, wave, escalate)."""
        pattern = cmd.get("pattern")
        output_type = cmd.get("output_type", "vibrate")
        device_name = cmd.get("device", "all")
        intensity = cmd.get("intensity", 0.6)
        duration = cmd.get("duration", 10)

        targets = self._resolve_targets(device_name)
        if not targets:
            return self._ack(False, f"No device found: {device_name}", request_id)

        otype = OUTPUT_TYPE_MAP.get(output_type)
        if not otype:
            return self._ack(False, f"Unsupported output type: {output_type}", request_id)

        for cd in targets:
            task_key = f"{cd.profile.short_name}:{pattern}"
            # Cancel existing pattern on this device
            if task_key in self._pattern_tasks:
                self._pattern_tasks[task_key].cancel()

            if pattern == "pulse":
                task = asyncio.create_task(
                    self._run_pulse(cd, otype, intensity, duration)
                )
            elif pattern == "wave":
                task = asyncio.create_task(
                    self._run_wave(cd, otype, intensity, duration)
                )
            elif pattern == "escalate":
                hold_seconds = cmd.get("hold_seconds", 0)
                task = asyncio.create_task(
                    self._run_escalate(cd, otype, intensity, duration, hold_seconds)
                )
            else:
                return self._ack(False, f"Unknown pattern: {pattern}", request_id)

            self._pattern_tasks[task_key] = task

        names = ", ".join(cd.profile.short_name for cd in targets)
        return self._ack(True, f"{pattern} ({output_type}) on {names} for {duration}s", request_id)

    async def _handle_stop(self, cmd: dict, request_id: str) -> dict:
        """Stop all outputs and cancel patterns."""
        device_name = cmd.get("device", "all")
        targets = self._resolve_targets(device_name)

        # If a specific device was requested but not found, stop ALL as safety fallback
        # but tell Claude what happened so it can correct the device name
        fallback_stop = False
        if device_name != "all" and not targets:
            fallback_stop = True
            targets = list(self.devices.values())

        # Cancel relevant pattern tasks
        for key, task in list(self._pattern_tasks.items()):
            if device_name == "all" or fallback_stop or any(
                cd.profile.short_name in key for cd in targets
            ):
                task.cancel()
                del self._pattern_tasks[key]

        for cd in targets:
            try:
                await cd.buttplug_device.stop()
            except Exception:
                pass

        if fallback_stop:
            available = ", ".join(self.devices.keys()) or "none"
            return self._ack(
                True,
                f"Device '{device_name}' not found — stopped ALL devices as safety fallback. "
                f"Available devices: {available}",
                request_id,
            )

        names = ", ".join(cd.profile.short_name for cd in targets) if targets else "all"
        return self._ack(True, f"Stopped: {names}", request_id)

    async def _handle_sensor(self, cmd: dict, request_id: str) -> dict:
        """Read sensor data from a device."""
        sensor = cmd.get("sensor", "battery")
        device_name = cmd.get("device")

        targets = self._resolve_targets(device_name)
        if not targets:
            return self._ack(False, f"No device found: {device_name}", request_id)

        cd = targets[0]
        dev = cd.buttplug_device
        try:
            if sensor == "battery":
                if not dev.has_battery():
                    return self._ack(False, f"{cd.profile.short_name} has no battery sensor", request_id)
                level = await dev.battery()
                return self._ack(
                    True,
                    f"{cd.profile.short_name} battery: {level:.0%}",
                    request_id,
                    data={"battery": level},
                )
            elif sensor == "rssi":
                if not dev.has_rssi():
                    return self._ack(False, f"{cd.profile.short_name} has no RSSI sensor", request_id)
                rssi = await dev.rssi()
                return self._ack(
                    True,
                    f"{cd.profile.short_name} RSSI: {rssi}",
                    request_id,
                    data={"rssi": rssi},
                )
            else:
                return self._ack(
                    False,
                    f"Sensor '{sensor}' read not supported in buttplug-py v1.0. "
                    f"Available: battery, rssi",
                    request_id,
                )
        except Exception as e:
            return self._ack(False, f"Sensor read error: {e}", request_id)

    # ── Pattern Runners ─────────────────────────────────────────────

    async def _run_pulse(self, cd: ConnectedDevice, otype, intensity: float, duration: float):
        try:
            start = time.time()
            floor = cd.profile.intensity_floor
            adj = self._apply_floor(intensity, floor)
            while time.time() - start < duration:
                await cd.buttplug_device.run_output(DeviceOutputCommand(otype, adj))
                await asyncio.sleep(0.5)
                await cd.buttplug_device.run_output(DeviceOutputCommand(otype, 0))
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await cd.buttplug_device.stop()
            except Exception:
                pass

    async def _run_wave(self, cd: ConnectedDevice, otype, intensity: float, duration: float):
        try:
            start = time.time()
            floor = cd.profile.intensity_floor
            while time.time() - start < duration:
                elapsed = time.time() - start
                # Raw sine: 0.0 to 1.0
                raw = (math.sin(elapsed * 2.0) + 1.0) / 2.0 * intensity
                # Map smoothly above the floor: floor..intensity (never drops below floor)
                # Only true zero if raw is actually zero (which it never quite is with sine)
                if raw <= 0.01:
                    adj = 0
                elif floor > 0:
                    adj = floor + raw * (1.0 - floor)
                    adj = min(1.0, adj)
                else:
                    adj = min(1.0, raw)
                await cd.buttplug_device.run_output(DeviceOutputCommand(otype, adj))
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await cd.buttplug_device.stop()
            except Exception:
                pass

    async def _run_escalate(self, cd: ConnectedDevice, otype, peak: float, duration: float, hold_seconds: float = 0):
        try:
            steps = 20
            floor = cd.profile.intensity_floor
            for i in range(steps + 1):
                val = (i / steps) * peak
                if val <= 0.01:
                    adj = 0
                elif floor > 0:
                    adj = floor + val * (1.0 - floor)
                    adj = min(1.0, adj)
                else:
                    adj = self._apply_floor(val, floor)
                await cd.buttplug_device.run_output(DeviceOutputCommand(otype, adj))
                await asyncio.sleep(duration / steps)
            # At peak now. hold_seconds: 0 = hold indefinitely, >0 = hold then stop
            if hold_seconds > 0:
                await asyncio.sleep(hold_seconds)
                await cd.buttplug_device.stop()
            # else: stay at peak until explicit stop command
        except asyncio.CancelledError:
            try:
                await cd.buttplug_device.stop()
            except Exception:
                pass

    # ── Helpers ──────────────────────────────────────────────────────

    async def _timed_stop(self, cd: ConnectedDevice, otype, duration: float):
        await asyncio.sleep(duration)
        try:
            await cd.buttplug_device.run_output(DeviceOutputCommand(otype, 0))
        except Exception:
            pass

    def _resolve_targets(self, device_name: str) -> list[ConnectedDevice]:
        if device_name == "all":
            return list(self.devices.values())
        cd = self.devices.get(device_name)
        return [cd] if cd else []

    @staticmethod
    def _apply_floor(intensity: float, floor: float) -> float:
        if intensity <= 0:
            return 0
        if floor <= 0:
            return min(1.0, intensity)
        return max(floor, min(1.0, intensity))

    def _ack(self, success: bool, message: str, request_id: str = None, data: dict = None) -> dict:
        result = {
            "type": "command_ack",
            "success": success,
            "message": message,
        }
        if request_id:
            result["request_id"] = request_id
        if data:
            result["data"] = data
        return result

    async def emergency_stop(self):
        """Stop ALL devices immediately. Called when server connection drops."""
        log.critical("LOCAL EMERGENCY STOP — all devices halted")
        for cd in self.devices.values():
            try:
                await cd.buttplug_device.stop()
            except Exception:
                pass
        # Cancel all patterns
        for task in self._pattern_tasks.values():
            task.cancel()
        self._pattern_tasks.clear()


# ════════════════════════════════════════════════════════════════════════
# Relay Agent — Bridges server ↔ Intiface
# ════════════════════════════════════════════════════════════════════════

class RelayAgent:
    """
    Main relay loop. Connects to both the VPS server and local Intiface,
    and bridges commands between them.
    """

    def __init__(
        self,
        server_url: str,
        token: str,
        intiface_url: str = "ws://127.0.0.1:12345",
        devices_json: str = None,
    ):
        self.server_url = server_url
        self.token = token
        self.intiface_url = intiface_url
        self.controller = DeviceController(
            intiface_url=intiface_url,
            profiles=load_profiles(devices_json),
        )
        self._running = False

    async def run(self):
        """Main loop with auto-reconnection."""
        self._running = True

        # Connect to local Intiface first
        log.info(f"Connecting to Intiface at {self.intiface_url}...")
        await self.controller.connect()
        log.info(f"Found {len(self.controller.devices)} device(s)")

        while self._running:
            try:
                await self._connect_and_relay()
            except Exception as e:
                log.error(f"Server connection error: {e}")
                await self.controller.emergency_stop()

            if self._running:
                log.info("Reconnecting to server in 5 seconds...")
                await asyncio.sleep(5)

    async def _connect_and_relay(self):
        """Single connection lifecycle."""
        log.info(f"Connecting to server at {self.server_url}...")

        async with websockets.connect(self.server_url) as ws:
            # Authenticate
            await ws.send(json.dumps({
                "type": "phone_auth",
                "token": self.token,
            }))

            auth_response = json.loads(await ws.recv())
            if auth_response.get("type") != "auth_ok":
                log.error(f"Auth failed: {auth_response}")
                return

            log.info("Authenticated with server!")

            # Send current device list immediately — no need to wait for server scan
            # (we already scanned during controller.connect())
            if self.controller.devices:
                device_list = self.controller.get_device_list()
                await ws.send(json.dumps({
                    "type": "device_list",
                    "devices": device_list,
                }))
                log.info(f"Sent device list to server: {len(device_list)} device(s)")
            else:
                log.warning("No devices to report — was the initial scan empty?")

            # Message loop
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    await self._handle_server_message(ws, msg)
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from server")
                except Exception as e:
                    log.error(f"Error handling server message: {e}")

        # If we get here, the connection closed
        log.warning("Server connection closed")
        await self.controller.emergency_stop()

    async def _handle_server_message(self, ws, msg: dict):
        """Handle incoming message from the server."""
        msg_type = msg.get("type")

        if msg_type == "heartbeat_ping":
            # Respond immediately
            await ws.send(json.dumps({
                "type": "heartbeat_pong",
                "timestamp": msg.get("timestamp", time.time()),
            }))

        elif msg_type in ("command", "pattern", "stop", "read_sensor", "scan"):
            # Execute locally and send ack
            ack = await self.controller.execute_command(msg)
            await ws.send(json.dumps(ack))

            # After a scan, also send the updated device list
            if msg_type == "scan":
                device_list = self.controller.get_device_list()
                await ws.send(json.dumps({
                    "type": "device_list",
                    "devices": device_list,
                }))
                log.info(f"Scan complete — sent device list: {len(device_list)} device(s)")

        else:
            log.debug(f"Unknown server message type: {msg_type}")

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        await self.controller.emergency_stop()
        await self.controller.disconnect()


# ════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Signal Bridge Phone Relay Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Connect to your VPS:
  python relay_client.py --server wss://signal-bridge.example.com/ws/phone --token eyJ...

  # Local testing (server on same machine):
  python relay_client.py --server ws://localhost:8420/ws/phone --token eyJ...

  # Custom Intiface port:
  python relay_client.py --server wss://example.com/ws/phone --token eyJ... --intiface ws://127.0.0.1:54321
        """,
    )
    parser.add_argument(
        "--server", required=True,
        help="WebSocket URL of the Signal Bridge server (e.g. wss://example.com/ws/phone)",
    )
    parser.add_argument(
        "--token", default=os.environ.get("SB_TOKEN"),
        help="Your JWT auth token (get from /auth/login). "
             "Can also be set via SB_TOKEN env var.",
    )
    parser.add_argument(
        "--intiface", default="ws://127.0.0.1:12345",
        help="Local Intiface Central WebSocket URL (default: ws://127.0.0.1:12345)",
    )
    parser.add_argument(
        "--devices", default=None,
        help="Path to devices.json (default: ./devices.json)",
    )
    args = parser.parse_args()

    if not args.token:
        parser.error("Token required: use --token or set SB_TOKEN environment variable")

    agent = RelayAgent(
        server_url=args.server,
        token=args.token,
        intiface_url=args.intiface,
        devices_json=args.devices,
    )

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        log.info("Shutting down...")
        asyncio.run(agent.stop())


if __name__ == "__main__":
    main()
