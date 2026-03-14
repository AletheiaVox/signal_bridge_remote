"""
Signal Bridge Remote — MCP Tool Definitions

All tools that Claude can call to control devices. Each tool:
  1. Validates input
  2. Builds a command message
  3. Routes it through the session registry to the user's phone
  4. Returns the result to Claude

Expanded to support ALL Buttplug output types:
  vibrate, rotate, oscillate, constrict, temperature, led, position, spray

And sensor input types:
  battery, rssi, pressure, button, depth, position
"""
from __future__ import annotations
import asyncio
import contextvars
import json
from typing import Optional

from .models import (
    OutputType, InputType,
    DeviceCommand, PatternCommand, StopCommand, ScanCommand, ReadSensorCommand,
    CommandAck,
)
from .session_registry import registry

# Set by auth middleware before each MCP request
current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_user_id")


# ════════════════════════════════════════════════════════════════════════
# Tool registry — built at import time, consumed by the MCP endpoint
# ════════════════════════════════════════════════════════════════════════

TOOLS: list[dict] = []        # MCP tool definitions (schema)
HANDLERS: dict[str, callable] = {}  # tool_name → async handler function


def _register_tool(name: str, description: str, params: dict, required: list[str] = None):
    """Decorator factory for registering MCP tools."""
    def decorator(fn):
        schema = {
            "type": "object",
            "properties": params,
        }
        # Infer required fields: any param without a "default" key is required
        if required is not None:
            schema["required"] = required
        else:
            inferred = [k for k, v in params.items() if "default" not in v]
            if inferred:
                schema["required"] = inferred
        TOOLS.append({
            "name": name,
            "description": description,
            "inputSchema": schema,
        })
        HANDLERS[name] = fn
        return fn
    return decorator


# ════════════════════════════════════════════════════════════════════════
# Helper
# ════════════════════════════════════════════════════════════════════════

async def _send(command: dict) -> str:
    """Route a command to the current user's phone and return result text."""
    user_id = current_user_id.get()
    ack = await registry.send_to_user(user_id, command)
    if ack.success:
        return ack.message or "OK"
    else:
        return f"Error: {ack.message}"


# ════════════════════════════════════════════════════════════════════════
# Device Discovery
# ════════════════════════════════════════════════════════════════════════

@_register_tool(
    "list_devices",
    "List all connected devices with their capabilities, intensity floors, and notes.",
    {},
)
async def list_devices(**kwargs) -> str:
    user_id = current_user_id.get()
    devices = await registry.get_devices(user_id)

    # If cache is empty but phone is connected, try requesting a fresh scan
    if not devices:
        session = await registry.get_session(user_id)
        if session:
            # Phone is connected but device list is empty — request a scan
            try:
                scan_ack = await session.send_command({"type": "scan"}, timeout=15.0)
                if scan_ack.success:
                    # Give a moment for the device_list message to arrive and be processed
                    await asyncio.sleep(0.5)
                    devices = await registry.get_devices(user_id)
            except Exception:
                pass

    if not devices:
        # Check if there's even a session
        session = await registry.get_session(user_id)
        if not session:
            return (
                "No phone connected. Start the relay client on your phone/PC "
                "and connect it to the server."
            )
        return (
            "Phone is connected but no devices found. Make sure Intiface Central "
            "is running and devices are turned on."
        )

    lines = []
    for d in devices:
        caps = ", ".join(d.get("capabilities", {}).keys())
        notes = d.get("notes", "")
        floor = d.get("intensity_floor", 0)
        lines.append(
            f"• {d.get('short_name', '?')} — capabilities: [{caps}]"
            + (f" | floor: {floor}" if floor > 0 else "")
            + (f" | {notes}" if notes else "")
        )
    return "\n".join(lines)


@_register_tool(
    "scan_devices",
    "Rescan for new or reconnected Bluetooth devices.",
    {},
)
async def scan_devices(**kwargs) -> str:
    return await _send(ScanCommand().model_dump())


# ════════════════════════════════════════════════════════════════════════
# Output Commands — one tool per output type
# ════════════════════════════════════════════════════════════════════════

_OUTPUT_PARAMS = {
    "device": {
        "type": "string",
        "description": "Device short name (e.g. 'ferri', 'lush', 'gravity') or 'all'",
        "default": "all",
    },
    "intensity": {
        "type": "number",
        "description": "Intensity from 0.0 (off) to 1.0 (maximum)",
        "default": 0.5,
    },
    "duration": {
        "type": "number",
        "description": "Duration in seconds. 0 = stay on until stop command.",
        "default": 0,
    },
}


def _make_output_handler(output_type: OutputType):
    """Factory for output command handlers."""
    async def handler(
        device: str = "all", intensity: float = 0.5, duration: float = 0, **kw
    ) -> str:
        cmd = DeviceCommand(
            action=output_type,
            device=device,
            intensity=max(0.0, min(1.0, intensity)),
            duration=max(0.0, duration),
        )
        return await _send(cmd.model_dump())
    return handler


# Standard outputs (available on most devices)
_register_tool(
    "vibrate",
    "Send vibration to a device. Most common output type.",
    _OUTPUT_PARAMS,
)(_make_output_handler(OutputType.VIBRATE))

_register_tool(
    "rotate",
    "Send rotation/sonic pulse output. Device-specific — some devices use this "
    "for sonic clitoral stimulation rather than physical rotation.",
    _OUTPUT_PARAMS,
)(_make_output_handler(OutputType.ROTATE))

_register_tool(
    "oscillate",
    "Send oscillation/thrusting output. Device-specific — typically linear "
    "thrusting motion.",
    _OUTPUT_PARAMS,
)(_make_output_handler(OutputType.OSCILLATE))

# Extended outputs (device-specific, may not be available on all hardware)
_register_tool(
    "constrict",
    "Send constriction/compression output. Device-specific — available on "
    "devices with squeeze or compression mechanisms.",
    _OUTPUT_PARAMS,
)(_make_output_handler(OutputType.CONSTRICT))

_register_tool(
    "temperature",
    "Set temperature output. Device-specific — available on devices with "
    "heating or cooling elements. Intensity maps to temperature range.",
    _OUTPUT_PARAMS,
)(_make_output_handler(OutputType.TEMPERATURE))

_register_tool(
    "led",
    "Control LED light output. Device-specific — intensity controls brightness.",
    _OUTPUT_PARAMS,
)(_make_output_handler(OutputType.LED))

_register_tool(
    "position",
    "Set linear position. Device-specific — intensity maps to position "
    "along the device's range of motion (0.0 = retracted, 1.0 = extended).",
    _OUTPUT_PARAMS,
)(_make_output_handler(OutputType.POSITION))

_register_tool(
    "spray",
    "Trigger spray/liquid output. Device-specific.",
    _OUTPUT_PARAMS,
)(_make_output_handler(OutputType.SPRAY))


# ════════════════════════════════════════════════════════════════════════
# Stop
# ════════════════════════════════════════════════════════════════════════

@_register_tool(
    "stop",
    "Immediately stop all output on a device (or all devices). "
    "Also cancels any running patterns.",
    {
        "device": {
            "type": "string",
            "description": "Device short name or 'all'",
            "default": "all",
        },
    },
)
async def stop(device: str = "all", **kwargs) -> str:
    return await _send(StopCommand(device=device).model_dump())


# ════════════════════════════════════════════════════════════════════════
# Patterns — work with ANY output type
# ════════════════════════════════════════════════════════════════════════

_PATTERN_PARAMS = {
    "device": {
        "type": "string",
        "description": "Device short name or 'all'",
        "default": "all",
    },
    "output_type": {
        "type": "string",
        "description": "Which output to modulate: vibrate, rotate, oscillate, "
                       "constrict, temperature, led, position, spray",
        "default": "vibrate",
    },
    "intensity": {
        "type": "number",
        "description": "Peak intensity (0.0–1.0)",
        "default": 0.6,
    },
    "duration": {
        "type": "number",
        "description": "Duration in seconds",
        "default": 10,
    },
}


def _make_pattern_handler(pattern_name: str):
    async def handler(
        device: str = "all",
        output_type: str = "vibrate",
        intensity: float = 0.6,
        duration: float = 10,
        hold_seconds: float = 0,
        **kw,
    ) -> str:
        cmd = PatternCommand(
            pattern=pattern_name,
            output_type=OutputType(output_type),
            device=device,
            intensity=max(0.0, min(1.0, intensity)),
            duration=max(0.0, duration),
            hold_seconds=max(0.0, hold_seconds),
        )
        return await _send(cmd.model_dump())
    return handler


_register_tool(
    "pulse",
    "Rhythmic on/off pattern. 0.5s on at intensity, 0.3s off, repeating. "
    "Works with any output type (default: vibrate).",
    _PATTERN_PARAMS,
)(_make_pattern_handler("pulse"))

_register_tool(
    "wave",
    "Smooth sine-wave intensity modulation. Rises and falls continuously. "
    "Works with any output type (default: vibrate).",
    _PATTERN_PARAMS,
)(_make_pattern_handler("wave"))

_register_tool(
    "escalate",
    "Gradual ramp from 0% to peak intensity over the duration, then hold at peak. "
    "Use hold_seconds to auto-stop after holding (0 = hold indefinitely until stop command). "
    "Works with any output type (default: vibrate).",
    {k: v for k, v in _PATTERN_PARAMS.items() if k != "intensity"}
    | {
        "intensity": {"type": "number", "description": "Peak intensity to ramp up to", "default": 1.0},
        "hold_seconds": {
            "type": "number",
            "description": "Seconds to hold at peak after ramp completes. 0 = hold indefinitely until explicit stop.",
            "default": 0,
        },
    },
)(_make_pattern_handler("escalate"))


# ════════════════════════════════════════════════════════════════════════
# Sensor Inputs — read data FROM the device
# ════════════════════════════════════════════════════════════════════════

@_register_tool(
    "read_battery",
    "Read battery level from a device. Returns percentage (0-100).",
    {
        "device": {
            "type": "string",
            "description": "Device short name",
        },
    },
)
async def read_battery(device: str, **kwargs) -> str:
    cmd = ReadSensorCommand(sensor=InputType.BATTERY, device=device)
    return await _send(cmd.model_dump())


@_register_tool(
    "read_sensor",
    "Read a sensor value from a device. Available sensors depend on hardware: "
    "battery, rssi (signal strength), pressure, button, depth, position. "
    "Not all devices support all sensors.",
    {
        "device": {
            "type": "string",
            "description": "Device short name",
        },
        "sensor": {
            "type": "string",
            "description": "Sensor type: battery, rssi, pressure, button, depth, position",
        },
    },
)
async def read_sensor(device: str, sensor: str, **kwargs) -> str:
    cmd = ReadSensorCommand(sensor=InputType(sensor), device=device)
    return await _send(cmd.model_dump())
