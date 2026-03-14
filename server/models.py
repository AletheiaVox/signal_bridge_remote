"""
Signal Bridge Remote — Shared Models & Command Protocol

Defines the JSON message format between all three tiers:
  Claude ←(MCP)→ VPS Server ←(WebSocket)→ Phone
"""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, Any
from enum import Enum


# ── Output Types (commands TO the device) ───────────────────────────────

class OutputType(str, Enum):
    VIBRATE = "vibrate"
    ROTATE = "rotate"
    OSCILLATE = "oscillate"
    CONSTRICT = "constrict"      # compression / squeeze
    TEMPERATURE = "temperature"  # heating / cooling
    LED = "led"                  # light control
    POSITION = "position"        # linear positioning
    SPRAY = "spray"              # liquid / spray


# ── Input Types (readings FROM the device) ──────────────────────────────

class InputType(str, Enum):
    BATTERY = "battery"
    RSSI = "rssi"          # signal strength
    PRESSURE = "pressure"
    BUTTON = "button"
    DEPTH = "depth"
    POSITION = "position"


# ════════════════════════════════════════════════════════════════════════
# Server → Phone messages
# ════════════════════════════════════════════════════════════════════════

class DeviceCommand(BaseModel):
    """Direct output command to a device."""
    type: str = "command"
    action: OutputType
    device: str = "all"
    intensity: float = Field(0.5, ge=0.0, le=1.0)
    duration: float = Field(0.0, ge=0.0)  # 0 = indefinite


class PatternCommand(BaseModel):
    """Run a named pattern on a device."""
    type: str = "pattern"
    pattern: str  # "pulse", "wave", "escalate"
    output_type: OutputType = OutputType.VIBRATE
    device: str = "all"
    intensity: float = Field(0.6, ge=0.0, le=1.0)
    duration: float = Field(10.0, ge=0.0)
    hold_seconds: float = Field(0.0, ge=0.0)  # escalate only: 0 = hold at peak indefinitely


class StopCommand(BaseModel):
    type: str = "stop"
    device: str = "all"


class ScanCommand(BaseModel):
    type: str = "scan"


class ReadSensorCommand(BaseModel):
    type: str = "read_sensor"
    sensor: InputType
    device: str


class HeartbeatPing(BaseModel):
    type: str = "heartbeat_ping"
    timestamp: float


# ════════════════════════════════════════════════════════════════════════
# Phone → Server messages
# ════════════════════════════════════════════════════════════════════════

class HeartbeatPong(BaseModel):
    type: str = "heartbeat_pong"
    timestamp: float


class DeviceListReport(BaseModel):
    type: str = "device_list"
    devices: list[dict[str, Any]] = []


class CommandAck(BaseModel):
    type: str = "command_ack"
    success: bool = True
    message: str = ""
    request_id: Optional[str] = None
    data: Optional[dict[str, Any]] = None  # sensor readings, etc.


class PhoneAuth(BaseModel):
    type: str = "phone_auth"
    token: str


# ════════════════════════════════════════════════════════════════════════
# MCP JSON-RPC models
# ════════════════════════════════════════════════════════════════════════

class MCPRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[str | int] = None
    method: str
    params: Optional[dict[str, Any]] = None


class MCPResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[str | int] = None
    result: Optional[Any] = None
    error: Optional[dict[str, Any]] = None
