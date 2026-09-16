# Raspberry Pi

To try it:

```bash
python3 chamber.py
```

To set up a Pi from scratch — one command, start to finish:

```bash
sudo bash -c "$(curl -fsSL https://climate-chambers.github.io/pi/setup.sh)"
```

It asks for the chamber name, then downloads the agent, installs it as a
service that starts at boot, enables the hardware watchdog and starts it.
Press Enter at the prompt to accept the default name.

The first run registers this Pi in Firestore and claims a chamber document;
every run after that re-claims the same one. No dependencies — Python 3
standard library only, so there is no venv on the Pi and no wheel to rebuild
after an OS upgrade.

## The files

| File | Lines | Job | Touches |
|---|---|---|---|
| [`chamber.py`](chamber.py) | 166 | entry point; wires the other three together and runs the control loop | — |
| [`control.py`](control.py) | 245 | the control law: hysteresis, minimum on/off times, faults | nothing |
| [`hardware.py`](hardware.py) | 328 | sensors and actuators — **the file to edit when wiring** | GPIO |
| [`cloud.py`](cloud.py) | 520 | auth, Firestore REST, the sync thread, on-disk state | network |
| [`setup.sh`](setup.sh) | 230 | download, prompt, systemd unit, watchdog. Run once, with sudo | — |

The split is not cosmetic. `control.py` imports no I/O of any kind and takes
its clock as an argument, so the entire control law can be exercised on a
laptop in milliseconds:

```python
import control
c = control.Controller(control.Tuning())
c.step(now=0.0, desired={"mode": "AUTO", "targetMode": "absolute",
                         "targetValue": 30.0}, inside_c=25.0, ambient_c=21.0)
# Decision(heating=True, venting=False, demand='HEATING', target_c=30.0, ...)
```

## The network is not in the control path

```
hardware.py ──→ [ control loop, 1 Hz ] ──→ hardware.py
                       │ reads the setpoint from memory
                       │ hands its snapshot to ↓
                [ sync thread, 10 s ] ←──→ Firestore
```

The control loop makes **no** network calls. Not "retries them" or "times them
out quickly" — makes none. `cloud.py` runs on its own thread, is allowed to
fail for days, and publishes on a slower cadence. Unplug the network and the
chamber holds its setpoint exactly as before; the dashboard simply goes grey.

Two things make that real rather than aspirational:

* **The setpoint is cached on disk** (`~/.chamber-agent/desired.json`). A Pi
  that loses power and comes back with no internet still knows what it was
  asked to do, instead of falling back to a hardcoded default.
* **`reported.setpointAgeS` and `reported.cloudOnline`** are published, so the
  dashboard can say *"running on a setpoint from four hours ago"* rather than
  implying the number on screen is fresh.

If a network call ever appears inside the `while` loop in `chamber.py`, this
guarantee is gone. That is the one invariant worth protecting when editing.

## The control law

Four steps, in this order — the order *is* the safety argument:

1. **Faults** — a bad sensor reading or an over-temperature trip wins outright.
2. **Mode** — `AUTO` / `HEATING` / `VENTILATION` / `OFF`, as the operator set it.
3. **Hysteresis** — an actuator starts when the chamber has drifted `band_on_c`
   from the setpoint and stops only once it is back inside `band_off_c`. The
   asymmetry is the point: one symmetric deadband makes the actuator chatter
   across a single threshold; two thresholds give it somewhere to sit.
4. **Minimum on/off times** — once something changes state it holds that state
   for a while, protecting the relay and the motor behind it.

A fault turns actuators off *immediately* and bypasses step 4. Anything may
always stop; only starting has to wait.

### Tunables

All are environment variables, all optional, all validated (a typo falls back
to the default and says so rather than being coerced to zero):

| Variable | Default | Meaning |
|---|---|---|
| `BAND_ON_C` | 0.5 | deviation at which an actuator starts |
| `BAND_OFF_C` | 0.1 | deviation at which it stops |
| `MIN_ON_S` | 30 | once running, run at least this long |
| `MIN_OFF_S` | 60 | once stopped, stay stopped at least this long |
| `MAX_INSIDE_C` | 60 | hard cutout — software backstop, **not** a thermostat |
| `MAX_OFFSET_C` | 10 | ceiling on a relative setpoint |
| `CONTROL_INTERVAL_S` | 1.0 | control loop period |
| `SYNC_INTERVAL_S` | 10 | how often the cloud thread publishes and polls |

The defaults are deliberately conservative: wide bands and long timers give a
system that is unmistakably stable, which is the right place to tune *from*.
The cost is overshoot — with `MIN_ON_S=30` a heater keeps running for up to 30 s
after reaching the setpoint. Narrow them once there is real data from real
probes; there is nothing to be gained from tuning against the simulator.

### Faults

| Condition | Response |
|---|---|
| No valid chamber reading (`None`, `NaN`, outside −40…125 °C) | everything off, `fault` published |
| `inside >= MAX_INSIDE_C` | everything off and **latched** |
| Latch reset | set the chamber to **OFF** in the dashboard, once it is 5 °C below the cutout |

A disconnected DS18B20 reads 85 °C and a shorted one reads −127. Neither is a
temperature, and treating them as one is how a chamber cooks its contents while
the dashboard shows a calm green number.

`MAX_INSIDE_C` is a software backstop and nothing more. A chamber with a real
heater still needs a mechanical over-temperature thermostat in series with it,
because no Python program can promise anything about a Pi that has hung.

## Wiring

### The fan as it is wired today

The fan is on physical **pin 4 (5V)** and **pin 6 (GND)**. Those are the 5V
rail and ground — not a GPIO — so the fan runs at full speed whenever the Pi
has power, and nothing in this repository can turn it off. That is safe (the
current comes from the power supply, not the SoC) but it is not control.

`hardware.py` knows this: with `FAN_GPIO` unset it publishes
`reported.fanControlled = false` and reports the fan as running, rather than
echoing back a command that had no effect.

### To actually control a 2-wire fan

A GPIO pin sources ~16 mA; a fan draws 100–250 mA. Move the fan's negative lead
off pin 6 and onto a logic-level MOSFET:

```
      +5V (pin 2 or 4)   or a separate +12V supply
            │
            ├───────┐
         [fan]      ▲ 1N5819    flyback diode, band toward +
            │       │
            └───────┘
            │ D
       ┌────┤
       │   ▐█▌ IRLZ44N / AO3400      logic-level: VGS(th) < 2.5V
GPIO ──/\/\─┤ G
      220Ω  │
            │ S
       ┌────┴────┬──── GND (pin 6)
      10kΩ       │     pull-down: a floating pin means OFF
       └─────────┘
```

Then `FAN_GPIO=18 python3 chamber.py`. A separate 12 V supply needs its
negative tied to the Pi's ground, or the MOSFET never sees a gate voltage.

### Or fit a 4-wire fan, which needs no transistor

| Wire | Usually | To |
|---|---|---|
| GND | black | pin 6 |
| +12V | yellow | external 12 V supply, **not** the Pi |
| Tach | green | a GPIO via a 10 kΩ pull-up to **3.3 V** (not 5 V) |
| PWM | blue | `FAN_GPIO` directly — it is a control input, not a load |

The tach wire gives real speed feedback, which is what lets the agent report
*"the fan was commanded on and is not turning"* — exactly the failure a climate
chamber has to catch.

### The demo rig — an LED for heat, a fan for cooling

The most useful configuration before any real chamber exists: the degrees are
**modelled**, but an LED and a fan really switch, so the control decision is
visible on a bench with no probes and no heater.

```bash
sudo HEATER_GPIO=17 HEATER_TYPE=led      FAN_GPIO=6 FAN_TYPE=relay      MIN_ON_S=5 MIN_OFF_S=5 RATE_C_PER_S=0.5      bash setup.sh
```

The agent prints what it understood, and the banner now names both halves
separately:

```
hardware: heater on GPIO17 as led, active-HIGH
hardware: fan on GPIO6 as relay, active-LOW
chamber: chamber-...   hardware: SIMULATED sensors / GPIO outputs
```

`SIMULATED sensors / GPIO outputs` is the demo rig. `reported.simulated` stays
`true` — it tracks the *readings*, not the outputs, because the degrees are
what the dashboard must warn about even while the LED is genuinely lit.

`MIN_ON_S=5` is there so the demo is watchable. The 30 s default is right for a
relay driving a real load; for an LED you want it to react while someone is
looking at it.

#### Pins

| Signal | BCM | Physical pin | Why this one |
|---|---|---|---|
| LED (heating) | GPIO17 | **11** | GPIO9–27 default to a **pull-down** at boot, so the pin is LOW and the LED is dark from power-on |
| Relay IN (cooling) | GPIO6 | **31** | GPIO0–8 default to a **pull-up**, so the pin is HIGH and an active-low relay stays released from power-on |

That asymmetry is the whole reason for the two choices, and it is the boot-gap
problem solved by picking the right pin instead of adding a resistor. GPIO0/1
are reserved for HAT EEPROM, 2/3 have fixed I²C pull-ups, 7–11 are SPI and
14/15 are the serial console — which leaves GPIO5 (pin 29) and GPIO6 (pin 31)
as the free pull-up pins.

#### LED — yes, it needs a resistor

A GPIO pin is a 3.3 V source rated ~16 mA (50 mA total across the whole
header). An LED with no resistor is a short across it.

```
GPIO17 (pin 11) ──/\/\/\──►|── GND (pin 9)
                   220Ω    LED
                          long leg = anode, toward the resistor
                          short leg / flat edge = cathode, to GND
```

**220 Ω** with a red LED gives (3.3 − 2.0) / 220 ≈ **5.9 mA** — comfortably
under the limit and plenty bright. 330 Ω works too, slightly dimmer.

**Use a red LED.** Blue, white and most greens have a forward voltage of
3.0–3.2 V, leaving almost nothing across the resistor: (3.3 − 3.1) / 220 ≈
0.9 mA, which is barely visible. Red is ~2.0 V and is the only colour that is
comfortable on a 3.3 V rail. Red also happens to be the right colour for heat.

Pins 11 and 9 are adjacent on the header, so this is two jumpers.

#### Fan through the relay

The fan is on pins 4 (5V) and 6 (GND) today, running permanently. Only the
positive lead moves — it goes through the relay contacts instead of straight
to the rail:

```
  5V  (pin 2)  ──── relay VCC
  GND (pin 39) ──── relay GND
  GPIO6 (pin 31) ── relay IN

  5V  (pin 4)  ──── relay COM
  relay NO     ──── fan +          NO = normally open, so the fan is OFF
  fan −        ──── GND (pin 6)    until the agent energises the coil
```

Use **NO**, not NC. With NO the fan is off when the relay is unpowered, which
is the state the Pi is in from power-on until the agent starts.

A 5 V fan draws 100–250 mA and the relay coil another 70–90 mA, all from the
Pi's 5 V rail. That is fine on a Pi 4 with the official supply. If you would
rather not hear the relay click every cycle, a logic-level MOSFET (diagram
above) is silent and has no contacts to wear — but the relay is what you
already have.

Verify the relay polarity before trusting it: see the 30-second click test
below.

### Relay modules — read this before wiring a heater

Two settings, both explicit because guessing either one damages hardware.

```bash
# the common blue opto-isolated relay board (SRD-05VDC-SL-C)
FAN_GPIO=18 HEATER_GPIO=23 sudo bash setup.sh
```

**`*_TYPE` (default `relay`).** A mechanical relay must never be given a PWM
signal. At 1 Hz that is 86,400 operations a day against a typical rating of
100,000 *mechanical* operations — dead in a day and a half. `relay` drives a
plain on/off output; `MIN_ON_S` / `MIN_OFF_S` in `control.py` protect it
further. Use `pwm` only for something that actually modulates: a 4-wire fan's
PWM input, a logic-level MOSFET, or a **solid-state** relay, which has nothing
to wear out and is happy at 1 Hz.

```bash
HEATER_TYPE=pwm    # only if the heater is on an SSR
```

**`*_ACTIVE_LOW` (default `true`).** Most cheap relay boards energise the coil
when the input is pulled **LOW**. The default is `true` because that is what
those boards do and because being wrong in that direction fails safe — a board
that is really active-HIGH simply never switches, which you notice at once and
harmlessly. Being wrong the other way leaves a heater energised.

```bash
HEATER_ACTIVE_LOW=0    # only if your board is documented active-HIGH
```

#### Verify the polarity in 30 seconds, with the mains side disconnected

Take the load off the relay's screw terminals first. Then listen for the click:

```bash
python3 -c "
from gpiozero import OutputDevice
from time import sleep
r = OutputDevice(23, active_high=False, initial_value=False)
print('should be OFF now — no click, LED off'); sleep(3)
print('ON');  r.on();  sleep(3)
print('OFF'); r.off(); sleep(1)
"
```

If the relay is already clicked-in at the first line, your board is
active-HIGH: re-run `setup.sh` with `HEATER_ACTIVE_LOW=0`.

#### The gap software cannot close

`initial_value=False` releases the coil the instant the device is constructed —
but that line only runs once the agent starts. From power-on until then, the
pin is a **floating input**, and on an active-low board a floating input can
read LOW and pull the relay in. That is a heater switched on for ~30 seconds
on every boot.

Only hardware fixes it: a **10 kΩ pull-UP to 3.3 V** on the signal line of an
active-low board. (Note this is the opposite of the pull-DOWN a MOSFET needs —
the resistor always goes to whichever rail means *off* for your part.)

And regardless: a real heater needs a mechanical over-temperature thermostat
wired in series with it. `MAX_INSIDE_C` is a backstop, not the protection.

#### Other relay-board wiring notes

* **JD-VCC jumper.** Leaving it fitted powers the coils from the Pi's 5V,
  which defeats the opto-isolation you paid for. Remove it and feed JD-VCC from
  a separate 5V supply, grounds commoned, if the board supports it.
* **Coil current.** ~70–90 mA per relay. That is the module's own transistor's
  job, never the GPIO pin's — which is why you use a module and not a bare relay.
* **Do not PWM a mains fan**, whatever is switching it.

## The chamber id

Defaults to `chamber-<hostname>`, so name the Pi something you recognise
(`sudo raspi-config` → System Options → Hostname) before the first run. Or set
it explicitly:

```bash
CHAMBER_ID=chamber-02 CHAMBER_NAME="תא אקלים 2 — בית דגן" python3 chamber.py
```

The id is saved in `~/.chamber-agent/identity.json` on first registration and
reused afterwards, so a later hostname change never orphans a chamber that
already has history. Renaming a chamber in the Firebase console also sticks —
the agent never overwrites a name it did not create.

## One-time project setup

Anonymous sign-in must be enabled once for the whole project:

<https://console.firebase.google.com/project/climate-chambers-1/authentication/providers>
→ Sign-in method → Add new provider → **Anonymous** → Enable → Save.

Without it the agent stops with `ADMIN_ONLY_OPERATION` and prints that link.

## No key on the device

The Pi holds **no service-account key and no admin token**. It signs in
anonymously with the project's public web API key — the same key the dashboard
already ships in its HTML — and claims a chamber document by writing its own
uid into `agentUid`.

From that moment the security rules allow **only that uid** to write that
chamber's telemetry, heartbeat, reported state and command acknowledgements.
A browser cannot write them at all, whatever account it is signed in with.

The tradeoff, chosen deliberately: registration is open, so any device that can
reach the project may create a *new* chamber. It cannot read or touch a chamber
it does not own. Unwanted chambers are deleted in the Firebase console.

## Install it for real (survives reboots and power cuts)

```bash
sudo bash -c "$(curl -fsSL https://climate-chambers.github.io/pi/setup.sh)"
```

That is the entire setup for a new Pi. Note the `bash -c "$(curl ...)"` form
rather than `curl ... | bash`: with a pipe, stdin *is* the pipe, so the script
could not prompt — it would read its own text as your answer.

If the files are already on the Pi, `sudo bash setup.sh` does the same without
downloading. Re-running is safe: it updates the files and restarts the service,
and the chamber keeps its identity.

Running `python3 chamber.py` by hand is for *trying* it. That process dies with
the ssh session and does not come back after a power cut — which is the
difference between a demo and a chamber you can leave alone.

| Failure | What happens |
|---|---|
| Agent crashes | systemd restarts it after 5 s (`Restart=always`) |
| Power cut | Pi boots, unit is `WantedBy=multi-user.target`, agent starts |
| Network down at boot | agent starts anyway — the unit does **not** wait on the network |
| Setpoint unknown | read from `~/.chamber-agent/desired.json`, written atomically |
| Identity lost | it is not: `identity.json` survives, so the Pi re-claims *its* chamber |
| Agent hangs, kernel wedges | hardware watchdog reboots the Pi after 15 s |

### Two Pis, one hostname

Every fresh Pi OS image is called `raspberrypi`, so the chamber id derived from
it collides on the second Pi. `register()` walks a numeric suffix until a free
id is found — `chamber-raspberrypi`, then `-2`, then `-3` — and only treats an
id as taken when the rules refuse the claim with 403, which happens exactly
when another device is still reporting on it.

The winning id is written to `identity.json` and adopted on every later boot.
That matters in both directions: a Pi renamed in `raspi-config` keeps the
chamber holding its history instead of registering a new one, and a Pi that
settled on `-3` does not re-fight for the base id every reboot. An explicit
`CHAMBER_ID` still overrides — that is the operator speaking.

### Why the state files are written atomically

`open(path, "w")` truncates first and writes second. Lose power in that window
and the file comes back empty. For `desired.json` that means a silent fall back
to the default setpoint; for `identity.json` it is worse — the Pi looks like a
brand-new device on the next boot, registers again, creates a **second**
chamber and abandons the one with all the history.

`write_json_atomic()` in `cloud.py` writes a temp file, fsyncs it, then
`os.replace`s it, which is atomic on POSIX. A reader afterwards sees the whole
old file or the whole new one, never a fragment.

### The one thing software cannot promise

The watchdog reboots a hung Pi, but between the hang and the reboot the
actuators stay wherever they were. GPIO pins are also floating inputs from
power-on until the agent runs. Both are why the 10 kΩ gate pull-down matters,
and why a real heater still needs a mechanical over-temperature thermostat in
series with it. Treat `MAX_INSIDE_C` as a backstop, never as the protection.

### Fetching without ssh

The four `.py` files are deliberately *not* in the Hosting ignore list in
`firebase.json`, so a Pi can fetch them from the deployed site:

```bash
mkdir -p ~/chamber && cd ~/chamber
for f in chamber control hardware cloud; do
  curl -fsSL "https://climate-chambers.github.io/pi/$f.py" -o "$f.py"
done
python3 chamber.py
```

(`pi/README.md` *is* ignored, and so is `__pycache__` — bytecode built for the
wrong architecture has no business being served.)

## Protocol

| Path | Direction | Written by |
|---|---|---|
| `chambers/{id}.desired` | setpoint in | dashboard |
| `chambers/{id}.reported` | actual actuator state out | the agent |
| `chambers/{id}.lastSeen` | heartbeat out | the agent (server timestamp) |
| `chambers/{id}.agentUid` | ownership claim | the agent |
| `chambers/{id}/telemetry/current` | live readings out | the agent |

New fields in `reported`, all published so the dashboard never has to guess:

| Field | Meaning |
|---|---|
| `cloudOnline` | whether the sync thread is currently reaching Firestore |
| `setpointAgeS` | seconds since the setpoint was last confirmed (−1 = never) |
| `fault` | empty when healthy; a short reason when not |
| `held` | a minimum on/off timer is holding a change back |
| `fanControlled` / `heaterControlled` | whether the agent can command that output at all |
| `simulated` | true whenever the readings came out of a model |

Polling, not streaming, and that is deliberate. Firestore's listener is
gRPC-only, so a push subscription would mean a service-account key on every
device and a heavy dependency — to learn about a setpoint change in 200 ms
instead of 10 s, in a system whose thermal time constant is minutes.

## What is deliberately not here

**Commands.** `firestore.rules` defines a `commands` subcollection and
`js/chamber-store.js` has a branch that writes to it, but no button in
`chamber.html` reaches that branch — the only three the UI sends (`setTarget`,
`setSimAmbient`, `setSystemMode`) all travel through `desired`. So the agent
has nothing to acknowledge, and a handler here would be an answer to a question
nobody asks. `tools/virtual-pi.mjs` still implements the pattern if command
buttons are ever added.

**History.** `chamber_agent.py` used to write a `history/{id}` time series every
five minutes. Nothing in the dashboard ever read it. If a chart is wanted later,
write the series then — with a retention policy, which the old code lacked.

**Removed 2026-09-15:** `pi_connect.py` and `chamber_agent.py`, the previous
single-file agents. In both of them a network failure stopped the control loop —
`pi_connect.py` caught only `KeyboardInterrupt`, so any `URLError` killed the
process; `chamber_agent.py` caught `URLError` but polled the setpoint first in
the `try`, so a drop skipped `hardware.apply()` and left the actuators latched
in whatever state they happened to be in. That is the bug this rewrite exists
to fix, and it is why the network lives on its own thread now.
