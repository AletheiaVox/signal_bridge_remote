#!/usr/bin/env python3
"""
Signal Bridge Relay Client - Termux Edition v3
- Processes commands in background tasks so heartbeats always respond instantly
- Drains Intiface WebSocket responses to prevent buffer buildup
"""

import argparse, asyncio, json, logging, math, os, time, sys

try:
    import websockets
except ImportError:
    print("Missing dependency. Run: pip install websockets")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("relay")

DEFAULT_DEVICES = {
    "ferri": {
        "device_id": "ferri",
        "name": "Lovense Ferri",
        "intensity_floor": 0.0,
        "supported_outputs": ["vibrate"],
    },
    "enigma": {
        "device_id": "enigma",
        "name": "Lovense Enigma",
        "intensity_floor": 0.4,
        "supported_outputs": ["vibrate", "rotate"],
    },
}


def load_profiles(path="devices.json"):
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        log.info(f"Loaded device profiles from {path}")
        return data.get("devices", data)
    log.info("No devices.json found - using built-in defaults")
    return DEFAULT_DEVICES


class ButtplugRaw:
    def __init__(self, intiface_url="ws://127.0.0.1:12345"):
        self.url = intiface_url
        self.ws = None
        self._msg_id = 0
        self.bp_devices = {}
        self.name_map = {}
        self.profiles = load_profiles()
        self._drain_task = None

    def _next_id(self):
        self._msg_id += 1
        return self._msg_id

    async def connect(self):
        log.info(f"Connecting to Intiface at {self.url} ...")
        self.ws = await websockets.connect(self.url)
        await self._send([{
            "RequestServerInfo": {
                "Id": self._next_id(),
                "ClientName": "Signal Bridge Termux",
                "MessageVersion": 3,
            }
        }])
        resp = await self._recv()
        if resp and "ServerInfo" in resp[0]:
            info = resp[0]["ServerInfo"]
            log.info(f"Connected to {info.get('ServerName', 'Intiface')} (protocol v{info.get('MessageVersion', '?')})")
        else:
            log.warning(f"Unexpected handshake response: {resp}")

    async def start_drain(self):
        """Background task to read and process Intiface messages (device events, Ok responses)."""
        async def _drain():
            try:
                while self.ws:
                    try:
                        raw = await asyncio.wait_for(self.ws.recv(), timeout=30.0)
                        msgs = json.loads(raw)
                        for msg in msgs:
                            self._handle_event(msg)
                    except asyncio.TimeoutError:
                        pass
            except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
                pass
        self._drain_task = asyncio.create_task(_drain())

    async def stop_drain(self):
        if self._drain_task:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

    async def scan(self, duration=5.0):
        log.info("Scanning for devices ...")
        await self._send([{"StartScanning": {"Id": self._next_id()}}])
        await asyncio.sleep(duration)
        await self._send([{"RequestDeviceList": {"Id": self._next_id()}}])
        await asyncio.sleep(1.0)
        log.info(f"Scan complete - {len(self.bp_devices)} device(s) found")

    def _handle_event(self, msg):
        if "DeviceAdded" in msg:
            self._add_device(msg["DeviceAdded"])
        elif "DeviceRemoved" in msg:
            idx = msg["DeviceRemoved"].get("DeviceIndex")
            for name, bidx in list(self.name_map.items()):
                if bidx == idx:
                    del self.name_map[name]
                    break
            self.bp_devices.pop(idx, None)
            log.info(f"Device removed (index {idx})")
        elif "DeviceList" in msg:
            for dev in msg["DeviceList"].get("Devices", []):
                self._add_device(dev)

    def _add_device(self, dev_info):
        idx = dev_info["DeviceIndex"]
        bp_name = dev_info.get("DeviceName", f"device-{idx}")
        self.bp_devices[idx] = dev_info
        short_name = None
        bp_lower = bp_name.lower()
        for pid, profile in self.profiles.items():
            pname = profile["name"].lower()
            if pname in bp_lower or pid.lower() in bp_lower:
                short_name = pid
                break
        if not short_name:
            short_name = bp_name.lower().replace(" ", "_")
        self.name_map[short_name] = idx
        log.info(f"Device: {bp_name} -> '{short_name}' (index {idx})")

    def get_device_list(self):
        result = []
        for short_name, idx in self.name_map.items():
            bp_dev = self.bp_devices.get(idx, {})
            bp_name = bp_dev.get("DeviceName", short_name)
            profile = self.profiles.get(short_name, {})
            capabilities = {}
            for feature in bp_dev.get("DeviceMessages", {}).get("ScalarCmd", []):
                at = feature.get("ActuatorType", "").lower()
                if at in ("vibrate", "rotate", "oscillate"):
                    capabilities[at] = {}
            if not capabilities:
                for o in profile.get("supported_outputs", ["vibrate"]):
                    capabilities[o] = {}
            result.append({
                "short_name": short_name,
                "name": profile.get("name", bp_name),
                "intensity_floor": profile.get("intensity_floor", 0.0),
                "capabilities": capabilities,
                "notes": profile.get("name", bp_name),
            })
        return result

    async def scalar_cmd(self, idx, intensity, actuator_type="Vibrate"):
        bp_dev = self.bp_devices.get(idx, {})
        scalars = []
        for i, feature in enumerate(bp_dev.get("DeviceMessages", {}).get("ScalarCmd", [])):
            if feature.get("ActuatorType", "").lower() == actuator_type.lower():
                scalars.append({
                    "Index": i,
                    "Scalar": max(0.0, min(1.0, intensity)),
                    "ActuatorType": feature["ActuatorType"],
                })
        if not scalars:
            scalars = [{"Index": 0, "Scalar": max(0.0, min(1.0, intensity)), "ActuatorType": actuator_type}]
        await self._send([{
            "ScalarCmd": {
                "Id": self._next_id(),
                "DeviceIndex": idx,
                "Scalars": scalars,
            }
        }])

    async def stop_device(self, idx):
        await self._send([{"StopDeviceCmd": {"Id": self._next_id(), "DeviceIndex": idx}}])

    async def stop_all(self):
        await self._send([{"StopAllDevices": {"Id": self._next_id()}}])

    async def _send(self, msgs):
        if self.ws:
            await self.ws.send(json.dumps(msgs))

    async def _recv(self):
        if self.ws:
            raw = await self.ws.recv()
            return json.loads(raw)
        return None

    async def close(self):
        await self.stop_drain()
        if self.ws:
            await self.ws.close()


class PatternRunner:
    def __init__(self, bp):
        self.bp = bp
        self.active_tasks = {}

    def _floor(self, raw, floor):
        if raw <= 0.01:
            return 0.0
        if floor > 0:
            return min(1.0, floor + raw * (1.0 - floor))
        return min(1.0, raw)

    def _resolve_targets(self, device):
        if device == "all":
            return list(self.bp.name_map.items())
        if device in self.bp.name_map:
            return [(device, self.bp.name_map[device])]
        return []

    async def cancel_patterns(self, device="all"):
        if device == "all":
            for task in self.active_tasks.values():
                task.cancel()
            self.active_tasks.clear()
        else:
            task = self.active_tasks.pop(device, None)
            if task:
                task.cancel()

    async def run_command(self, cmd):
        msg_type = cmd.get("type", "")
        request_id = cmd.get("request_id", "")
        if msg_type == "command":
            return await self._handle_command(cmd, request_id)
        elif msg_type == "pattern":
            return await self._handle_pattern(cmd, request_id)
        elif msg_type == "stop":
            return await self._handle_stop(cmd, request_id)
        elif msg_type == "scan":
            return await self._handle_scan(request_id)
        elif msg_type == "read_sensor":
            return self._ack(False, "Sensors not supported in Termux relay", request_id)
        else:
            return self._ack(False, f"Unknown command type: {msg_type}", request_id)

    async def _handle_command(self, cmd, request_id):
        device = cmd.get("device", "all")
        intensity = cmd.get("intensity", 0.5)
        output_type = cmd.get("action", cmd.get("output_type", "vibrate"))
        duration = cmd.get("duration", 0)
        targets = self._resolve_targets(device)

        if not targets:
            available = list(self.bp.name_map.keys())
            return self._ack(False, f"Device not found. Available: {available}", request_id)

        log.info(f"Command: {output_type} intensity={intensity} duration={duration} targets={[t[0] for t in targets]}")

        for short_name, idx in targets:
            profile = self.bp.profiles.get(short_name, {})
            floor = profile.get("intensity_floor", 0.0)
            adj = self._floor(intensity, floor)
            log.info(f"  {short_name}: raw={intensity} floor={floor} adjusted={adj}")
            await self.bp.scalar_cmd(idx, adj, output_type)

        names = [t[0] for t in targets]

        if duration > 0:
            async def auto_stop():
                await asyncio.sleep(duration)
                for sn, ix in targets:
                    await self.bp.stop_device(ix)
                log.info(f"Auto-stopped after {duration}s")
            asyncio.create_task(auto_stop())

        return self._ack(True, "Set " + output_type + " " + str(intensity) + " on " + ", ".join(names), request_id, names)

    async def _handle_pattern(self, cmd, request_id):
        pattern = cmd.get("pattern", "pulse")
        device = cmd.get("device", "all")
        intensity = cmd.get("intensity", 0.6)
        duration = cmd.get("duration", 10.0)
        output_type = cmd.get("action", cmd.get("output_type", "vibrate"))
        hold = cmd.get("hold_seconds", 0.0)
        targets = self._resolve_targets(device)

        if not targets:
            return self._ack(False, "Device not found", request_id)

        for short_name, idx in targets:
            await self.cancel_patterns(short_name)
            profile = self.bp.profiles.get(short_name, {})
            floor = profile.get("intensity_floor", 0.0)

            if pattern == "pulse":
                task = asyncio.create_task(self._run_pulse(idx, output_type, intensity, duration, floor))
            elif pattern == "wave":
                task = asyncio.create_task(self._run_wave(idx, output_type, intensity, duration, floor))
            elif pattern == "escalate":
                task = asyncio.create_task(self._run_escalate(idx, output_type, intensity, duration, hold, floor))
            else:
                return self._ack(False, "Unknown pattern: " + pattern, request_id)
            self.active_tasks[short_name] = task

        names = [t[0] for t in targets]
        return self._ack(True, "Pattern " + pattern + " started on " + ", ".join(names), request_id, names)

    async def _handle_stop(self, cmd, request_id):
        device = cmd.get("device", "all")
        targets = self._resolve_targets(device)
        fallback = False

        if device != "all" and not targets:
            fallback = True
            targets = list(self.bp.name_map.items())

        for short_name, idx in targets:
            await self.cancel_patterns(short_name)
            await self.bp.stop_device(idx)

        if fallback:
            available = ", ".join(self.bp.name_map.keys()) or "none"
            return self._ack(True, "Unknown device - stopped ALL as safety fallback. Available: " + available, request_id, [t[0] for t in targets])

        if device == "all":
            await self.bp.stop_all()
            self.active_tasks.clear()

        names = [t[0] for t in targets]
        return self._ack(True, "Stopped " + (", ".join(names) if names else "all"), request_id, names)

    async def _handle_scan(self, request_id):
        await self.bp.scan(duration=5.0)
        return self._ack(True, "Scan complete - " + str(len(self.bp.bp_devices)) + " device(s)", request_id)

    async def _run_pulse(self, idx, output_type, intensity, duration, floor):
        try:
            start = time.time()
            on = True
            while time.time() - start < duration:
                if on:
                    adj = self._floor(intensity, floor)
                    await self.bp.scalar_cmd(idx, adj, output_type)
                else:
                    await self.bp.scalar_cmd(idx, 0.0, output_type)
                on = not on
                await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await self.bp.stop_device(idx)
            except Exception:
                pass

    async def _run_wave(self, idx, output_type, intensity, duration, floor):
        try:
            start = time.time()
            while time.time() - start < duration:
                elapsed = time.time() - start
                raw = (math.sin(elapsed * 2.0) + 1.0) / 2.0 * intensity
                adj = self._floor(raw, floor)
                await self.bp.scalar_cmd(idx, adj, output_type)
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await self.bp.stop_device(idx)
            except Exception:
                pass

    async def _run_escalate(self, idx, output_type, peak, duration, hold, floor):
        try:
            steps = 20
            for i in range(steps + 1):
                val = (i / steps) * peak
                adj = self._floor(val, floor)
                await self.bp.scalar_cmd(idx, adj, output_type)
                await asyncio.sleep(duration / steps)
            if hold > 0:
                await asyncio.sleep(hold)
                await self.bp.stop_device(idx)
        except asyncio.CancelledError:
            try:
                await self.bp.stop_device(idx)
            except Exception:
                pass

    def _ack(self, success, message, request_id, devices=None):
        return {
            "type": "command_ack",
            "request_id": request_id,
            "success": success,
            "message": message,
            "devices_affected": devices or [],
        }


async def relay_loop(server_url, token, intiface_url):
    bp = ButtplugRaw(intiface_url)
    runner = PatternRunner(bp)
    ws_lock = asyncio.Lock()

    while True:
        try:
            await bp.connect()
            await bp.start_drain()
            await bp.scan(duration=5.0)

            device_list = bp.get_device_list()
            log.info(f"Devices ready: {[d['short_name'] for d in device_list]}")

            log.info(f"Connecting to server: {server_url}")
            async with websockets.connect(server_url) as ws:
                await ws.send(json.dumps({"type": "phone_auth", "token": token}))
                auth_resp = json.loads(await ws.recv())

                if auth_resp.get("type") != "auth_ok":
                    log.error(f"Auth failed: {auth_resp}")
                    await asyncio.sleep(5)
                    continue

                log.info("Authenticated with server!")

                if device_list:
                    await ws.send(json.dumps({"type": "device_list", "devices": device_list}))
                    log.info(f"Sent device list: {len(device_list)} device(s)")
                else:
                    log.warning("No devices to report after scan")

                async def process_command(msg, msg_type):
                    """Handle a command in the background so heartbeats stay responsive."""
                    try:
                        ack = await runner.run_command(msg)
                        async with ws_lock:
                            await ws.send(json.dumps(ack))
                        log.info(f"-> Ack: {ack.get('message', '')}")

                        if msg_type == "scan":
                            dl = bp.get_device_list()
                            async with ws_lock:
                                await ws.send(json.dumps({"type": "device_list", "devices": dl}))
                            log.info(f"Sent updated device list: {len(dl)} device(s)")
                    except Exception as e:
                        log.error(f"Command processing error: {e}")

                async for raw_msg in ws:
                    try:
                        msg = json.loads(raw_msg)
                        msg_type = msg.get("type", "")

                        if msg_type in ("ping", "heartbeat_ping"):
                            async with ws_lock:
                                await ws.send(json.dumps({"type": "heartbeat_pong"}))
                            continue

                        if msg_type in ("command", "pattern", "stop", "read_sensor", "scan"):
                            log.info(f"<- Server: {msg_type}")
                            asyncio.create_task(process_command(msg, msg_type))

                        else:
                            log.warning(f"Unknown message type: {msg_type}")

                    except json.JSONDecodeError:
                        log.warning("Bad JSON from server")

        except websockets.exceptions.ConnectionClosed as e:
            log.warning(f"Connection closed: {e}. Reconnecting in 5s ...")
        except ConnectionRefusedError:
            log.warning("Connection refused. Is Intiface running? Retrying in 5s ...")
        except Exception as e:
            log.error(f"Error: {e}. Retrying in 5s ...")

        try:
            await bp.close()
        except Exception:
            pass
        bp.ws = None
        bp.bp_devices.clear()
        bp.name_map.clear()

        await asyncio.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="Signal Bridge Termux Relay")
    parser.add_argument("--server", default="wss://signal-bridge.duckdns.org/ws/phone", help="VPS WebSocket URL")
    parser.add_argument("--token", default=os.environ.get("SB_TOKEN"), help="JWT auth token (or set SB_TOKEN env var)")
    parser.add_argument("--intiface", default="ws://127.0.0.1:12345", help="Intiface Central WebSocket URL")
    args = parser.parse_args()

    if not args.token:
        parser.error("Token required: use --token or set SB_TOKEN env var")

    log.info("=== Signal Bridge Termux Relay v3 ===")
    asyncio.run(relay_loop(args.server, args.token, args.intiface))


if __name__ == "__main__":
    main()
