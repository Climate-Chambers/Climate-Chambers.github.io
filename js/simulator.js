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

/* The demo's dynamics, and the one place they are written down: half a degree
   a second toward the target, exactly like pi/pi_connect.py. The dashboard
   states these figures to the operator (renderSimModelNote in chamber.html),
   which only stays true if the text is generated from the same constants the
   loop below applies — so keep them here, not there. */
export const SIM_MODEL = {
    ratePerSecondC: 0.5,
    intervalS: TICK_MS / 1000,
    deadbandC: 0.3          // the deadband in evaluateControlLoop()
};

/**
 * @param {object} ui  the window.ChamberUI bridge exposed by chamber.html
 */
export function createSimulator(ui) {
    const { state } = ui;
    let timer = null;

    // So the dashboard can describe this model while it is the one running.
    state.localSimModel = SIM_MODEL;

    /* Advances the demo one step. Internal sensor values are written into their
       fields, which are authoritative in simulator mode. Ambient is owned here
       rather than read from the DOM, because the ambient fields are read-only
       displays of a measured value — the same division of responsibility the
       real Pi has. */
    function tick() {
        /* Outdoors: the operator's demo override when one is set, otherwise a
           slow drift, mirroring virtual-pi.mjs / pi_connect.py. Honouring the
           override here as well means the same demo works whether or not a
           controller is connected. */
        if (typeof state.simAmbient === 'number') {
            state.ambientTemp = state.simAmbient;
        } else {
            state.ambientTemp += (Math.random() - 0.5) * 0.08;
        }
        state.ambientRH = Math.min(95, Math.max(15, state.ambientRH + (Math.random() - 0.5) * 0.3));

        const heating = state.targetHeater > 0.5;
        const venting = state.targetFanTop > 0.5;

        /* Half a degree a second toward the target and stop there. In AUTO the
           target is the limit; in a manual mode there is none, except that
           ventilation can never pull the chamber below the outside air. */
        const delta = SIM_MODEL.ratePerSecondC * SIM_MODEL.intervalS;
        const auto = state.mode === 'AUTO';

        for (let i = 1; i <= state.sensorCount; i++) {
            const tEl = document.getElementById(`s${i}-temp`);
            const rEl = document.getElementById(`s${i}-rh`);
            if (!tEl || !rEl) continue;

            let t = parseFloat(tEl.value);
            let r = parseFloat(rEl.value);
            if (isNaN(t) || isNaN(r)) continue;

            if (heating) {
                t = auto ? Math.min(t + delta, state.calculatedTargetTemp) : t + delta;
                r = Math.max(5, r - 0.18);
            } else if (venting) {
                const floor = auto
                    ? Math.max(state.ambientTemp, state.calculatedTargetTemp)
                    : state.ambientTemp;
                t = Math.max(t - delta, floor);
                r += (state.ambientRH - r) * 0.10;
            }

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
