"""
Signal Bridge Remote — Session Intensity Governor

Tracks cumulative session intensity ("heat") per user and enforces
cooldown periods when the threshold is reached.

Heat model:
  - Accumulates at: current_intensity × HEAT_RATE per second
  - Dissipates at: COOL_RATE per second when intensity = 0
  - Partial dissipation when running at low intensity:
    net_rate = (intensity × HEAT_RATE) - COOL_RATE
  - Cooldown triggers at COOLDOWN_THRESHOLD (default 90%)
  - Cooldown exits when heat falls to COOLDOWN_EXIT (default 30%)
    AND at least COOLDOWN_DURATION seconds have passed

The governor state is piggybacked on heartbeat pings so the phone
can display heat level and cooldown countdown in real time.

This is a soft safety layer — the hard safety is the dead man's switch.
The governor exists to pace sessions, not to prevent hardware damage.
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field

from . import config

log = logging.getLogger("signal_bridge.governor")


@dataclass
class GovernorConfig:
    """Per-user governor config (overrides server defaults)."""
    heat_rate: float = config.GOVERNOR_HEAT_RATE
    cool_rate: float = config.GOVERNOR_COOL_RATE
    cooldown_threshold: float = config.GOVERNOR_COOLDOWN_THRESHOLD
    cooldown_exit: float = config.GOVERNOR_COOLDOWN_EXIT
    cooldown_duration: float = config.GOVERNOR_COOLDOWN_DURATION
    enabled: bool = config.GOVERNOR_ENABLED


@dataclass
class GovernorState:
    """Per-user heat tracking state."""
    heat: float = 0.0               # 0..100
    current_intensity: float = 0.0  # last known intensity (0..1)
    in_cooldown: bool = False
    cooldown_entered_at: float = 0.0
    cooldown_count: int = 0         # total cooldowns this session
    last_tick: float = field(default_factory=time.time)
    cfg: GovernorConfig = field(default_factory=GovernorConfig)

    def tick(self) -> None:
        """Update heat based on elapsed time since last tick."""
        now = time.time()
        dt = now - self.last_tick
        self.last_tick = now

        if dt <= 0 or dt > 10:
            # Sanity: skip huge jumps (e.g., system clock change)
            return

        if self.in_cooldown:
            # During cooldown: always dissipate, intensity is forced to 0
            self.heat -= self.cfg.cool_rate * dt
            self.heat = max(0.0, self.heat)

            # Check cooldown exit conditions
            elapsed = now - self.cooldown_entered_at
            if (self.heat <= self.cfg.cooldown_exit
                    and elapsed >= self.cfg.cooldown_duration):
                self.in_cooldown = False
                log.info(
                    f"Cooldown ended: heat={self.heat:.1f}% "
                    f"after {elapsed:.0f}s"
                )
        else:
            # Normal operation: accumulate or dissipate
            net = (self.current_intensity * self.cfg.heat_rate
                   - self.cfg.cool_rate)
            # Only dissipate if there's actual heat to lose
            if net < 0 and self.heat <= 0:
                return
            self.heat += net * dt
            self.heat = max(0.0, min(100.0, self.heat))

            # Check cooldown trigger
            if self.heat >= self.cfg.cooldown_threshold:
                self.in_cooldown = True
                self.cooldown_entered_at = now
                self.cooldown_count += 1
                self.current_intensity = 0.0
                log.warning(
                    f"Cooldown triggered: heat={self.heat:.1f}% "
                    f"(cooldown #{self.cooldown_count})"
                )

    def record_command(self, intensity: float) -> None:
        """Record that a command was sent at a given intensity."""
        self.current_intensity = max(0.0, min(1.0, intensity))

    def record_stop(self) -> None:
        """Record that devices were stopped."""
        self.current_intensity = 0.0

    @property
    def cooldown_remaining(self) -> int:
        """Seconds remaining in cooldown (0 if not in cooldown)."""
        if not self.in_cooldown:
            return 0
        elapsed = time.time() - self.cooldown_entered_at
        # Time-based minimum
        time_remaining = max(0, self.cfg.cooldown_duration - elapsed)
        # Heat-based: estimate time to reach exit threshold
        if self.cfg.cool_rate > 0:
            heat_remaining = max(
                0,
                (self.heat - self.cfg.cooldown_exit)
                / self.cfg.cool_rate,
            )
        else:
            heat_remaining = 0
        return int(max(time_remaining, heat_remaining))

    @property
    def predicted_seconds(self) -> int | None:
        """
        At current intensity, how many seconds until cooldown triggers?
        Returns None if intensity is 0 or heat is dissipating.
        """
        if self.in_cooldown or self.current_intensity <= 0:
            return None
        net = (self.current_intensity * self.cfg.heat_rate
               - self.cfg.cool_rate)
        if net <= 0:
            return None  # heat is stable or dissipating
        remaining_heat = self.cfg.cooldown_threshold - self.heat
        if remaining_heat <= 0:
            return 0
        return int(remaining_heat / net)

    def to_dict(self) -> dict:
        """Serialize for piggybacking on heartbeat pings."""
        return {
            "heat_pct": round(self.heat, 1),
            "in_cooldown": self.in_cooldown,
            "cooldown_remaining": self.cooldown_remaining,
            "cooldown_count": self.cooldown_count,
            "predicted_seconds": self.predicted_seconds,
        }


class Governor:
    """
    Central governor managing per-user heat state.

    Usage:
      - Call tick(user_id) on every heartbeat to update heat
      - Call check(user_id) before sending commands — returns (allowed, reason)
      - Call record_command(user_id, intensity) after sending a command
      - Call record_stop(user_id) when devices are stopped
      - Call get_state(user_id) to get current state for heartbeat piggyback
    """

    def __init__(self):
        self._states: dict[str, GovernorState] = {}

    def _get(self, user_id: str) -> GovernorState:
        if user_id not in self._states:
            self._states[user_id] = GovernorState()
        return self._states[user_id]

    def tick(self, user_id: str) -> None:
        """Advance the heat model. Call on every heartbeat."""
        self._get(user_id).tick()

    def check(self, user_id: str) -> tuple[bool, str]:
        """
        Check if a command is allowed.
        Returns (allowed: bool, reason: str).
        """
        state = self._get(user_id)
        if not state.cfg.enabled:
            return True, ""

        if state.in_cooldown:
            remaining = state.cooldown_remaining
            return False, (
                f"Cooldown active ({remaining}s remaining). "
                f"Session heat reached {state.cfg.cooldown_threshold:.0f}%. "
                f"Wait for cooldown to complete before sending more commands."
            )
        return True, ""

    def apply_user_config(self, user_id: str, effective: dict) -> None:
        """Apply per-user config overrides from the database."""
        state = self._get(user_id)
        state.cfg = GovernorConfig(
            enabled=bool(effective.get("governor_enabled", True)),
            heat_rate=float(effective.get("heat_rate", config.GOVERNOR_HEAT_RATE)),
            cool_rate=float(effective.get("cool_rate", config.GOVERNOR_COOL_RATE)),
            cooldown_threshold=float(effective.get("cooldown_threshold", config.GOVERNOR_COOLDOWN_THRESHOLD)),
            cooldown_exit=float(effective.get("cooldown_exit", config.GOVERNOR_COOLDOWN_EXIT)),
            cooldown_duration=float(effective.get("cooldown_duration", config.GOVERNOR_COOLDOWN_DURATION)),
        )
        log.info(f"Applied user config for {user_id}: heat_rate={state.cfg.heat_rate}, "
                 f"cool_rate={state.cfg.cool_rate}, threshold={state.cfg.cooldown_threshold}")

    def record_command(self, user_id: str, intensity: float) -> None:
        """Record that a command was dispatched."""
        self._get(user_id).record_command(intensity)

    def record_stop(self, user_id: str) -> None:
        """Record that devices were stopped."""
        self._get(user_id).record_stop()

    def get_state(self, user_id: str) -> dict:
        """Get governor state dict for heartbeat piggyback."""
        return self._get(user_id).to_dict()

    def remove_user(self, user_id: str) -> None:
        """Clean up when a user disconnects."""
        self._states.pop(user_id, None)


# Singleton
governor = Governor()
