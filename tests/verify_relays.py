"""Behavioral verification of the pattern-lifecycle + feature_index changes
in both relay clients, using fakes for the hardware layer.

Run from anywhere:  python tests/verify_relays.py
Needs the phone deps (buttplug, websockets). No hardware, no network —
the Intiface/Bluetooth layer is faked and every call is recorded.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL") + f" {name}" + (f" — {detail}" if detail and not cond else ""))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ════════════════════════════════════════════════════════════════════
# Part 1: termux_relay_v3.PatternRunner with a fake ButtplugRaw
# ════════════════════════════════════════════════════════════════════

v3 = load("termux_relay_v3", str(REPO_ROOT / "termux_relay_v3.py"))


class FakeBP:
    def __init__(self):
        self.calls = []  # (kind, idx, value, otype, feature_index)
        self.profiles = {"lush": {"name": "Lush", "intensity_floor": 0.0},
                         "dolce": {"name": "Dolce", "intensity_floor": 0.0}}
        self.name_map = {"lush": 0, "dolce": 1}
        self.bp_devices = {
            0: {"DeviceIndex": 0, "DeviceName": "Lovense Lush",
                "DeviceMessages": {"ScalarCmd": [{"ActuatorType": "Vibrate"}]}},
            1: {"DeviceIndex": 1, "DeviceName": "Lovense Dolce",
                "DeviceMessages": {"ScalarCmd": [{"ActuatorType": "Vibrate"},
                                                  {"ActuatorType": "Vibrate"}]}},
        }

    async def scalar_cmd(self, idx, intensity, actuator_type="Vibrate", feature_index=None):
        self.calls.append(("scalar", idx, intensity, actuator_type, feature_index))

    async def stop_device(self, idx):
        self.calls.append(("stop", idx, None, None, None))

    async def stop_all(self):
        self.calls.append(("stop_all", None, None, None, None))


async def test_v3():
    # duration=0 pulse runs indefinitely, stop cancels + stops device
    bp = FakeBP()
    r = v3.PatternRunner(bp)
    ack = await r.run_command({"type": "pattern", "pattern": "pulse", "device": "lush",
                               "intensity": 0.5, "duration": 0})
    await asyncio.sleep(1.0)
    writes = [c for c in bp.calls if c[0] == "scalar"]
    task = r.active_tasks.get("lush")
    check("v3 pulse duration=0 keeps running", ack["success"] and len(writes) >= 2 and task and not task.done(),
          f"writes={len(writes)} task_done={task.done() if task else 'missing'}")
    await r.run_command({"type": "stop", "device": "lush"})
    await asyncio.sleep(0.05)
    check("v3 stop cancels indefinite pulse", not r.active_tasks and any(c[0] == "stop" for c in bp.calls))

    # escalate hold=0 suspends at peak (task alive), cancel -> device stopped
    bp = FakeBP()
    r = v3.PatternRunner(bp)
    await r.run_command({"type": "pattern", "pattern": "escalate", "device": "lush",
                         "intensity": 0.8, "duration": 0.2, "hold_seconds": 0})
    await asyncio.sleep(0.6)
    task = r.active_tasks.get("lush")
    peak_writes = [c for c in bp.calls if c[0] == "scalar" and abs(c[2] - 0.8) < 1e-6]
    stops_before = [c for c in bp.calls if c[0] == "stop"]
    check("v3 escalate hold=0 suspends at peak", task and not task.done() and peak_writes and not stops_before,
          f"task={bool(task)} done={task.done() if task else '-'} peak={len(peak_writes)} stops={len(stops_before)}")
    await r.run_command({"type": "stop", "device": "lush"})
    await asyncio.sleep(0.05)
    check("v3 escalate stop after hold works", any(c[0] == "stop" for c in bp.calls))

    # escalate hold>0 auto-stops via finally
    bp = FakeBP()
    r = v3.PatternRunner(bp)
    await r.run_command({"type": "pattern", "pattern": "escalate", "device": "lush",
                         "intensity": 0.8, "duration": 0.1, "hold_seconds": 0.2})
    await asyncio.sleep(0.8)
    check("v3 escalate hold>0 auto-stops", any(c[0] == "stop" for c in bp.calls),
          f"calls={bp.calls[-3:]}")

    # direct command supersedes a running pattern
    bp = FakeBP()
    r = v3.PatternRunner(bp)
    await r.run_command({"type": "pattern", "pattern": "wave", "device": "lush",
                         "intensity": 0.6, "duration": 0})
    await asyncio.sleep(0.2)
    await r.run_command({"type": "command", "action": "vibrate", "device": "lush",
                         "intensity": 0.3, "duration": 0})
    await asyncio.sleep(0.3)
    # after the direct command, no further wave writes should occur:
    # the last scalar calls should all be the direct 0.3 write
    idx_direct = max(i for i, c in enumerate(bp.calls) if c[0] == "scalar" and c[2] == 0.3)
    later_waves = [c for c in bp.calls[idx_direct + 1:] if c[0] == "scalar" and c[2] != 0.3]
    check("v3 direct command supersedes pattern", not r.active_tasks and not later_waves,
          f"tasks={list(r.active_tasks)} later={later_waves[:3]}")

    # auto-stop tracked: superseded before firing
    bp = FakeBP()
    r = v3.PatternRunner(bp)
    await r.run_command({"type": "command", "action": "vibrate", "device": "lush",
                         "intensity": 0.5, "duration": 0.2})
    await asyncio.sleep(0.05)
    await r.run_command({"type": "command", "action": "vibrate", "device": "lush",
                         "intensity": 0.7, "duration": 0})
    await asyncio.sleep(0.4)
    zero_writes = [c for c in bp.calls if c[0] == "scalar" and c[2] == 0.0]
    check("v3 stale auto-stop cancelled by newer command", not zero_writes and not r.timed_stops,
          f"zeros={zero_writes} pending={list(r.timed_stops)}")

    # auto-stop still fires when NOT superseded
    bp = FakeBP()
    r = v3.PatternRunner(bp)
    await r.run_command({"type": "command", "action": "vibrate", "device": "lush",
                         "intensity": 0.5, "duration": 0.15})
    await asyncio.sleep(0.5)
    zero_writes = [c for c in bp.calls if c[0] == "scalar" and c[2] == 0.0]
    check("v3 auto-stop fires when due", len(zero_writes) == 1 and not r.timed_stops,
          f"zeros={zero_writes}")

    # feature_index: dolce has two vibrate features; index filters ScalarCmd
    bp = FakeBP()
    r = v3.PatternRunner(bp)
    await r.run_command({"type": "command", "action": "vibrate", "device": "dolce",
                         "intensity": 0.4, "duration": 0, "feature_index": 1})
    fi_calls = [c for c in bp.calls if c[0] == "scalar"]
    check("v3 feature_index threaded to scalar_cmd", fi_calls and fi_calls[0][4] == 1, f"{fi_calls}")

    # stop unknown device -> stop-all fallback intact
    bp = FakeBP()
    r = v3.PatternRunner(bp)
    ack = await r.run_command({"type": "stop", "device": "nonexistent"})
    check("v3 unknown-device stop falls back to all", ack["success"] and "ALL" in ack["message"]
          and any(c[0] == "stop" for c in bp.calls), ack["message"])


asyncio.run(test_v3())

# ════════════════════════════════════════════════════════════════════
# Part 2: relay_client.DeviceController with a fake buttplug device
# ════════════════════════════════════════════════════════════════════

rc = load("relay_client", str(REPO_ROOT / "phone" / "relay_client.py"))


class FakeFeature:
    def __init__(self, rec, fi, otypes):
        self.rec, self.fi, self.otypes = rec, fi, otypes

    def has_output(self, otype):
        return otype in self.otypes

    async def run_output(self, cmd):
        self.rec.append(("feature", self.fi, cmd.output_type, cmd.value))


class FakeDevice:
    def __init__(self, rec, n_vib=1):
        self.rec = rec
        self.features = {i: FakeFeature(rec, i, {rc.OutputType.VIBRATE}) for i in range(n_vib)}

    async def run_output(self, cmd):
        self.rec.append(("all", None, cmd.output_type, cmd.value))

    async def stop(self, **kw):
        self.rec.append(("stop", None, None, None))


def make_controller(devs):
    c = rc.DeviceController.__new__(rc.DeviceController)
    c.intiface_url = ""
    c.profiles = []
    c.client = None
    c.devices = {}
    c._pattern_tasks = {}
    c._timed_stops = {}
    c._connected = True
    for name, n in devs:
        prof = rc.DeviceProfile(short_name=name, match_strings=[name])
        c.devices[name] = rc.ConnectedDevice(0, FakeDevice_rec[name], prof, ["vibrate"])
    return c


async def test_rc():
    global FakeDevice_rec
    V = rc.OutputType.VIBRATE

    # pulse duration=0 indefinite + stop
    rec = []
    FakeDevice_rec = {"lush": FakeDevice(rec)}
    c = make_controller([("lush", 1)])
    ack = await c.execute_command({"type": "pattern", "pattern": "pulse", "device": "lush",
                                   "intensity": 0.5, "duration": 0, "request_id": "r1"})
    await asyncio.sleep(1.0)
    task = c._pattern_tasks.get("lush")
    writes = [x for x in rec if x[0] == "all"]
    check("rc pulse duration=0 keeps running", ack["success"] and task and not task.done() and len(writes) >= 2,
          f"writes={len(writes)}")
    await c.execute_command({"type": "stop", "device": "lush", "request_id": "r2"})
    await asyncio.sleep(0.05)
    check("rc stop cancels indefinite pulse", not c._pattern_tasks and ("stop", None, None, None) in rec)

    # escalate hold=0 suspends at peak, error-free; explicit stop lands in finally
    rec = []
    FakeDevice_rec = {"lush": FakeDevice(rec)}
    c = make_controller([("lush", 1)])
    await c.execute_command({"type": "pattern", "pattern": "escalate", "device": "lush",
                             "intensity": 0.8, "duration": 0.2, "hold_seconds": 0, "request_id": "r3"})
    await asyncio.sleep(0.6)
    task = c._pattern_tasks.get("lush")
    stops = [x for x in rec if x[0] == "stop"]
    check("rc escalate hold=0 suspends at peak", task and not task.done() and not stops,
          f"task={bool(task)} stops={stops}")
    await c.execute_command({"type": "stop", "device": "lush", "request_id": "r4"})
    await asyncio.sleep(0.05)
    check("rc escalate finally stops on cancel", any(x[0] == "stop" for x in rec))

    # escalate error mid-ramp -> device stopped (the §3 safety fix)
    rec = []
    dev = FakeDevice(rec)
    calls = {"n": 0}
    orig = dev.run_output

    async def flaky(cmd):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("BT hiccup")
        await orig(cmd)
    dev.run_output = flaky
    FakeDevice_rec = {"lush": dev}
    c = make_controller([("lush", 1)])
    await c.execute_command({"type": "pattern", "pattern": "escalate", "device": "lush",
                             "intensity": 0.8, "duration": 0.3, "hold_seconds": 0, "request_id": "r5"})
    await asyncio.sleep(0.5)
    check("rc escalate error mid-ramp stops device", any(x[0] == "stop" for x in rec),
          f"rec tail={rec[-3:]}")

    # new pattern supersedes old pattern (per-device keying)
    rec = []
    FakeDevice_rec = {"lush": FakeDevice(rec)}
    c = make_controller([("lush", 1)])
    await c.execute_command({"type": "pattern", "pattern": "pulse", "device": "lush",
                             "intensity": 0.9, "duration": 0, "request_id": "r6"})
    await asyncio.sleep(0.1)
    await c.execute_command({"type": "pattern", "pattern": "wave", "device": "lush",
                             "intensity": 0.4, "duration": 0, "request_id": "r7"})
    await asyncio.sleep(0.3)
    idx_wave_start = len(rec)  # marker not needed; assert one task only
    check("rc one pattern per device", len(c._pattern_tasks) == 1)
    # after wave started, no more 0.9 pulse-on writes
    tail = rec[-4:]
    pulse_writes_late = [x for x in tail if x[0] == "all" and x[3] == 0.9]
    check("rc old pattern actually cancelled", not pulse_writes_late, f"tail={tail}")
    await c.execute_command({"type": "stop", "device": "all", "request_id": "r8"})

    # direct command supersedes pattern + auto-stop bookkeeping
    rec = []
    FakeDevice_rec = {"lush": FakeDevice(rec)}
    c = make_controller([("lush", 1)])
    await c.execute_command({"type": "pattern", "pattern": "wave", "device": "lush",
                             "intensity": 0.6, "duration": 0, "request_id": "r9"})
    await asyncio.sleep(0.2)
    await c.execute_command({"type": "command", "action": "vibrate", "device": "lush",
                             "intensity": 0.3, "duration": 0.2, "request_id": "r10"})
    check("rc direct command cancels pattern", not c._pattern_tasks and len(c._timed_stops) == 1)
    await c.execute_command({"type": "command", "action": "vibrate", "device": "lush",
                             "intensity": 0.7, "duration": 0, "request_id": "r11"})
    await asyncio.sleep(0.4)
    zeros = [x for x in rec if x[0] == "all" and x[3] == 0]
    # wave writes near-zero floats but exact 0 only from timed stop / pulse-off
    check("rc stale auto-stop cancelled", not c._timed_stops and not zeros,
          f"zeros={zeros} pending={list(c._timed_stops)}")

    # feature_index routes through device.features[i]
    rec = []
    FakeDevice_rec = {"dolce": FakeDevice(rec, n_vib=2)}
    c = make_controller([("dolce", 2)])
    ack = await c.execute_command({"type": "command", "action": "vibrate", "device": "dolce",
                                   "intensity": 0.4, "duration": 0, "feature_index": 1, "request_id": "r12"})
    feats = [x for x in rec if x[0] == "feature"]
    check("rc feature_index targets single motor", ack["success"] and feats and feats[0][1] == 1
          and not [x for x in rec if x[0] == "all"], f"{rec}")

    # bad feature_index -> helpful error, no crash
    ack = await c.execute_command({"type": "command", "action": "vibrate", "device": "dolce",
                                   "intensity": 0.4, "duration": 0, "feature_index": 7, "request_id": "r13"})
    check("rc bad feature_index helpful error", not ack["success"] and "valid" in ack["message"],
          ack["message"])


asyncio.run(test_rc())

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
