/* ==========================================================================
   Chamber picker — live card grid for index.html.

   Subscribes to the chambers collection, plus one telemetry/current listener
   per chamber so each card can show a real reading. Chamber count is small
   (one per physical chamber), so a listener per card is well within budget.
   ========================================================================== */

import {
    collection,
    doc,
    onSnapshot,
    orderBy,
    query
} from 'https://www.gstatic.com/firebasejs/12.18.0/firebase-firestore.js';

import { db, HEARTBEAT_TIMEOUT_MS } from './firebase-config.js?v=20260915a';

const MODE_LABELS = {
    AUTO: 'אוטומטי',
    VENTILATION: 'אוורור ידני',
    HEATING: 'חימום ידני',
    OFF: 'כבוי'
};

const MODE_STYLES = {
    AUTO: 'bg-sky-100 text-sky-800 border-sky-300',
    VENTILATION: 'bg-sky-100 text-sky-800 border-sky-300',
    HEATING: 'bg-orange-100 text-orange-800 border-orange-300',
    OFF: 'bg-slate-200 text-slate-700 border-slate-300'
};

const DEMAND_LABELS = {
    HEATING: 'חימום פעיל',
    VENTILATION: 'אוורור פעיל',
    IDLE: 'בטווח היעד'
};

/* chamberId -> { chamber, telemetry, unsubTelemetry } */
const rows = new Map();

let gridEl = null;
let emptyEl = null;
let countEl = null;
let heartbeatTimer = null;
let hasLoaded = false;

export function mountChamberList({ grid, empty, count }) {
    gridEl = grid;
    emptyEl = empty;
    countEl = count;

    const q = query(collection(db, 'chambers'), orderBy('name'));

    const unsub = onSnapshot(q, (snap) => {
        snap.docChanges().forEach((change) => {
            const id = change.doc.id;

            if (change.type === 'removed') {
                const row = rows.get(id);
                if (row && row.unsubTelemetry) row.unsubTelemetry();
                rows.delete(id);
                return;
            }

            const existing = rows.get(id);
            if (existing) {
                existing.chamber = change.doc.data();
            } else {
                const row = { chamber: change.doc.data(), telemetry: null, unsubTelemetry: null };
                rows.set(id, row);
                // Telemetry lives in a subcollection, so it needs its own listener.
                row.unsubTelemetry = onSnapshot(
                    doc(db, 'chambers', id, 'telemetry', 'current'),
                    (tSnap) => {
                        row.telemetry = tSnap.exists() ? tSnap.data() : null;
                        render();
                    },
                    () => { /* telemetry unreadable — card still renders without it */ }
                );
            }
        });
        hasLoaded = true;
        render();
    }, (err) => {
        gridEl.innerHTML = '';
        emptyEl.classList.remove('hidden');
        emptyEl.innerHTML = `
            <i class="fa-solid fa-triangle-exclamation text-3xl text-red-400 mb-3"></i>
            <p class="text-sm font-bold text-slate-800 mb-1">לא ניתן לטעון את רשימת תאי האקלים</p>
            <p class="text-xs font-mono text-slate-400">${err.code || err.message}</p>`;
    });

    // lastSeen freshness is time-dependent, so repaint even without new data.
    heartbeatTimer = setInterval(render, 10000);

    return () => {
        unsub();
        clearInterval(heartbeatTimer);
        rows.forEach((row) => row.unsubTelemetry && row.unsubTelemetry());
        rows.clear();
    };
}

export function isOnline(chamber) {
    const lastSeen = chamber && chamber.lastSeen;
    if (!lastSeen || typeof lastSeen.toMillis !== 'function') return false;
    return Date.now() - lastSeen.toMillis() < HEARTBEAT_TIMEOUT_MS;
}

function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

function fmt(value, digits, suffix) {
    return typeof value === 'number' && isFinite(value)
        ? value.toFixed(digits) + suffix
        : '—';
}

function render() {
    if (!gridEl) return;

    const entries = [...rows.entries()];
    countEl.innerText = entries.length ? `${entries.length} תאים` : '';
    emptyEl.classList.toggle('hidden', entries.length > 0);

    // Distinguish "still loading" from "loaded, but genuinely empty".
    if (!entries.length && hasLoaded) {
        emptyEl.innerHTML = `
            <i class="fa-solid fa-warehouse text-3xl text-slate-300 mb-3"></i>
            <p class="text-sm font-bold text-slate-700 mb-1">אין תאי אקלים מוגדרים</p>
            <p class="text-xs text-slate-500">יש להריץ את סקריפט האתחול
                <code class="font-mono bg-slate-100 px-1.5 py-0.5 rounded">tools/seed-chambers.mjs</code>,
                או להמתין שבקר Raspberry Pi ירשום את עצמו אוטומטית.</p>`;
    }

    gridEl.innerHTML = entries.map(([id, row]) => card(id, row)).join('');
}

function card(id, { chamber, telemetry }) {
    const online = isOnline(chamber);
    const reported = chamber.reported || {};
    const desired = chamber.desired || {};

    // Reported mode is the truth; desired is only an outstanding request.
    const mode = reported.mode || desired.mode || 'OFF';

    /* A mismatch between the two only means "in flight" while there is a
       controller online to answer it. Once the Pi goes quiet, `reported` is
       frozen at the last thing the hardware actually did, so a mismatch is
       permanent by definition — the earlier version showed "ממתין לבקר"
       forever on every chamber whose controller had ever run and then
       stopped, even though the mode change was accepted and stored. */
    const mismatch = Boolean(desired.mode && reported.mode && desired.mode !== reported.mode);
    const awaitingAck = mismatch && online;
    const savedForLater = mismatch && !online;

    const avgTemp = telemetry ? telemetry.avgTemp : undefined;
    const avgRH = telemetry ? telemetry.avgRH : undefined;
    const ambient = telemetry && telemetry.ambient ? telemetry.ambient.temp : undefined;

    const demand = reported.demand && DEMAND_LABELS[reported.demand];

    return `
    <a href="chamber.html?id=${encodeURIComponent(id)}"
       class="group bg-white rounded-2xl border border-slate-200 p-5 shadow-sm hover:shadow-md hover:border-sky-300 transition flex flex-col gap-4">

        <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
                <h3 class="text-base font-extrabold text-slate-900 truncate group-hover:text-sky-700 transition">
                    ${escapeHtml(chamber.name || id)}
                </h3>
                <p class="text-xs text-slate-500 font-medium truncate">
                    <i class="fa-solid fa-location-dot text-slate-400"></i>
                    ${escapeHtml(chamber.location || 'מיקום לא הוגדר')}
                </p>
            </div>
            <span class="shrink-0 flex items-center gap-1.5 text-[11px] font-bold px-2 py-1 rounded-full border
                ${online
                    ? 'bg-emerald-50 text-emerald-700 border-emerald-300'
                    : 'bg-slate-100 text-slate-500 border-slate-300'}">
                <span class="w-2 h-2 rounded-full ${online ? 'bg-emerald-500 animate-pulse' : 'bg-slate-400'}"></span>
                ${online ? 'מקושר' : 'לא מקושר'}
            </span>
        </div>

        <div class="grid grid-cols-3 gap-2 text-center">
            <div class="bg-teal-50 border border-teal-200 rounded-xl p-2.5">
                <span class="text-[10px] font-bold text-teal-700 uppercase tracking-wider block">פנים</span>
                <span class="text-base font-black text-teal-700">${fmt(avgTemp, 1, '°C')}</span>
                <span class="text-[11px] font-bold text-teal-700/70 block">${fmt(avgRH, 0, '%')}</span>
            </div>
            <div class="bg-amber-50 border border-amber-200 rounded-xl p-2.5">
                <span class="text-[10px] font-bold text-amber-700 uppercase tracking-wider block">חוץ</span>
                <span class="text-base font-black text-amber-700">${fmt(ambient, 1, '°C')}</span>
                <span class="text-[11px] font-bold text-amber-700/70 block">סביבה</span>
            </div>
            <div class="bg-slate-50 border border-slate-200 rounded-xl p-2.5">
                <span class="text-[10px] font-bold text-slate-500 uppercase tracking-wider block">יעד</span>
                <span class="text-base font-black text-slate-700">${fmt(reported.calculatedTargetTemp, 1, '°C')}</span>
                <span class="text-[11px] font-bold text-slate-400 block">${escapeHtml(demand || '—')}</span>
            </div>
        </div>

        <div class="flex items-center justify-between gap-2 pt-1 border-t border-slate-100">
            <span class="text-[11px] font-bold px-2.5 py-1 rounded-full border ${MODE_STYLES[mode] || MODE_STYLES.OFF}">
                מצב: ${MODE_LABELS[mode] || mode}
            </span>
            ${awaitingAck
                ? `<span class="text-[11px] font-bold text-amber-700 bg-amber-50 border border-amber-300 px-2 py-1 rounded-full"
                         title="השינוי נשלח לבקר וממתין לאישורו">
                       <i class="fa-solid fa-circle-notch fa-spin"></i> ממתין לבקר
                   </span>`
                : savedForLater
                ? `<span class="text-[11px] font-bold text-slate-600 bg-slate-100 border border-slate-300 px-2 py-1 rounded-full"
                         title="השינוי נשמר ויוחל כשהבקר יתחבר">
                       <i class="fa-regular fa-clock"></i> מבוקש: ${escapeHtml(MODE_LABELS[desired.mode] || desired.mode)}
                   </span>`
                : `<span class="text-xs font-bold text-sky-600 group-hover:text-sky-800 transition">
                       פתח בקרה <i class="fa-solid fa-arrow-left"></i>
                   </span>`}
        </div>
    </a>`;
}
