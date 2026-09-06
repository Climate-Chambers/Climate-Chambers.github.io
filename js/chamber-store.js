/* ==========================================================================
   Per-chamber Firestore binding for chamber.html.

   Reads:   chambers/{id}                  -> config, desired, reported, lastSeen
            chambers/{id}/telemetry/current -> ambient + sensor array
   Writes:  chambers/{id}.desired           -> setpoints (the only field rules
                                               allow a browser to touch)
            chambers/{id}/commands          -> one-shot actions, status 'pending'

   Everything else on the chamber — reported state, telemetry, heartbeat — is
   written exclusively by the Raspberry Pi that owns it (the device whose uid
   is in `agentUid`); the rules refuse those fields to every browser.
   ========================================================================== */

import {
    addDoc,
    collection,
    doc,
    getDocs,
    onSnapshot,
    orderBy,
    query,
    serverTimestamp,
    updateDoc
} from 'https://www.gstatic.com/firebasejs/12.18.0/firebase-firestore.js';

import { auth, db, HEARTBEAT_TIMEOUT_MS } from './firebase-config.js?v=20260906a';

/**
 * Subscribes the dashboard to one chamber. Returns a teardown function.
 *
 * @param {string} chamberId
 * @param {object} ui         window.ChamberUI bridge
 * @param {object} simulator  createSimulator() instance, used as offline fallback
 */
export async function attachChamber(chamberId, ui, simulator) {
    const chamberRef = doc(db, 'chambers', chamberId);
    const telemetryRef = doc(db, 'chambers', chamberId, 'telemetry', 'current');

    /* ------------------------------------------------------------------
       Writer: the browser's half of the desired/reported split.
       ------------------------------------------------------------------ */
    const adapter = {
        async sendCommand(command, payload = {}) {
            const uid = auth.currentUser ? auth.currentUser.uid : null;
            if (!uid) return { ok: false, reason: 'not-signed-in' };

            /* Setpoints persist in BOTH modes, on purpose.
               A setpoint is the operator's intent, not a measurement: it is
               legitimate to configure a chamber whose Pi is not connected yet,
               and the agent will pick the value up the moment it boots. What
               simulator mode must never do is invent *readings* — telemetry
               and `reported` are hardware-only, and the security rules block
               the browser from writing them at all.

               An earlier version bailed out here whenever the simulator was
               active, which silently discarded real setpoint changes on any
               chamber that had no heartbeat yet. */
            try {
                if (command === 'setSystemMode' || command === 'setTarget') {
                    /* Always write the COMPLETE desired map, never dotted
                       sub-paths. The security rules require the resulting map
                       to carry all five keys; a dotted update only satisfies
                       that if the document already had a full map, so on a
                       hand-created chamber it would fail with a bare
                       permission-denied. Sending the whole map makes the write
                       valid whatever state the document was in. */
                    const s = ui.state;
                    await updateDoc(chamberRef, {
                        desired: {
                            mode: payload.mode || s.desiredMode || s.mode || 'AUTO',
                            targetMode: payload.targetMode || s.targetMode || 'relative',
                            targetValue: typeof payload.targetValue === 'number'
                                ? payload.targetValue
                                : (typeof s.targetValue === 'number' ? s.targetValue : 10),
                            updatedAt: serverTimestamp(),
                            updatedBy: uid
                        }
                    });
                } else {
                    // Imperative one-shots (calibrate / reboot / toggleDevice).
                    await addDoc(collection(db, 'chambers', chamberId, 'commands'), {
                        type: command,
                        payload,
                        status: 'pending',
                        createdAt: serverTimestamp(),
                        createdBy: uid
                    });
                }
                ui.setDbStatus(true);
                return { ok: true };
            } catch (err) {
                console.error('[chamber-store] write rejected:', err);
                ui.setDbStatus(false, writeErrorLabel(err));
                return { ok: false, reason: err.code || err.message };
            }
        }
    };
    ui.setBackendAdapter(adapter);

    /* ------------------------------------------------------------------
       Reader: chamber document (config + desired + reported + heartbeat)
       ------------------------------------------------------------------ */
    let missing = false;

    const unsubChamber = onSnapshot(chamberRef, (snap) => {
        ui.setDbStatus(!snap.metadata.fromCache);

        if (!snap.exists()) {
            missing = true;
            showMissingChamber(chamberId);
            return;
        }
        missing = false;
        const data = snap.data();

        document.getElementById('chamber-title').innerText = data.name || chamberId;
        document.getElementById('chamber-location').innerText = data.location || 'מיקום לא הוגדר';
        document.title = `${data.name || chamberId} — בקרת תא אקלים`;

        if (typeof data.sensorCount === 'number' && data.sensorCount !== ui.state.sensorCount) {
            ui.renderSensorRows(data.sensorCount, ui.state.sensorLabels);
        }

        ui.applyDesired(data.desired);
        ui.applyReported(data.reported);

        evaluateHeartbeat(data.lastSeen);
    }, (err) => {
        console.error('[chamber-store] chamber listener error:', err);
        ui.setDbStatus(false, readErrorLabel(err));
    });

    /* ------------------------------------------------------------------
       Reader: live telemetry
       ------------------------------------------------------------------ */
    const unsubTelemetry = onSnapshot(telemetryRef, (snap) => {
        if (!snap.exists()) return;
        // Ignore hardware readings while the operator is running a simulation.
        if (!ui.isLive()) return;
        ui.applyTelemetry(snap.data());
    }, (err) => {
        console.error('[chamber-store] telemetry listener error:', err);
    });

    /* ------------------------------------------------------------------
       Heartbeat: lastSeen goes stale on its own, so poll the clock.
       ------------------------------------------------------------------ */
    let lastSeenMs = 0;
    let autoSimApplied = false;

    function evaluateHeartbeat(lastSeen) {
        if (lastSeen && typeof lastSeen.toMillis === 'function') {
            lastSeenMs = lastSeen.toMillis();
        }
        refreshHeartbeat();
    }

    function refreshHeartbeat() {
        if (missing) return;
        const online = lastSeenMs > 0 && (Date.now() - lastSeenMs) < HEARTBEAT_TIMEOUT_MS;

        ui.setPiOnline(online, lastSeenMs ? `מנותק (${agoLabel(lastSeenMs)})` : 'לא נרשם בקר');

        /* A chamber whose Pi has never checked in has no telemetry at all, so
           live mode would show a permanently blank panel. Fall back to the
           simulator once, and only in that case — if the Pi has ever been
           seen we keep showing its last known values and simply report it as
           offline, which is the honest thing to display. */
        if (!online && lastSeenMs === 0 && ui.isLive() && !autoSimApplied) {
            autoSimApplied = true;
            ui.setSourceMode('sim');
            simulator.start();
        }

        // A controller that comes back takes over from the simulator.
        if (online && autoSimApplied && !ui.isLive()) {
            autoSimApplied = false;
            simulator.stop();
            ui.setSourceMode('live');
        }
    }

    const heartbeatTimer = setInterval(refreshHeartbeat, 5000);

    // Paint the initial source banner and sensor rows.
    ui.setSourceMode(ui.state.source);

    return function detach() {
        unsubChamber();
        unsubTelemetry();
        clearInterval(heartbeatTimer);
        simulator.stop();
    };
}

/**
 * Renders the chamber switcher strip. Read-only; a failure here must not
 * break the dashboard, so errors are swallowed after logging.
 */
export async function listChamberTabs(currentId, host) {
    if (!host) return;
    try {
        const snap = await getDocs(query(collection(db, 'chambers'), orderBy('name')));
        host.innerHTML = snap.docs.map((d) => {
            const active = d.id === currentId;
            const name = (d.data().name || d.id);
            return `<a href="chamber.html?id=${encodeURIComponent(d.id)}" class="tab"${
                active ? ' aria-current="page"' : ''
            }>${escapeHtml(name)}</a>`;
        }).join('');
    } catch (err) {
        console.error('[chamber-store] could not list chambers:', err);
    }
}

function showMissingChamber(chamberId) {
    document.getElementById('chamber-title').innerText = 'תא לא נמצא';
    const banner = document.getElementById('source-banner');
    const text = document.getElementById('source-banner-text');
    const btn = document.getElementById('btn-toggle-sim');
    if (!banner) return;
    banner.classList.remove('hidden');
    banner.className = 'banner banner-error';
    text.innerHTML = `<i class="fa-solid fa-circle-exclamation"></i> לא קיים תא אקלים בשם <code dir="ltr">${escapeHtml(chamberId)}</code>. יש לחזור לרשימת התאים.`;
    btn.className = 'banner-btn';
    btn.innerText = 'חזרה לרשימה';
    btn.onclick = () => location.assign('index.html');
}

function writeErrorLabel(err) {
    return err && err.code === 'permission-denied' ? 'אין הרשאת כתיבה' : 'שגיאת כתיבה';
}

function readErrorLabel(err) {
    return err && err.code === 'permission-denied' ? 'אין הרשאת קריאה' : 'שגיאת קריאה';
}

function agoLabel(ms) {
    const secs = Math.round((Date.now() - ms) / 1000);
    if (secs < 90) return `לפני ${secs} שנ׳`;
    const mins = Math.round(secs / 60);
    if (mins < 90) return `לפני ${mins} דק׳`;
    return `לפני ${Math.round(mins / 60)} שע׳`;
}

function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}
