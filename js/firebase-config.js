/* ==========================================================================
   Firebase initialization — shared by every page.

   Loaded as an ES module straight from the gstatic CDN, so there is no npm
   install and no build step. The apiKey below is NOT a secret: it only
   identifies the project. Access is enforced entirely by firestore.rules.
   ========================================================================== */

import { initializeApp } from 'https://www.gstatic.com/firebasejs/12.18.0/firebase-app.js';
import { getAuth, connectAuthEmulator } from 'https://www.gstatic.com/firebasejs/12.18.0/firebase-auth.js';
import { getFirestore, connectFirestoreEmulator } from 'https://www.gstatic.com/firebasejs/12.18.0/firebase-firestore.js';

const firebaseConfig = {
    apiKey: 'AIzaSyAlWfYpecZ1IxjGGsLSPUbJwzStarRlOoU',
    authDomain: 'climate-chambers-1.firebaseapp.com',
    projectId: 'climate-chambers-1',
    storageBucket: 'climate-chambers-1.firebasestorage.app',
    messagingSenderId: '852945839328',
    appId: '1:852945839328:web:9bfd7784dd3b8e539adc85',
    measurementId: 'G-Q15EV8EX70'
};

export const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
export const db = getFirestore(app);

/* Emulator mode is opt-in via ?emu=1 and remembered for the session, so it
   survives navigation between pages. Without the flag, localhost talks to the
   real project — which is what you normally want when testing against
   production data. Add ?emu=0 to leave emulator mode. */
export const usingEmulators = (() => {
    const flag = new URLSearchParams(location.search).get('emu');
    if (flag === '1') sessionStorage.setItem('useEmulators', '1');
    if (flag === '0') sessionStorage.removeItem('useEmulators');
    return sessionStorage.getItem('useEmulators') === '1';
})();

if (usingEmulators) {
    connectAuthEmulator(auth, 'http://127.0.0.1:9099', { disableWarnings: true });
    connectFirestoreEmulator(db, '127.0.0.1', 8080);
    console.warn('[firebase] Using LOCAL EMULATORS — data is not production data.');
}

/* Heartbeat window: a chamber counts as online if its Pi wrote lastSeen
   within this many milliseconds. Shared by index.html and chamber.html.
   Comfortably more than three agent cycles (LOOP_INTERVAL_S defaults to 10),
   so one slow network round-trip does not flap the chamber offline. */
export const HEARTBEAT_TIMEOUT_MS = 45000;
