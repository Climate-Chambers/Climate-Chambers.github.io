#!/usr/bin/env bash
# ==========================================================================
# One command. Sets up a Raspberry Pi as a climate chamber, start to finish.
#
#   sudo bash -c "$(curl -fsSL https://climate-chambers-1.web.app/pi/setup.sh)"
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
${FAN_GPIO:+Environment=FAN_GPIO=$FAN_GPIO}
${HEATER_GPIO:+Environment=HEATER_GPIO=$HEATER_GPIO}
${CHAMBER_LOCATION:+Environment=CHAMBER_LOCATION=$CHAMBER_LOCATION}
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
echo "    https://climate-chambers-1.web.app/chamber.html?id=$CHAMBER_ID"
echo
echo "  Watch it      journalctl -u chamber -f"
echo "  Stop it       sudo systemctl stop chamber"
echo "  Remove it     sudo systemctl disable --now chamber"
[ "$NEEDS_REBOOT" = 1 ] && echo "
  Reboot once to arm the hardware watchdog:  sudo reboot"
echo "=============================================="
echo
