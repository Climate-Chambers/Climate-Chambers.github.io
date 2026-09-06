/* ==========================================================================
   Local physics simulator — the demo / offline fallback.

   This is the thermal model that used to run unconditionally inside the
   dashboard. It now runs ONLY when the chamber has no reachable controller,
   or when an operator explicitly asks for it, and it never writes to
   Firestore. That guarantees a demonstration can never be mistaken for real
   chamber data, nor overwrite it.

   The real equivalent of this loop lives on the Raspberry Pi
   (pi/chamber_agent.py) and in tools/virtual-pi.mjs.
   ========================================================================== */

const TICK_MS = 2500;

/**
 * @param {object} ui  the window.ChamberUI bridge exposed by chamber.html
 */
export function createSimulator(ui) {
    const { state } = ui;
    let timer = null;

    /* Advances the thermal model one step. Internal sensor values are written
       into their (editable) fields, which are authoritative in simulator mode.
       Ambient is owned here rather than read from the DOM, because the ambient
       fields are read-only displays of a measured value — the same division of
       responsibility the real Pi has. */
    function tick() {
        // Slow outdoor drift, mirroring virtual-pi.mjs / chamber_agent.py.
        state.ambientTemp += (Math.random() - 0.5) * 0.08;
        state.ambientRH = Math.min(95, Math.max(15, state.ambientRH + (Math.random() - 0.5) * 0.3));

        const heating = state.targetHeater > 0.5;
        const venting = state.targetFanTop > 0.5;

        for (let i = 1; i <= state.sensorCount; i++) {
            const tEl = document.getElementById(`s${i}-temp`);
            const rEl = document.getElementById(`s${i}-rh`);
            if (!tEl || !rEl) continue;

            let t = parseFloat(tEl.value);
            let r = parseFloat(rEl.value);
            if (isNaN(t) || isNaN(r)) continue;

            if (heating) {
                t += 0.22 + Math.random() * 0.12;
                r = Math.max(5, r - 0.18);
            } else if (venting) {
                t += (state.ambientTemp - t) * 0.10;
                r += (state.ambientRH - r) * 0.10;
            } else {
                t += (state.ambientTemp - t) * 0.015;
            }
            t += (Math.random() - 0.5) * 0.06;

            tEl.value = t.toFixed(1);
            rEl.value = r.toFixed(1);
        }

        // Always re-render: ambient drifts even on a tick where no sensor
        // field changed (e.g. every field is focused for editing).
        ui.updateCalculations();
    }

    function randomize() {
        const base = state.calculatedTargetTemp - 2 + Math.random() * 4;
        for (let i = 1; i <= state.sensorCount; i++) {
            const tEl = document.getElementById(`s${i}-temp`);
            const rEl = document.getElementById(`s${i}-rh`);
            if (tEl) tEl.value = (base + (Math.random() * 1.2 - 0.6)).toFixed(1);
            if (rEl) rEl.value = (48 + Math.random() * 6).toFixed(1);
        }
        ui.updateCalculations();
    }

    return {
        start() {
            if (timer) return;
            // Seed plausible readings so the panel isn't blank.
            if (!state.sensors.length || !isFinite(state.sensors[0].temp)) randomize();
            timer = setInterval(tick, TICK_MS);
            tick();
        },
        stop() {
            clearInterval(timer);
            timer = null;
        },
        get running() {
            return timer !== null;
        },
        randomize,
        tick
    };
}
