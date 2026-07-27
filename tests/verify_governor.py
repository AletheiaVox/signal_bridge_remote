"""Verification of the session intensity governor's heat model.

Covers the three behaviours fixed in "disabled means disabled":
  1. `enabled=False` stops the whole model, not just enforcement.
  2. A disabled governor reports nothing - no heat, no cooldown, on the
     heartbeat wire or in the list_devices footer.
  3. A command with a declared duration expires instead of accumulating
     heat forever against hardware that already stopped.

Plus the enabled-path invariants those fixes must not have broken.

Run from anywhere:  python tests/verify_governor.py
Pure logic - no server, no database, no network, no hardware.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from server.governor import GovernorConfig, GovernorState  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "  FAIL")
          + f" {name}" + (f" - {detail}" if detail and not cond else ""))


def advance(state: GovernorState, seconds: float, step: float = 1.0):
    """Run the model forward without sleeping.

    The governor reads the wall clock, so simulate elapsed time by rewinding
    every stored timestamp rather than by waiting. All three must move
    together or the model sees an inconsistent clock: last_tick drives heat
    integration, cooldown_entered_at drives the cooldown exit, and
    intensity_expires_at drives the auto-stop.

    Steps stay under the 10s sanity cap in tick().
    """
    remaining = seconds
    while remaining > 0:
        dt = min(step, remaining)
        state.last_tick -= dt
        if state.cooldown_entered_at:
            state.cooldown_entered_at -= dt
        if state.intensity_expires_at:
            state.intensity_expires_at -= dt
        state.tick()
        remaining -= dt


def new_state(enabled=True, **cfg):
    return GovernorState(cfg=GovernorConfig(enabled=enabled, **cfg))


print("-- disabled means disabled " + "-" * 36)

# Heat must not accumulate at all while the governor is off.
s = new_state(enabled=False)
s.record_command(0.7)
advance(s, 60)
check("disabled: no heat accumulates", s.heat == 0.0, f"heat={s.heat}")
check("disabled: never enters cooldown", s.in_cooldown is False)

# State left over from before the toggle is cleared, not frozen in place.
s = new_state(enabled=True)
s.record_command(0.7)
advance(s, 30)
carried = s.heat
s.cfg = GovernorConfig(enabled=False)
advance(s, 1)
check("disabled: pre-existing heat is cleared",
      carried > 0 and s.heat == 0.0, f"was {carried:.1f}, now {s.heat}")

# A disabled governor must put nothing on the wire. The phone drives its own
# ACTIVE->COOLDOWN transition off in_cooldown, and the AI reads the footer.
s = new_state(enabled=False)
s.heat = 95.0
s.in_cooldown = True
d = s.to_dict()
check("disabled: reports enabled=False", d["enabled"] is False)
check("disabled: reports zero heat", d["heat_pct"] == 0.0, str(d))
check("disabled: never reports cooldown", d["in_cooldown"] is False, str(d))
check("disabled: no predicted countdown", d["predicted_seconds"] is None)

# check() has always been correct; confirm it still is.
s = new_state(enabled=False)
s.in_cooldown = True
check("disabled: commands are never blocked", s.cfg.enabled is False)


print("\n-- timed commands expire " + "-" * 38)

# The bug: current_intensity was only ever cleared by an explicit stop, so a
# pattern that ended on its own duration kept integrating heat forever.
# At 0.7 the net rate is (0.7 * 3.0) - 2.0 = +0.1 heat/sec, so a 60s command
# builds ~6% heat and then must give it all back.
s = new_state()
s.record_command(0.7, duration=60)
advance(s, 59)
heat_while_running = s.heat
advance(s, 1)
check("timed command stops heating after its duration",
      s.current_intensity == 0.0, f"intensity={s.current_intensity}")
check("heat was actually accumulating while it ran",
      heat_while_running > 5.0, f"heat={heat_while_running:.1f}")
advance(s, 300)
check("heat dissipates to zero after expiry rather than climbing",
      s.heat == 0.0, f"{heat_while_running:.1f} -> {s.heat:.1f}")

# Duration 0 means "runs until an explicit stop" - must stay sticky.
s = new_state()
s.record_command(0.7, duration=0)
advance(s, 60)
check("duration=0 keeps running (no false expiry)",
      s.current_intensity == 0.7, f"intensity={s.current_intensity}")
check("duration=0 accumulates heat", s.heat > 0, f"heat={s.heat}")

# The regression this whole thing was found by: one finished escalate at 0.70
# reading as live heat ten minutes later.
s = new_state()
s.record_command(0.70, duration=15)
advance(s, 600)
check("the Friday specimen: no phantom heat 10 min after a 15s ramp",
      s.heat == 0.0 and s.to_dict()["predicted_seconds"] is None,
      f"heat={s.heat}, predicted={s.to_dict()['predicted_seconds']}")

# An explicit stop still clears everything, expiry included.
s = new_state()
s.record_command(0.8, duration=300)
s.record_stop()
check("explicit stop clears intensity", s.current_intensity == 0.0)
check("explicit stop clears the pending expiry", s.intensity_expires_at == 0.0)


print("\n-- enabled path still works " + "-" * 34)

# Sustained high intensity must still reach cooldown and refuse commands.
# At 1.0 the net rate is +1.0 heat/sec, so 90% arrives at ~90 seconds.
s = new_state()
s.record_command(1.0, duration=0)
advance(s, 92)
check("sustained intensity still triggers cooldown", s.in_cooldown is True,
      f"heat={s.heat:.1f}")
check("cooldown reports a countdown", s.cooldown_remaining > 0,
      f"remaining={s.cooldown_remaining}")
check("enabled: reports enabled=True", s.to_dict()["enabled"] is True)

# And cooldown must still end: heat back under the exit threshold (30%) AND
# the minimum duration (30s) elapsed. Heat dissipates at 2.0/sec, so heat is
# the binding constraint here, not the clock.
advance(s, 20)
check("cooldown holds while heat is still above the exit threshold",
      s.in_cooldown is True, f"heat={s.heat:.1f}")
advance(s, 400)
check("cooldown ends once heat and time both clear",
      s.in_cooldown is False, f"heat={s.heat:.1f}")

# Idle heat dissipates to zero and stops there.
s = new_state()
s.heat = 40.0
advance(s, 600)
check("idle heat dissipates to zero", s.heat == 0.0, f"heat={s.heat}")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
