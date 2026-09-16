#!/usr/bin/env python3
"""The climate chamber agent.

    python3 chamber.py

One command. The first run registers this Pi in Firestore and claims a chamber
document; every run after that re-claims the same one and carries on. Nothing
to install — Python 3 standard library only.

The shape of the thing:

    hardware.py ──→ [ control loop, 1 Hz ] ──→ hardware.py
                           │  reads the setpoint from memory
                           │  hands its snapshot to ↓
                    [ sync thread, 10 s ] ←──→ Firestore

The control loop makes no network calls. Not "retries them" or "times out
quickly" — makes none. The sync thread is the only code with a socket, it runs
separately, and it is allowed to fail for days. Unplug the network and the
chamber holds its setpoint exactly as before; the dashboard simply goes grey.

That is the property worth protecting when editing this file. If a network
call ever appears inside the `while` loop below, the guarantee is gone.
"""

import os
import re
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone

import cloud
import control
import hardware

CONTROL_INTERVAL_S = float(os.environ.get("CONTROL_INTERVAL_S", "1.0"))
PRINT_INTERVAL_S = float(os.environ.get("PRINT_INTERVAL_S", "10.0"))

HOSTNAME = socket.gethostname()
CHAMBER_ID = os.environ.get("CHAMBER_ID") or "chamber-" + (
    re.sub(r"[^a-z0-9-]+", "-", HOSTNAME.lower()).strip("-") or "pi"
)
CHAMBER_NAME = os.environ.get("CHAMBER_NAME") or f"תא אקלים {HOSTNAME}"


def build_reports(hw, tuning, decision, desired, setpoint, sync, readings, ambient, inside_avg):
    """Shape the two documents the dashboard reads.

    Split by how fast they change: `reported` is state and actuator read-back,
    `telemetry/current` is readings. Keeping them apart is what lets a browser
    subscribe to one without paying for every wobble of the other.
    """
    amb_t, amb_rh, amb_online = ambient
    age = setpoint.age_s()

    reported = {
        "mode": desired.get("mode", "AUTO"),
        "demand": decision.demand,
        "calculatedTargetTemp": round(decision.target_c, 1),
        # Honesty flags. Every consumer of this document can tell whether the
        # agent is actually in touch with the cloud, whether a setpoint is
        # stale, and whether anything below was measured or invented.
        "cloudOnline": sync.online,
        "setpointAgeS": round(age, 1) if age is not None else -1.0,
        # How long a setpoint change can sit before this agent notices it.
        # Published rather than hardcoded in the HTML so the dashboard can tell
        # the operator how long to expect to wait, and stay truthful if the
        # interval is retuned — the same reason simModel is published.
        "syncIntervalS": cloud.SYNC_INTERVAL_S,
        # The two timers behind `held`, so the dashboard can explain a pause in
        # the operator's own numbers instead of quoting a default it hopes is
        # still true.
        "minOnS": tuning.min_on_s,
        "minOffS": tuning.min_off_s,
        "fault": decision.fault or "",
        "held": decision.held,
        "updatedAt": datetime.now(timezone.utc),
    }
    reported.update({k: (round(v, 3) if isinstance(v, float) else v)
                     for k, v in hw.state().items()})

    # The dashboard hides its "these numbers are a demo" note when simModel is
    # absent, which is how the note disappears by itself the moment real
    # hardware is wired. Publishing the rates the loop is *actually* applying,
    # rather than a constant in the HTML, is what stops the note drifting out
    # of step with the code.
    if hw.sensors == "SIMULATED":
        reported["simModel"] = {
            "ratePerSecondC": hardware.RATE_C_PER_S,
            "intervalS": CONTROL_INTERVAL_S,
            "deadbandC": tuning.band_on_c,
        }

    telemetry = {
        "ambient": {"temp": round(amb_t, 1), "rh": round(amb_rh, 1), "online": amb_online},
        "sensors": [
            {"id": i + 1, "label": hw.label(i), "temp": round(t, 1),
             "rh": round(rh, 1), "online": ok}
            for i, (t, rh, ok) in enumerate(readings)
        ],
        "avgTemp": round(inside_avg, 1) if inside_avg is not None else 0.0,
        "avgRH": round(sum(r[1] for r in readings) / len(readings), 1) if readings else 0.0,
    }
    return reported, telemetry


def main():
    tuning = control.Tuning.from_env()
    hw = hardware.Hardware()
    controller = control.Controller(tuning)
    setpoint = cloud.Setpoint()
    connection = cloud.Cloud(CHAMBER_ID, CHAMBER_NAME, hardware.SENSOR_COUNT,
                             id_is_explicit=bool(os.environ.get("CHAMBER_ID")))

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    sync = cloud.SyncWorker(connection, setpoint, stop)

    print(f"{cloud.AGENT_VERSION}  host={HOSTNAME}  ip={cloud.local_ip()}")
    print(f"chamber: {CHAMBER_ID}   hardware: {hw.backend}   control: {CONTROL_INTERVAL_S}s")
    print(f"bands: on ±{tuning.band_on_c}°C / off ±{tuning.band_off_c}°C   "
          f"min on {tuning.min_on_s:.0f}s / off {tuning.min_off_s:.0f}s   "
          f"cutout {tuning.max_inside_c:.0f}°C")
    print(f"dashboard: https://{cloud.PROJECT_ID}.web.app/chamber.html?id={CHAMBER_ID}")
    print("(Ctrl-C to stop)\n")

    # Started after the banner so its first message, which is often "offline",
    # lands below the configuration it is reporting against.
    sync.start()

    last_print = 0.0
    last_cycle = time.monotonic()

    try:
        while not stop.is_set():
            now = time.monotonic()
            dt_s, last_cycle = now - last_cycle, now

            desired = setpoint.current()
            hw.set_sim_ambient(desired.get("simAmbient"))

            ambient = hw.read_ambient()
            readings = hw.read_inside()

            live = [r[0] for r in readings if r[2] and control.valid_reading(r[0], tuning)]
            inside_avg = sum(live) / len(live) if live else None

            decision = controller.step(now, desired, inside_avg, ambient[0])
            hw.apply(decision, dt_s)

            sync.offer(*build_reports(hw, tuning, decision, desired, setpoint, sync,
                                      readings, ambient, inside_avg))

            if now - last_print >= PRINT_INTERVAL_S:
                last_print = now
                inside = f"{inside_avg:5.1f}" if inside_avg is not None else "  ?  "
                note = f"  !{decision.fault}" if decision.fault else ("  (held)" if decision.held else "")
                print(f"[{datetime.now():%H:%M:%S}] out {ambient[0]:5.1f}°C  in {inside}°C  "
                      f"target {decision.target_c:5.1f}°C  -> {decision.demand:11s} "
                      f"[{desired.get('mode', 'AUTO')}] "
                      f"{'cloud' if sync.online else 'LOCAL'}{note}")

            stop.wait(CONTROL_INTERVAL_S)

    finally:
        print("\nStopping: actuators off.")
        hw.cleanup()


if __name__ == "__main__":
    sys.exit(main())
