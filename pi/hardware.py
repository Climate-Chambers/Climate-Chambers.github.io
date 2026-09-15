#!/usr/bin/env python3
"""Everything that touches a pin.

This is the only file that knows a GPIO exists, and the only one to edit when
real sensors and actuators are wired. `control.py` decides, `cloud.py` reports,
and neither of them can tell whether anything below is real.

It runs in two backends, chosen automatically:

    * REAL       — gpiozero is importable and the pins are configured
    * SIMULATED  — anything else, including a laptop

The simulated backend keeps the old demo dynamics (half a degree a second
toward the target) so the dashboard and the 3D model behave exactly as they did
before any hardware existed. Every simulated value is flagged as such all the
way up to the browser; nothing here is ever allowed to look like a measurement.

WIRING, as of today
    The fan on this Pi is connected to physical pin 4 (5V) and pin 6 (GND).
    That is the 5V rail itself, not a GPIO — so the fan spins at full speed
    whenever the Pi has power and NOTHING can turn it off. FAN_GPIO is
    therefore unset by default, and `state()` reports fanControlled=False so
    the dashboard says "always on" instead of drawing a fan it cannot command.

    To gain control, move the fan's negative lead off pin 6 and onto the drain
    of a logic-level MOSFET (source to GND, gate to the pin named by FAN_GPIO
    through 220Ω, 10kΩ gate-to-GND pull-down so a floating pin means OFF), or
    fit a 4-wire fan and drive its PWM lead from FAN_GPIO directly.
"""

import os
import random

# Pins are BCM numbers, not physical positions. Unset means "not wired", which
# is honest and safe: the agent reports the actuator as uncontrolled rather
# than pretending a command took effect.
FAN_GPIO = os.environ.get("FAN_GPIO")
HEATER_GPIO = os.environ.get("HEATER_GPIO")
FAN_PWM_HZ = int(os.environ.get("FAN_PWM_HZ", "25000"))   # 25 kHz: the 4-wire fan standard

SENSOR_COUNT = int(os.environ.get("SENSOR_COUNT", "5"))
SENSOR_LABELS = ["עליון שמאל", "עליון ימין", "מרכז התא", "תחתון שמאל", "תחתון ימין"]

AMBIENT_C = float(os.environ.get("AMBIENT_C", "21.0"))
AMBIENT_RH = float(os.environ.get("AMBIENT_RH", "45.0"))
INSIDE_RH = float(os.environ.get("INSIDE_RH", "50.0"))
RATE_C_PER_S = float(os.environ.get("RATE_C_PER_S", "0.5"))


def cpu_temp():
    """The one real sensor every Pi already has.

    Published as a diagnostic only. It is the board's temperature, not the
    chamber's, and it must never stand in for a chamber probe.
    """
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="utf-8") as fh:
            return round(int(fh.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


class Hardware:
    """Sensors in, actuators out.

    Four methods and a label. Replace the bodies, keep the signatures, and
    nothing else in the agent needs to change.
    """

    def __init__(self):
        self._outputs = {"fan": 0.0, "heater": 0.0}
        self._pins = {}
        self.backend = "SIMULATED"

        if FAN_GPIO or HEATER_GPIO:
            try:
                from gpiozero import PWMOutputDevice
                if FAN_GPIO:
                    self._pins["fan"] = PWMOutputDevice(int(FAN_GPIO), frequency=FAN_PWM_HZ)
                if HEATER_GPIO:
                    # Slow PWM for a solid-state relay: a mains SSR switches at
                    # zero crossing, so anything faster than a few hertz is
                    # meaningless to it.
                    self._pins["heater"] = PWMOutputDevice(int(HEATER_GPIO), frequency=1)
                self.backend = "REAL"
            except Exception as exc:        # noqa: BLE001 — never block startup on wiring
                print(f"hardware: falling back to simulation ({exc})")
                self._pins.clear()

        # Simulated chamber state, used only by the SIMULATED backend.
        self._inside_c = AMBIENT_C
        self._ambient_c = AMBIENT_C

    # ---- sensors ---------------------------------------------------------

    def read_ambient(self):
        """Outside air as (temp_c, rh, online).

        REPLACE: read the ambient probe, e.g. an SHT31 on I2C bus 1. Return
        online=False when it does not answer — control.py treats a missing
        ambient as a degraded state, not as zero degrees.
        """
        return self._ambient_c, AMBIENT_RH, True

    def read_inside(self):
        """The in-chamber probes as a list of (temp_c, rh, online).

        REPLACE: read the real probes. Report online=False for any that fails
        to respond; the dashboard surfaces that as a dropout on the 3D model
        rather than quietly averaging a dead sensor's last value.
        """
        jitter = 0.0 if self.backend == "REAL" else (random.random() - 0.5) * 0.05
        return [(self._inside_c + jitter, INSIDE_RH, True) for _ in range(SENSOR_COUNT)]

    def label(self, index):
        return SENSOR_LABELS[index] if index < len(SENSOR_LABELS) else f"חיישן {index + 1}"

    # ---- actuators -------------------------------------------------------

    def apply(self, decision, dt_s):
        """Drive the outputs from a Decision, then advance the simulation.

        The simulation step lives here rather than in control.py on purpose:
        control.py must not contain a single line that behaves differently
        depending on whether the hardware is real.
        """
        fan = 1.0 if decision.venting else 0.0
        heater = 1.0 if decision.heating else 0.0

        for name, value in (("fan", fan), ("heater", heater)):
            self._outputs[name] = value
            pin = self._pins.get(name)
            if pin is not None:
                pin.value = value

        if self.backend == "SIMULATED":
            self._simulate(decision, dt_s)

    def _simulate(self, decision, dt_s):
        """Half a degree a second toward the target, and that is the whole model.

        Two limits are all the physics it has: it does not sail past the
        target, and ventilation cannot pull the chamber below the outside air,
        because fans only exchange air. Anything more would be a thermal model
        nobody has validated, dressed up as a measurement.
        """
        delta = RATE_C_PER_S * dt_s
        if decision.heating:
            self._inside_c = min(self._inside_c + delta, decision.target_c)
        elif decision.venting:
            floor = max(self._ambient_c, decision.target_c)
            self._inside_c = max(self._inside_c - delta, floor)

    def set_sim_ambient(self, temp_c):
        """The dashboard's outside-temperature override.

        A relative target only demonstrates anything if outside can move, and
        there is no climate room around a demo. Ignored entirely by the REAL
        backend — an operator must never be able to fake a measurement.
        """
        if self.backend == "SIMULATED" and temp_c is not None:
            self._ambient_c = float(temp_c)
        elif self.backend == "SIMULATED":
            self._ambient_c = AMBIENT_C

    def state(self):
        """What the outputs really are — this is what gets reported.

        `fanControlled` is the honest bit: with the fan on pin 4/6 it is wired
        straight to 5V, so it is running at full speed no matter what `fan`
        says. Reporting the request as though it were the state is how a
        dashboard ends up lying.
        """
        controlled = "fan" in self._pins
        return {
            "fanTopSpeed": self._outputs["fan"] if controlled else 1.0,
            "fanBottomSpeed": self._outputs["fan"] if controlled else 1.0,
            "shutterTopOpen": self._outputs["fan"],
            "shutterBottomOpen": self._outputs["fan"],
            "heaterIntensity": self._outputs["heater"],
            "fanControlled": controlled,
            "heaterControlled": "heater" in self._pins,
            "simulated": self.backend == "SIMULATED",
            "piCpuTemp": cpu_temp() or 0.0,
        }

    def cleanup(self):
        """Safe state on the way out.

        gpiozero releases the pin on close, which leaves it floating — so the
        pull-down resistor described at the top of this file is what actually
        guarantees the heater is off. Software cannot promise this alone.
        """
        for name, pin in self._pins.items():
            try:
                pin.value = 0.0
                pin.close()
            except Exception:       # noqa: BLE001 — shutdown must not raise
                pass
        self._outputs = {"fan": 0.0, "heater": 0.0}
