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


def _flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# How each output is driven. Getting this wrong damages hardware, so both
# settings are explicit rather than guessed.
#
# TYPE — "relay" (default) or "pwm".
#
#   A mechanical relay MUST NOT be given a PWM signal. At 1 Hz it would be
#   switched 86,400 times a day against a typical rating of 100,000 mechanical
#   operations: dead in a day and a half. Relays get a plain on/off output and
#   are protected further by MIN_ON_S / MIN_OFF_S in control.py.
#
#   Use "pwm" only for something that actually modulates: a 4-wire fan's PWM
#   input, a logic-level MOSFET, or a solid-state relay (an SSR has no moving
#   parts and is happy at 1 Hz, which is what slow-PWM heat control needs).
#
# ACTIVE_LOW — true for most of the cheap opto-isolated relay boards, where
#   the coil energises when the input is pulled LOW. Default true because that
#   is what the common blue SRD-05VDC modules do, and because being wrong in
#   this direction fails safe: a board that is really active-HIGH simply never
#   switches on, which you notice immediately and harmlessly. Being wrong the
#   other way leaves a heater energised.
#
#   VERIFY IT ANYWAY, with the mains side disconnected — see pi/README.md.
# ---------------------------------------------------------------------------

#   "led" is the third type: electrically identical to "relay" (a digital pin,
#   never PWM) but it defaults ACTIVE_LOW the other way, because an LED wired
#   GPIO -> resistor -> anode, cathode -> GND lights when the pin goes HIGH.
#   Encoding that in the type name is what stops someone pairing an LED with a
#   relay board's polarity and wondering why it is lit whenever it should be
#   dark.

FAN_TYPE = (os.environ.get("FAN_TYPE") or "relay").strip().lower()
HEATER_TYPE = (os.environ.get("HEATER_TYPE") or "relay").strip().lower()

# Relay boards idle HIGH, LEDs and MOSFET gates idle LOW. Defaulting per type
# means the common wiring needs no flag at all; the env var still overrides.
_DEFAULT_ACTIVE_LOW = {"relay": True, "led": False, "pwm": False}

FAN_ACTIVE_LOW = _flag("FAN_ACTIVE_LOW", _DEFAULT_ACTIVE_LOW.get(FAN_TYPE, False))
HEATER_ACTIVE_LOW = _flag("HEATER_ACTIVE_LOW", _DEFAULT_ACTIVE_LOW.get(HEATER_TYPE, False))

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

        # TWO independent facts, which an earlier version wrongly collapsed
        # into one `backend` flag:
        #
        #   sensors  where the readings come from
        #   outputs  whether any real pin gets driven
        #
        # Conflating them made the useful middle case impossible — a demo rig
        # where the chamber is a model but an LED and a fan really do switch,
        # so you can watch the control decision happen on a bench with no
        # probes and no heater. All four combinations are legitimate:
        #
        #   SIMULATED + NONE   pure software demo, what you get with no wiring
        #   SIMULATED + GPIO   demo rig: modelled degrees, real LED and fan
        #   REAL      + GPIO   an actual chamber
        #   REAL      + NONE   monitoring only, no actuators fitted
        #
        # `sensors` is hardcoded to SIMULATED because no probe driver exists
        # yet: read_ambient() and read_inside() below still return model
        # values. Flip it in the same commit that makes them read hardware,
        # not before — it is what drives `reported.simulated`, and therefore
        # whether the dashboard warns that the numbers are invented.
        self.sensors = "SIMULATED"
        self.outputs = "NONE"

        if FAN_GPIO or HEATER_GPIO:
            try:
                if FAN_GPIO:
                    self._pins["fan"] = self._open(
                        "fan", int(FAN_GPIO), FAN_TYPE, FAN_ACTIVE_LOW, FAN_PWM_HZ)
                if HEATER_GPIO:
                    # 1 Hz slow-PWM is for an SSR, which switches at zero
                    # crossing and has nothing to wear out. A mechanical relay
                    # must never get here — see _open().
                    self._pins["heater"] = self._open(
                        "heater", int(HEATER_GPIO), HEATER_TYPE, HEATER_ACTIVE_LOW, 1)
                self.outputs = "GPIO"
            except Exception as exc:        # noqa: BLE001 — never block startup on wiring
                print(f"hardware: no GPIO, outputs disabled ({exc})")
                self._pins.clear()

        # Simulated chamber state. Used whenever `sensors` is SIMULATED, which
        # is independent of whether pins are driven.
        self._inside_c = AMBIENT_C
        self._ambient_c = AMBIENT_C

    @property
    def backend(self):
        """One-line summary for the startup banner and the logs."""
        return f"{self.sensors} sensors / {self.outputs} outputs"

    @staticmethod
    def _open(name, pin, kind, active_low, hz):
        """Open one output, refusing the combination that destroys hardware.

        `initial_value=False` is the whole safety argument here: gpiozero
        drives the pin to the inactive level the instant the device is
        constructed, so an active-low relay board is pushed HIGH — coil
        released — before anything else happens.

        It cannot help before this line runs, though. From power-on until the
        agent starts, the pin is a floating input, and a floating input on an
        active-low board may read LOW and energise the relay. Only a physical
        10 kΩ pull-UP to 3.3V fixes that. Software cannot.
        """
        from gpiozero import OutputDevice, PWMOutputDevice

        if kind == "pwm":
            dev = PWMOutputDevice(pin, active_high=not active_low,
                                  initial_value=0.0, frequency=hz)
        elif kind in ("relay", "led"):
            # Electrically the same call. They are separate names because the
            # type picks the ACTIVE_LOW default, and because "led" in a service
            # file says at a glance that the pin drives an indicator rather
            # than something that can cook the contents of a chamber.
            dev = OutputDevice(pin, active_high=not active_low,
                               initial_value=False)
        else:
            raise ValueError(
                f"{name.upper()}_TYPE={kind!r} is not understood; "
                "use 'relay', 'led' or 'pwm'")

        print(f"hardware: {name} on GPIO{pin} as {kind}, "
              f"active-{'LOW' if active_low else 'HIGH'}"
              + (f", {hz} Hz" if kind == "pwm" else ""))
        return dev

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
        jitter = (random.random() - 0.5) * 0.05 if self.sensors == "SIMULATED" else 0.0
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
            if pin is None:
                continue
            # A relay is on or off; handing it a fraction would silently round
            # and make the reported state a lie. `active_high` set at open time
            # is what maps this to the right electrical level, so nothing here
            # needs to know the board's polarity.
            pin.value = value if hasattr(pin, "frequency") else bool(value >= 0.5)

        # Keyed on `sensors`, NOT on whether pins exist. Driving an LED must
        # not stop the modelled temperature from moving — that combination is
        # the entire point of the demo rig.
        if self.sensors == "SIMULATED":
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
        if self.sensors != "SIMULATED":
            return
        self._ambient_c = AMBIENT_C if temp_c is None else float(temp_c)

    def state(self):
        """What the outputs really are — this is what gets reported.

        `fanControlled` is the honest bit. A fan wired straight to pin 4/6 sits
        on the 5V rail and runs at full speed no matter what `fan` says, so it
        is reported as 1.0 and flagged uncontrolled. Once it goes through a
        relay the command IS the state and the flag flips. Reporting the
        request as though it were the state is how a dashboard ends up lying.

        `simulated` tracks the readings, not the outputs: on a demo rig the
        degrees are invented even though the LED is genuinely lit, and it is
        the degrees the dashboard must warn about.
        """
        fan_controlled = "fan" in self._pins
        return {
            "fanTopSpeed": self._outputs["fan"] if fan_controlled else 1.0,
            "fanBottomSpeed": self._outputs["fan"] if fan_controlled else 1.0,
            "shutterTopOpen": self._outputs["fan"],
            "shutterBottomOpen": self._outputs["fan"],
            "heaterIntensity": self._outputs["heater"],
            "fanControlled": fan_controlled,
            "heaterControlled": "heater" in self._pins,
            "simulated": self.sensors == "SIMULATED",
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
                pin.off()           # inactive level, whatever the polarity
                pin.close()
            except Exception:       # noqa: BLE001 — shutdown must not raise
                pass
        self._outputs = {"fan": 0.0, "heater": 0.0}
