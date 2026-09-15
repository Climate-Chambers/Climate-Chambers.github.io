#!/usr/bin/env python3
"""Everything that touches the network.

All of it runs on one background thread that is allowed to fail. The control
loop never calls into this file — it reads the setpoint from `Setpoint`, which
is a plain object in memory backed by a file on disk. That is the whole
offline story: pull the Ethernet cable and the chamber does not notice, because
nothing in the control path ever had a socket in it.

Firestore is reached over its REST API with the standard library alone, so
there is no venv on the Pi and no wheel to rebuild after an OS upgrade. The
API key below is public — it identifies the project and grants nothing by
itself; the security rules decide what this device may write.

Polling, not streaming, and that is a deliberate choice. Firestore's listener
is gRPC-only, so a push subscription would mean a service-account key on every
device and a heavy dependency — to learn about a setpoint change in 200 ms
instead of 10 s, in a system whose thermal time constant is minutes.
"""

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PROJECT_ID = "climate-chambers-1"
API_KEY = "AIzaSyAlWfYpecZ1IxjGGsLSPUbJwzStarRlOoU"

# Set explicitly by the systemd unit. Under `User=` systemd does export $HOME,
# so expanduser would usually work — but "usually" is not what you want holding
# the only copy of a chamber's identity, and an explicit path makes it obvious
# in `systemctl cat` where the state lives.
STATE_DIR = os.environ.get("CHAMBER_STATE_DIR") or os.path.expanduser("~/.chamber-agent")
IDENTITY_FILE = os.path.join(STATE_DIR, "identity.json")
SETPOINT_FILE = os.path.join(STATE_DIR, "desired.json")

DOC_ROOT = f"projects/{PROJECT_ID}/databases/(default)/documents"
FIRESTORE = f"https://firestore.googleapis.com/v1/{DOC_ROOT}"

SYNC_INTERVAL_S = float(os.environ.get("SYNC_INTERVAL_S", "10"))
AGENT_VERSION = "chamber/2.0"

# In Python 3 `socket.error` IS `OSError`, and both URLError and ConnectionError
# are subclasses of it — so this tuple is simply OSError, and it will happily
# catch a missing file as though the network were down. Two rules follow, and
# breaking either one is how a brand-new Pi silently fails to register:
#   * never wrap file I/O in a try that also has a NETWORK_ERRORS arm;
#   * always catch urllib.error.HTTPError BEFORE this, since it is a URLError
#     and a 403 is an answer from the server, not a dead network.
NETWORK_ERRORS = (OSError, TimeoutError)


def write_json_atomic(path, data, mode=None):
    """Write JSON so that a power cut can never leave a half-written file.

    `open(path, "w")` truncates first and writes second. Lose power in that
    window — which on a chamber is not hypothetical — and the file comes back
    empty, the load fails, and the agent silently falls back to a default
    setpoint. That is the exact failure this file exists to prevent.

    So: write a temporary file, fsync it, then `os.replace`, which is atomic on
    POSIX. A reader afterwards sees either the whole old file or the whole new
    one, never a fragment. The directory fsync is what makes the rename itself
    survive the cut on ext4.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)
    try:
        dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass        # not POSIX, or no permission — the replace still happened


def local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# The setpoint, and why it lives on disk
# ---------------------------------------------------------------------------

class Setpoint:
    """The operator's intent, cached locally.

    Written to disk every time the cloud delivers a change, so a Pi that loses
    power and comes back up with no internet still knows what it was asked to
    do. Without this, an outage that spans a reboot would leave the chamber
    controlling to a hardcoded default — which is the one failure mode that
    would actually damage an experiment.
    """

    DEFAULT = {"mode": "AUTO", "targetMode": "relative", "targetValue": 10.0}

    def __init__(self):
        self._lock = threading.Lock()
        self._value = dict(self.DEFAULT)
        self._synced_at = None
        try:
            with open(SETPOINT_FILE, encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                self._value.update(saved)
                print(f"setpoint: restored from disk — {self._value.get('mode')}")
        except (OSError, ValueError):
            pass

    def current(self):
        with self._lock:
            return dict(self._value)

    def age_s(self):
        """Seconds since the cloud last confirmed the setpoint, or None if never.

        Published to the dashboard so it can say "running on a setpoint from
        four hours ago" rather than implying the number on screen is fresh.
        """
        with self._lock:
            return None if self._synced_at is None else time.time() - self._synced_at

    def update(self, incoming):
        if not isinstance(incoming, dict):
            return
        with self._lock:
            before = dict(self._value)
            self._value.update(incoming)
            self._synced_at = time.time()
            changed = self._value != before
            snapshot = dict(self._value)

        if not changed:
            return
        if before.get("mode") != snapshot.get("mode"):
            print(f"setpoint: mode {before.get('mode')} -> {snapshot.get('mode')}")
        try:
            write_json_atomic(SETPOINT_FILE, snapshot)
        except OSError as exc:
            print(f"setpoint: could not persist to disk ({exc})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Firestore's typed JSON
# ---------------------------------------------------------------------------

def encode(value):
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, (int, float)):
        return {"doubleValue": float(value)}
    if isinstance(value, str):
        return {"stringValue": value}
    if value is None:
        return {"nullValue": None}
    if isinstance(value, datetime):
        return {"timestampValue": value.astimezone(timezone.utc)
                .isoformat().replace("+00:00", "Z")}
    if isinstance(value, list):
        return {"arrayValue": {"values": [encode(v) for v in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: encode(v) for k, v in value.items()}}}
    raise TypeError(type(value))


def decode(fields):
    out = {}
    for key, wrapper in (fields or {}).items():
        kind, raw = next(iter(wrapper.items()))
        if kind == "doubleValue":
            out[key] = float(raw)
        elif kind == "integerValue":
            out[key] = int(raw)
        elif kind == "mapValue":
            out[key] = decode(raw.get("fields", {}))
        elif kind == "nullValue":
            out[key] = None
        else:
            out[key] = raw
    return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _request(url, payload=None, token=None, form=False):
    headers = {}
    data = None
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode() or "{}")


# ---------------------------------------------------------------------------
# The connection
# ---------------------------------------------------------------------------

class Cloud:
    """Sign in, register once, then publish. Every method may raise; the worker
    thread above is the only caller and it catches everything."""

    MAX_ID_ATTEMPTS = 20

    def __init__(self, chamber_id, chamber_name, sensor_count, id_is_explicit=False):
        # The id the operator or the hostname *suggests*. The one actually in
        # use may differ: a saved id from a previous run wins over it, and a
        # collision with another live Pi bumps a suffix onto it.
        self.chamber_id = chamber_id
        self.id_is_explicit = id_is_explicit
        self.chamber_name = chamber_name
        # Published so the dashboard can size its sensor table from the device
        # rather than a constant in the HTML. Drop it and a chamber with three
        # probes still draws five rows, two of them permanently blank.
        self.sensor_count = sensor_count
        self.token = None
        self.uid = None
        self._refresh_token = None
        self._token_expires = 0.0
        self._registered = False

    # -- identity ----------------------------------------------------------

    def sign_in(self):
        """Anonymous sign-in, cached on disk so the Pi keeps the same identity
        — and therefore its claim on the chamber — across reboots.

        Reading the cache and using it are kept in separate steps. Folding them
        into one try/except looks tidier and is wrong: a missing identity file
        is an OSError, a dead network raises an OSError too, and the two need
        opposite responses — register a new device, or back off and retry.
        """
        if self.token and time.time() < self._token_expires:
            return

        saved = self._load_identity()
        refresh = None

        if saved:
            try:
                res = _request(f"https://securetoken.googleapis.com/v1/token?key={API_KEY}",
                               {"grant_type": "refresh_token",
                                "refresh_token": saved["refreshToken"]}, form=True)
                self.token, self.uid, refresh = (
                    res["id_token"], res["user_id"], res["refresh_token"])
            except urllib.error.HTTPError:
                # The saved refresh token was rejected (revoked, or the project
                # was reset). Fall through and register as a new device.
                print("cloud: saved identity rejected — registering anew")
            except (KeyError, ValueError):
                print("cloud: saved identity is unreadable — registering anew")

        if refresh is None:
            res = self._register_device()
            self.token, self.uid, refresh = (
                res["idToken"], res["localId"], res["refreshToken"])
            print(f"cloud: registered this device as {self.uid}")

        self._token_expires = time.time() + 45 * 60
        self._refresh_token = refresh

        # Adopt the id this device negotiated on a previous run. Without this
        # a Pi renamed in raspi-config would walk away from the chamber that
        # holds all its history and register a brand-new one — and a Pi that
        # had settled on a `-2` suffix would re-fight for the base id on every
        # reboot. An explicit CHAMBER_ID still overrides: that is the operator
        # speaking, and they get the last word.
        saved_id = (saved or {}).get("chamberId")
        if saved_id and not self.id_is_explicit and saved_id != self.chamber_id:
            print(f"cloud: using saved chamber id '{saved_id}'")
            self.chamber_id = saved_id

        self._save_identity()

    def _save_identity(self):
        """Atomic, and for a sharper reason than the setpoint: a truncated
        identity file makes this Pi look like a brand-new device on the next
        boot. It would register again, create a SECOND chamber, and abandon the
        one that has all the history."""
        if not self._refresh_token:
            return
        try:
            write_json_atomic(IDENTITY_FILE,
                              {"uid": self.uid, "refreshToken": self._refresh_token,
                               "chamberId": self.chamber_id}, mode=0o600)
        except OSError as exc:
            print(f"cloud: could not save identity ({exc})", file=sys.stderr)

    def _load_identity(self):
        """The saved identity, or None. Never raises — a first run, a wiped SD
        card and a corrupt file are all just "no identity yet"."""
        try:
            with open(IDENTITY_FILE, encoding="utf-8") as fh:
                saved = json.load(fh)
            return saved if saved.get("refreshToken") else None
        except (OSError, ValueError, AttributeError):
            return None

    def _register_device(self):
        try:
            return _request(
                f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={API_KEY}",
                {"returnSecureToken": True})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if "ADMIN_ONLY_OPERATION" in detail or "OPERATION_NOT_ALLOWED" in detail:
                sys.exit(
                    "\nAnonymous sign-in is disabled for this Firebase project, so the Pi\n"
                    "cannot register itself. Enable it once (ten seconds):\n"
                    f"  https://console.firebase.google.com/project/{PROJECT_ID}"
                    "/authentication/providers\n"
                    "  -> Sign-in method -> Add new provider -> Anonymous -> Enable -> Save\n"
                )
            raise

    # -- writes ------------------------------------------------------------

    def _commit(self, doc, data, mask=None, must_exist=None, stamp="lastSeen"):
        """One write. The timestamp is stamped by the server, so the
        dashboard's online/offline logic cannot be fooled by a wrong Pi clock."""
        op = {
            "update": {"name": doc, "fields": {k: encode(v) for k, v in data.items()}},
            "updateTransforms": [{"fieldPath": stamp, "setToServerValue": "REQUEST_TIME"}],
        }
        if mask is not None:
            op["updateMask"] = {"fieldPaths": mask}
        if must_exist is not None:
            op["currentDocument"] = {"exists": must_exist}
        return _request(f"https://firestore.googleapis.com/v1/{DOC_ROOT}:commit",
                        {"writes": [op]}, self.token)

    @property
    def doc(self):
        return f"{DOC_ROOT}/chambers/{self.chamber_id}"

    def register(self):
        """Claim a chamber document, picking a free id if the wanted one is taken.

        Three outcomes per candidate id:
          * it does not exist        -> create it, done
          * it exists and is ours    -> re-claim it, done
          * it exists and is someone
            else's, still reporting  -> the rules refuse with 403; try the next

        The suffix walk is what makes a fresh Pi OS image work unattended. Every
        new image is called `raspberrypi`, so the second Pi on a bench would
        otherwise collide with the first, be refused, and stop. Now it quietly
        becomes `chamber-raspberrypi-2`.

        The winning id is saved to identity.json, so this negotiation happens
        exactly once per device: afterwards the Pi re-claims *its* chamber even
        if the hostname changed, and never drifts onto a different suffix.
        """
        if self._registered:
            return

        base = self.chamber_id
        for attempt in range(1, self.MAX_ID_ATTEMPTS + 1):
            self.chamber_id = base if attempt == 1 else f"{base}-{attempt}"
            if self._try_claim():
                if self.chamber_id != base:
                    print(f"cloud: '{base}' is taken by another live device — "
                          f"using '{self.chamber_id}'")
                self._registered = True
                self._save_identity()
                return

        # Every candidate refused. Do not exit: this runs on the sync thread,
        # and the chamber must keep being controlled regardless of whether the
        # dashboard ever hears about it again.
        raise RuntimeError(
            f"could not claim an id after {self.MAX_ID_ATTEMPTS} attempts from "
            f"'{base}'. Set one explicitly: CHAMBER_ID=chamber-02")

    def _try_claim(self):
        """True if this device now owns self.chamber_id."""
        claim = {"agentUid": self.uid, "agentHost": socket.gethostname(),
                 "agentIp": local_ip(), "agentVersion": AGENT_VERSION,
                 "sensorCount": float(self.sensor_count)}
        try:
            self._commit(self.doc, {
                **claim,
                "name": self.chamber_name,
                "location": os.environ.get("CHAMBER_LOCATION", "לא הוגדר"),
                "devices": [],
                "desired": {**Setpoint.DEFAULT,
                            "updatedAt": datetime.now(timezone.utc),
                            "updatedBy": self.uid},
            }, must_exist=False)
            print(f'cloud: created chamber "{self.chamber_name}" (id: {self.chamber_id})')
            return True
        except urllib.error.HTTPError as exc:
            if b"ALREADY_EXISTS" not in exc.read():
                raise

        try:
            self._commit(self.doc, claim, mask=list(claim), must_exist=True)
            print(f"cloud: re-claimed existing chamber {self.chamber_id}")
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                return False        # someone else's, and they are still alive
            raise

    def pull_desired(self):
        doc = _request(f"{FIRESTORE}/chambers/{self.chamber_id}", token=self.token)
        return decode(doc.get("fields", {})).get("desired") or {}

    def publish(self, reported, telemetry):
        self._commit(self.doc, {"reported": reported, "agentUid": self.uid,
                                "agentIp": local_ip()},
                     mask=["reported", "agentUid", "agentIp"], must_exist=True)
        self._commit(f"{self.doc}/telemetry/current", telemetry,
                     mask=list(telemetry), stamp="updatedAt")


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

class SyncWorker(threading.Thread):
    """The only thread that talks to Firestore.

    It holds the latest snapshot the control loop handed it, publishes that,
    and pulls the setpoint back down. Both directions are best-effort: a
    failure backs off and is counted, and nothing propagates to the caller.
    `offer()` never blocks for longer than a dict copy.
    """

    def __init__(self, cloud, setpoint, stop_event):
        super().__init__(name="cloud-sync", daemon=True)
        self.cloud = cloud
        self.setpoint = setpoint
        self._shutdown = stop_event
        self._lock = threading.Lock()
        self._snapshot = None
        self.online = False
        self.failures = 0

    def offer(self, reported, telemetry):
        """Called by the control loop every cycle. Cheap, and never raises."""
        with self._lock:
            self._snapshot = (reported, telemetry)

    def run(self):
        backoff = 0.0
        while not self._shutdown.is_set():
            try:
                self.cloud.sign_in()
                self.cloud.register()

                with self._lock:
                    snapshot = self._snapshot
                if snapshot is not None:
                    self.cloud.publish(*snapshot)

                self.setpoint.update(self.cloud.pull_desired())

                if not self.online:
                    print("cloud: online")
                self.online, self.failures, backoff = True, 0, 0.0

            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                if exc.status == 404 or "NOT_FOUND" in body:
                    # Someone deleted this chamber in the console. Re-creating
                    # it behind their back would make unwanted chambers
                    # impossible to get rid of — so stop syncing and let the
                    # control loop carry on running the chamber locally.
                    print(f"cloud: chambers/{self.cloud.chamber_id} was deleted in the "
                          "console — no longer publishing. Control continues locally.",
                          file=sys.stderr)
                    return
                self._degrade(f"HTTP {exc.status}")
                backoff = min(120.0, max(5.0, backoff * 2 or 5.0))
            except NETWORK_ERRORS as exc:
                self._degrade(str(exc))
                backoff = min(120.0, max(5.0, backoff * 2 or 5.0))
            except Exception as exc:        # noqa: BLE001 — must never kill the thread
                self._degrade(f"unexpected: {exc}")
                backoff = min(120.0, max(5.0, backoff * 2 or 5.0))

            self._shutdown.wait(SYNC_INTERVAL_S + backoff)

    def _degrade(self, reason):
        self.failures += 1
        if self.online or self.failures == 1:
            print(f"cloud: offline ({reason}) — the chamber keeps running locally",
                  file=sys.stderr)
        self.online = False
