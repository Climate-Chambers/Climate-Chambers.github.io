#!/usr/bin/env bash
# ==========================================================================
# One command. Sets up a Raspberry Pi as a climate chamber, start to finish.
#
#   sudo bash -c "$(curl -fsSL https://climate-chambers.github.io/pi/setup.sh)"
#
# or, if the files are already on the Pi:
#
#   sudo bash setup.sh
#
# It asks for the chamber name, then does everything else itself: downloads
# the agent, installs it as a service that starts at boot, enables the
# hardware watchdog, and starts it. Nothing to edit, nothing to remember.
#
# NOTE the `bash -c "$(curl ...)"` form rather than `curl ... | bash`. With a
# pipe, stdin IS the pipe, so the script cannot ask you anything — the prompts
# below would silently read the script's own text as your answer.
#
# Answer nothing and press Enter for the defaults; every prompt has one.
# Re-running is safe: it updates the files and restarts the service, and the
# chamber keeps its identity.
#
# Target: Raspberry Pi OS (Bookworm) on a Pi 4 Model B.
# ==========================================================================
set -euo pipefail

# Where to fetch the agent's four .py files from. The dashboard's "add a
# chamber" panel passes this in, built from the address the page itself was
# served from, so the Pi always pulls from the same site the operator was
# looking at. The default below is only for a hand-typed run.
#
# NOTE this is GitHub Pages, NOT Firebase Hosting — the site is published from
# the repository and the `pi/` folder has to be uploaded with it. Firebase is
# still the backend the dashboard and the agent talk to; it just does not
# serve these files.
BASE_URL="${BASE_URL:-https://climate-chambers.github.io/pi}"
FILES="chamber.py control.py hardware.py cloud.py"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo:  sudo bash setup.sh" >&2
    exit 1
fi

RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
STATE_DIR="$RUN_HOME/.chamber-agent"
TARGET_DIR="$RUN_HOME/chamber"

echo
echo "=============================================="
echo "  Climate chamber — Raspberry Pi setup"
echo "=============================================="
echo "  user   $RUN_USER"

# ---- 1. the agent files --------------------------------------------------
# Either they are sitting next to this script, or we fetch them. Both paths
# end with the same four files in $TARGET_DIR owned by the right user.
HERE=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]:-}" ]; then
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "$HERE" ] && [ -f "$HERE/chamber.py" ]; then
    TARGET_DIR="$HERE"
    echo "  agent  $TARGET_DIR (already here)"
else
    echo "  agent  downloading to $TARGET_DIR"
    install -d -o "$RUN_USER" -g "$RUN_USER" "$TARGET_DIR"
    for f in $FILES; do
        curl -fsSL "$BASE_URL/$f" -o "$TARGET_DIR/$f" \
            || { echo "Could not download $f from $BASE_URL" >&2; exit 1; }
        chown "$RUN_USER:$RUN_USER" "$TARGET_DIR/$f"
    done
fi

# ---- 2. ask ---------------------------------------------------------------
# Only if nobody already answered, and only if there is a terminal to ask.
# A re-run on a Pi that already has an identity must not re-prompt: its id is
# settled and changing it would orphan the chamber's history.
EXISTING_ID=""
if [ -f "$STATE_DIR/identity.json" ]; then
    EXISTING_ID="$(sed -n 's/.*"chamberId"[ :]*"\([^"]*\)".*/\1/p' "$STATE_DIR/identity.json")"
fi

DEFAULT_NAME="תא אקלים $(hostname)"
if [ -n "$EXISTING_ID" ]; then
    echo
    echo "  This Pi is already registered as '$EXISTING_ID' — keeping it."
elif [ -t 0 ] && [ -z "${CHAMBER_NAME:-}" ]; then
    echo
    echo "  Give the chamber a name. It is what you will see in the dashboard,"
    echo "  and Hebrew is fine. Press Enter to accept the default."
    echo
    printf "  Chamber name [%s]: " "$DEFAULT_NAME"
    read -r answer </dev/tty || answer=""
    CHAMBER_NAME="${answer:-$DEFAULT_NAME}"
fi
CHAMBER_NAME="${CHAMBER_NAME:-$DEFAULT_NAME}"

# The document id is derived, not asked: it must be url-safe ASCII, and asking
# for a second, differently-spelled name is how people end up with a chamber
# called "North" whose id says "south". Hebrew slugifies to nothing, so the
# hostname is the fallback — and chamber.py resolves any collision by walking
# a numeric suffix, so two Pis called `raspberrypi` no longer fight.
SLUG="$(printf '%s' "$CHAMBER_NAME" | tr '[:upper:]' '[:lower:]' \
        | sed 's/[^a-z0-9]\+/-/g; s/^-//; s/-$//')"
[ -n "$SLUG" ] && [ "$SLUG" != "-" ] || SLUG="$(hostname | tr '[:upper:]' '[:lower:]' \
        | sed 's/[^a-z0-9]\+/-/g; s/^-//; s/-$//')"
[ -n "$SLUG" ] || SLUG="pi"
CHAMBER_ID="${CHAMBER_ID:-${EXISTING_ID:-chamber-$SLUG}}"

echo
echo "  name   $CHAMBER_NAME"
echo "  id     $CHAMBER_ID"
echo

# ---- 2b. wiring ----------------------------------------------------------
# Asked rather than assumed, because the alternative was worse in both
# directions: a bare install produced a chamber with no actuators and no hint
# as to why, and the fix — five environment variables the operator had to know
# to prepend — is not something anyone guesses from a dashboard button.
#
# Anything already set on the command line is left alone, and every question
# defaults to "not connected", so pressing Enter four times gives exactly the
# old behaviour. Skipped entirely when there is no terminal to ask.
ask_gpio() {
    # $1 prompt, $2 variable name. Accepts a BCM number or empty for none.
    local prompt="$1" varname="$2" reply
    [ -n "${!varname:-}" ] && return 0
    while :; do
        printf "  %s " "$prompt"
        read -r reply </dev/tty || reply=""
        [ -z "$reply" ] && return 0
        if [ "$reply" -ge 2 ] 2>/dev/null && [ "$reply" -le 27 ] 2>/dev/null; then
            printf -v "$varname" '%s' "$reply"
            return 0
        fi
        echo "    not a BCM GPIO number (2-27). Enter to skip."
    done
}

if [ -t 0 ] && [ -z "${FAN_GPIO:-}${HEATER_GPIO:-}" ]; then
    cat <<'WIRING'
  Wiring. Press Enter at any question to leave that output unconnected —
  the agent then models the chamber in software and drives no pins at all.

  BCM numbers, not physical pin positions. A demo rig usually uses an LED on
  17 (physical pin 11) for heating and a relay on 27 (physical pin 13) for the
  cooling fan.

WIRING
    ask_gpio "Heating output  — LED or relay, BCM GPIO [none]:" HEATER_GPIO
    if [ -n "${HEATER_GPIO:-}" ] && [ -z "${HEATER_TYPE:-}" ]; then
        printf "    Is it an LED or a relay? [led/relay] (led): "
        read -r reply </dev/tty || reply=""
        case "${reply:-led}" in
            relay|r) HEATER_TYPE=relay ;;
            *)       HEATER_TYPE=led ;;
        esac
    fi

    ask_gpio "Cooling output  — fan relay, BCM GPIO [none]:" FAN_GPIO
    if [ -n "${FAN_GPIO:-}" ] && [ -z "${FAN_ACTIVE_LOW:-}" ]; then
        # The one question that damages hardware if guessed wrong, so it is
        # asked in plain language rather than as "active low?".
        printf "    Does the relay switch ON when the pin goes LOW?\n"
        printf "    (cheap blue opto boards: yes.  Pololu carriers: no)  [y/N]: "
        read -r reply </dev/tty || reply=""
        case "$reply" in
            [Yy]*) FAN_ACTIVE_LOW=1 ;;
            *)     FAN_ACTIVE_LOW=0 ;;
        esac
    fi
    echo
fi

if [ -n "${HEATER_GPIO:-}" ] || [ -n "${FAN_GPIO:-}" ]; then
    if [ -n "${HEATER_GPIO:-}" ]; then
        echo "  heat   GPIO$HEATER_GPIO (${HEATER_TYPE:-relay})"
    else
        echo "  heat   not connected"
    fi
    if [ -n "${FAN_GPIO:-}" ]; then
        if [ "${FAN_ACTIVE_LOW:-0}" = 1 ]; then pol=LOW; else pol=HIGH; fi
        echo "  cool   GPIO$FAN_GPIO (${FAN_TYPE:-relay}, active-$pol)"
    else
        echo "  cool   not connected"
    fi
    echo
    echo "  Verify these against the board before trusting them. A wrong"
    echo "  polarity leaves an output energised — see pi/README.md."
    echo
fi

install -d -o "$RUN_USER" -g "$RUN_USER" -m 700 "$STATE_DIR"

# ---- 3. GPIO -------------------------------------------------------------
if getent group gpio >/dev/null && ! id -nG "$RUN_USER" | grep -qw gpio; then
    usermod -aG gpio "$RUN_USER"
    echo "  gpio   added $RUN_USER to the gpio group"
fi

# gpiozero only matters once a pin is actually being driven. Pi OS Desktop
# ships it, Lite does not, and an install with no wiring yet must not drag in
# apt for nothing.
if [ -n "${FAN_GPIO:-}${HEATER_GPIO:-}" ]; then
    if ! sudo -u "$RUN_USER" python3 -c "import gpiozero" 2>/dev/null; then
        echo "  gpio   installing python3-gpiozero..."
        apt-get update -qq && apt-get install -y -qq python3-gpiozero
    fi
fi

# ---- 4. the service ------------------------------------------------------
# Every tunable the agent understands, carried into the unit file if — and
# only if — it was set on this command line. Listing them explicitly beats
# exporting the whole environment: a stray variable from the operator's shell
# has no business steering a chamber.
#
# Keep this list in step with the tunables in pi/README.md. A variable missing
# from here is the sort of bug that works perfectly when you test by hand and
# then quietly does nothing once systemd is the one starting the agent.
PASSTHROUGH="
  CHAMBER_LOCATION
  FAN_GPIO FAN_TYPE FAN_ACTIVE_LOW FAN_PWM_HZ
  HEATER_GPIO HEATER_TYPE HEATER_ACTIVE_LOW
  SENSOR_COUNT AMBIENT_C RATE_C_PER_S
  BAND_ON_C BAND_OFF_C MIN_ON_S MIN_OFF_S MAX_INSIDE_C MAX_OFFSET_C
  CONTROL_INTERVAL_S SYNC_INTERVAL_S PUBLISH_INTERVAL_S
"
PASSTHROUGH_LINES=""
for var in $PASSTHROUGH; do
    if [ -n "${!var:-}" ]; then
        PASSTHROUGH_LINES="${PASSTHROUGH_LINES}Environment=$var=${!var}"$'\n'
        echo "  env    $var=${!var}"
    fi
done

cat > /etc/systemd/system/chamber.service <<UNIT
[Unit]
Description=Climate chamber agent
# Deliberately NOT After=network-online.target and NOT Wants=network.
# This agent controls the chamber with or without a network; waiting for one
# would hold the control loop behind a DHCP lease that may never arrive.
After=local-fs.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$TARGET_DIR
Environment=PYTHONUNBUFFERED=1
Environment=CHAMBER_STATE_DIR=$STATE_DIR
Environment=CHAMBER_ID=$CHAMBER_ID
Environment=CHAMBER_NAME=$CHAMBER_NAME
$PASSTHROUGH_LINES
ExecStart=/usr/bin/python3 $TARGET_DIR/chamber.py

# Restart=always, not on-failure: a clean exit is still a chamber with nobody
# watching it. RestartSec is short because what is down is the control loop.
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --quiet chamber.service
systemctl restart chamber.service
echo "  agent  installed and started"

# ---- 5. hardware watchdog ------------------------------------------------
# Covers the failure systemd cannot: the kernel wedging, or the agent
# deadlocking while still technically "running". Without it a hung Pi holds
# whatever the actuators were last set to until somebody notices.
BOOT_CFG=/boot/firmware/config.txt
[ -f "$BOOT_CFG" ] || BOOT_CFG=/boot/config.txt
NEEDS_REBOOT=0

if [ -f "$BOOT_CFG" ] && ! grep -q "^dtparam=watchdog=on" "$BOOT_CFG"; then
    echo "dtparam=watchdog=on" >> "$BOOT_CFG"
    NEEDS_REBOOT=1
    echo "  wdog   enabled (needs one reboot)"
fi

if ! grep -q "^RuntimeWatchdogSec=" /etc/systemd/system.conf; then
    sed -i 's/^#*RuntimeWatchdogSec=.*/RuntimeWatchdogSec=15/' /etc/systemd/system.conf
    grep -q "^RuntimeWatchdogSec=" /etc/systemd/system.conf \
        || echo "RuntimeWatchdogSec=15" >> /etc/systemd/system.conf
fi

# ---- 6. say what happened ------------------------------------------------
sleep 2
echo
echo "=============================================="
systemctl is-active --quiet chamber.service \
    && echo "  Running. The chamber is under control." \
    || echo "  NOT running — see: journalctl -u chamber -n 40"
echo
echo "  Dashboard"
echo "    https://climate-chambers.github.io/chamber.html?id=$CHAMBER_ID"
echo
echo "  Watch it      journalctl -u chamber -f"
echo "  Stop it       sudo systemctl stop chamber"
echo "  Remove it     sudo systemctl disable --now chamber"
[ "$NEEDS_REBOOT" = 1 ] && echo "
  Reboot once to arm the hardware watchdog:  sudo reboot"
echo "=============================================="
echo
