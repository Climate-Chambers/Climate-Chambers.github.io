#!/usr/bin/env python3
"""The control law.

This file decides what the chamber should do. It is deliberately the most
boring file in the project: pure functions and one small state machine, with
no network, no GPIO, no threads and no clock of its own — every time value is
passed in. That is what makes it possible to test the whole control law on a
laptop in milliseconds, and what makes it obvious by inspection that a network
outage cannot reach it.

The order of the checks below is the safety argument, so do not reorder them:

    1. faults      — a bad sensor or an over-temperature trip wins outright
    2. mode        — what the operator asked for
    3. hysteresis  — where the setpoint actually is
    4. min on/off  — protect the relays and motors from short-cycling

A fault turns actuators OFF immediately and bypasses step 4. Anything may
always stop; only starting has to wait.
"""

import os
from dataclasses import dataclass
from typing import Optional

MODES = ("AUTO", "HEATING", "VENTILATION", "OFF")


@dataclass(frozen=True)
class Tuning:
    """Everything a technician might want to change without reading code.

    The defaults are conservative on purpose: wide bands and long timers give
    a system that is unmistakably stable, which is the right place to start
    tuning from. Narrow them once there is real data.
    """

    band_on_c: float = 0.5       # deviation at which an actuator starts
    band_off_c: float = 0.1      # deviation at which it stops again
    min_on_s: float = 30.0       # once running, run at least this long
    min_off_s: float = 60.0      # once stopped, stay stopped at least this long
    max_inside_c: float = 60.0   # hard cutout — software backstop, not a thermostat
    reset_margin_c: float = 5.0  # how far below the cutout before a reset is allowed
    sensor_min_c: float = -40.0  # a reading outside this range is a broken sensor,
    sensor_max_c: float = 125.0  # not a cold or hot chamber
    max_offset_c: float = 10.0   # ceiling on a relative setpoint

    @classmethod
    def from_env(cls):
        """Read overrides from the environment, ignoring anything unparseable.

        A typo in a service file must not leave the chamber with a garbage
        setpoint band, so a bad value falls back to the default rather than
        crashing the agent or being silently coerced to zero.
        """
        values = {}
        for field in cls.__dataclass_fields__:
            raw = os.environ.get(field.upper())
            if raw is None:
                continue
            try:
                values[field] = float(raw)
            except ValueError:
                print(f"control: ignoring {field.upper()}={raw!r} (not a number)")
        return cls(**values)


@dataclass(frozen=True)
class Decision:
    """What the controller concluded this cycle, and why."""

    heating: bool = False
    venting: bool = False
    demand: str = "IDLE"           # HEATING | VENTILATION | IDLE
    target_c: float = 0.0
    fault: Optional[str] = None    # None when healthy; a short reason when not
    held: bool = False             # True when a min on/off timer is holding a change back


def valid_reading(value, tuning):
    """True when a number could plausibly have come from a working probe.

    A disconnected DS18B20 reads 85 °C; a shorted one reads 0 or -127. Neither
    is a temperature, and treating them as one is how a chamber cooks its
    contents while the dashboard shows a calm green number.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value != value:          # NaN — the only value not equal to itself
        return False
    return tuning.sensor_min_c <= value <= tuning.sensor_max_c


def resolve_target(desired, ambient_c, tuning):
    """The setpoint in absolute degrees, whichever way the operator expressed it.

    A relative target ("+10 °C above outside") is resolved against the ambient
    reading every cycle, so the target follows the weather. It is clamped
    upward only: ventilation exchanges air with outdoors, so it can never pull
    the chamber below ambient, and a negative offset would be a setpoint that
    is unreachable by construction.
    """
    try:
        value = float(desired.get("targetValue", 0.0))
    except (TypeError, ValueError):
        value = 0.0

    if desired.get("targetMode") == "absolute":
        return value
    offset = max(0.0, min(tuning.max_offset_c, value))
    return ambient_c + offset


class Controller:
    """The chamber's state machine.

    It holds exactly three pieces of memory — what the actuators are doing,
    when that last changed, and whether the over-temperature latch has tripped.
    Everything else is recomputed from the inputs each cycle.
    """

    def __init__(self, tuning=None):
        self.tuning = tuning or Tuning()
        self._heating = False
        self._venting = False
        self._changed_at = float("-inf")   # so the first change is never held
        self._tripped = False

    @property
    def tripped(self):
        return self._tripped

    def step(self, now, desired, inside_c, ambient_c):
        """Advance one cycle. `now` is a monotonic clock reading in seconds.

        Returns a Decision. The caller drives the hardware from it and then
        reports what the hardware actually did — never what was asked for.
        """
        tuning = self.tuning
        mode = desired.get("mode") if desired.get("mode") in MODES else "AUTO"

        # ---- 1. faults ---------------------------------------------------
        # Setting the chamber to OFF is the reset: it is the one action that
        # is unambiguously a human deciding this chamber should do nothing.
        if mode == "OFF" and self._tripped and valid_reading(inside_c, tuning) \
                and inside_c < tuning.max_inside_c - tuning.reset_margin_c:
            self._tripped = False
            print("control: over-temperature latch cleared")

        if not valid_reading(ambient_c, tuning):
            ambient_c = inside_c if valid_reading(inside_c, tuning) else 0.0

        if not valid_reading(inside_c, tuning):
            return self._fail_safe(now, "no valid chamber sensor", ambient_c)

        if inside_c >= tuning.max_inside_c:
            if not self._tripped:
                print(f"control: OVER-TEMPERATURE {inside_c:.1f}°C — actuators latched off")
            self._tripped = True

        if self._tripped:
            return self._fail_safe(
                now, "over-temperature latch (set mode OFF to reset)", ambient_c
            )

        # ---- 2. mode + 3. hysteresis -------------------------------------
        target_c = resolve_target(desired, ambient_c, tuning)
        want_heating, want_venting = self._want(mode, target_c, inside_c)

        # ---- 4. min on/off ------------------------------------------------
        heating, venting, held = self._hold(now, want_heating, want_venting)

        return Decision(
            heating=heating,
            venting=venting,
            demand="HEATING" if heating else "VENTILATION" if venting else "IDLE",
            target_c=target_c,
            held=held,
        )

    # -- internals ---------------------------------------------------------

    def _want(self, mode, target_c, inside_c):
        """What the setpoint asks for, before any timer gets a say.

        The two bands are asymmetric, and that asymmetry is the whole point:
        an actuator starts when the chamber has drifted `band_on_c` away and
        stops only once it is back within `band_off_c`. A single symmetric
        deadband makes the actuator chatter across one threshold; two
        thresholds give it somewhere to sit.
        """
        if mode == "OFF":
            return False, False
        if mode == "HEATING":
            return True, False
        if mode == "VENTILATION":
            return False, True

        tuning = self.tuning
        diff = target_c - inside_c

        if self._heating:
            return diff > tuning.band_off_c, False
        if self._venting:
            return False, -diff > tuning.band_off_c
        if diff > tuning.band_on_c:
            return True, False
        if -diff > tuning.band_on_c:
            return False, True
        return False, False

    def _hold(self, now, want_heating, want_venting):
        """Enforce the minimum on and off times.

        A thermostat that toggles a relay every few seconds destroys the relay
        and the motor behind it. This is the cheapest possible protection:
        once something changes state it keeps that state for a while, even if
        the setpoint says otherwise. The chamber's thermal mass means the cost
        is a fraction of a degree of overshoot.
        """
        want = (want_heating, want_venting)
        have = (self._heating, self._venting)
        if want == have:
            return have[0], have[1], False

        running = self._heating or self._venting
        floor = self.tuning.min_on_s if running else self.tuning.min_off_s
        if now - self._changed_at < floor:
            return have[0], have[1], True

        self._heating, self._venting = want
        self._changed_at = now
        return want[0], want[1], False

    def _fail_safe(self, now, reason, ambient_c):
        """Everything off, immediately, bypassing the min-on timer.

        Stopping is always allowed. The timers exist to stop actuators being
        started and restarted needlessly, and no protection is worth holding a
        heater on for.
        """
        if self._heating or self._venting:
            self._heating = self._venting = False
            self._changed_at = now
        return Decision(demand="IDLE", target_c=ambient_c, fault=reason)
