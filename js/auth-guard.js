/* ==========================================================================
   Authentication gate — the single chokepoint for every protected page.

   Access model: signup is open, but a brand-new user's profile document is
   written with approved:false and the security rules make that field
   unwritable by any client afterwards. An administrator flips it to true in
   the Firebase console, which bypasses rules. Until then the user is signed
   in but can read nothing except their own profile.
   ========================================================================== */

import {
    onAuthStateChanged,
    signOut
} from 'https://www.gstatic.com/firebasejs/12.18.0/firebase-auth.js';
import {
    doc,
    getDoc,
    setDoc,
    serverTimestamp
} from 'https://www.gstatic.com/firebasejs/12.18.0/firebase-firestore.js';

import { auth, db } from './firebase-config.js?v=20260908b';

/* Shape of a freshly created profile. Must match isValidNewUser() in
   firestore.rules — approved MUST be false or the write is rejected. */
export function newProfile(user, displayName) {
    return {
        email: user.email,
        displayName: (displayName || user.email.split('@')[0]).slice(0, 60),
        approved: false,
        createdAt: serverTimestamp()
    };
}

/**
 * Resolves with { user, profile } once a signed-in AND approved user is
 * present. Never resolves otherwise: unauthenticated visitors are redirected
 * to login.html, and pending users get the waiting screen rendered over the
 * page. Callers can therefore treat resolution as proof of access.
 */
export function requireApprovedUser() {
    return new Promise((resolve) => {
        onAuthStateChanged(auth, async (user) => {
            if (!user) {
                const next = encodeURIComponent(location.pathname + location.search);
                location.replace(`login.html?next=${next}`);
                return;
            }

            let profile = null;
            try {
                const ref = doc(db, 'users', user.uid);
                const snap = await getDoc(ref);

                if (!snap.exists()) {
                    // Covers accounts created directly in the Firebase console,
                    // which never ran the signup path in login.html.
                    profile = newProfile(user);
                    await setDoc(ref, profile);
                } else {
                    profile = snap.data();
                }
            } catch (err) {
                renderBlockingScreen(
                    'שגיאה בטעינת הפרופיל',
                    'לא ניתן לקרוא את פרטי המשתמש מ-Firestore. ייתכן שכללי האבטחה טרם פורסמו.',
                    String(err && err.code ? err.code : err)
                );
                return;
            }

            if (profile.approved !== true) {
                renderPendingApproval(user);
                return;
            }

            resolve({ user, profile });
        });
    });
}

export async function signOutAndRedirect() {
    await signOut(auth);
    location.replace('login.html');
}

/* Replaces the whole document with a centred notice. Used for terminal states
   where showing a half-populated dashboard would be misleading. */
function renderBlockingScreen(title, body, detail, extraHtml = '') {
    document.body.innerHTML = `
        <div class="min-h-screen flex items-center justify-center p-6 bg-slate-100">
            <div class="bg-white rounded-2xl border border-slate-200 shadow-sm max-w-lg w-full p-8 text-center">
                <div class="w-14 h-14 rounded-2xl bg-amber-100 border border-amber-300 flex items-center justify-center mx-auto mb-5">
                    <i class="fa-solid fa-hourglass-half text-2xl text-amber-600"></i>
                </div>
                <h1 class="text-xl font-extrabold text-slate-900 mb-2">${title}</h1>
                <p class="text-sm text-slate-600 leading-relaxed mb-4">${body}</p>
                ${detail ? `<p class="text-xs font-mono text-slate-400 mb-5 break-all">${detail}</p>` : ''}
                ${extraHtml}
            </div>
        </div>`;
}

/* Address an approver must be contacted at. The approval itself is a manual
   Firestore console action, so the user has no in-app way to progress — the
   contact instruction is the only actionable step and must be unmissable. */
const APPROVER_EMAIL = 'yehudah@volcani.agri.gov.il';

function renderPendingApproval(user) {
    const subject = encodeURIComponent('אישור הרשמה למערכת בקרת תאי אקלים');
    const body = encodeURIComponent(
        `שלום,\n\nנרשמתי למערכת בקרת תאי האקלים וברצוני לבקש אישור גישה.\n\nכתובת הדוא״ל שנרשמה: ${user.email}\n\nתודה.`
    );

    renderBlockingScreen(
        'החשבון ממתין לאישור מנהל',
        'ההרשמה הושלמה בהצלחה, אך הגישה לתאי האקלים טעונה אישור של מנהל המערכת.',
        user.email,
        `<div class="bg-amber-50 border-2 border-amber-300 rounded-xl p-4 mb-5 text-right">
            <p class="text-sm font-extrabold text-amber-900 mb-2 flex items-center gap-2">
                <i class="fa-solid fa-envelope text-amber-700"></i>
                נדרשת פנייה לקבלת אישור
            </p>
            <p class="text-sm text-amber-900 leading-relaxed">
                כדי לאשר את ההרשמה יש לפנות בדוא״ל לכתובת:
            </p>
            <a href="mailto:${APPROVER_EMAIL}?subject=${subject}&body=${body}"
               class="mt-2 block text-base font-extrabold text-amber-900 underline decoration-2 decoration-amber-500 hover:decoration-amber-700 break-all"
               dir="ltr">${APPROVER_EMAIL}</a>
            <p class="text-xs text-amber-800 mt-2.5">
                יש לציין בפנייה את כתובת הדוא״ל שנרשמה. לאחר קבלת האישור יש לרענן את הדף.
            </p>
        </div>
        <div class="flex items-center justify-center gap-3">
            <a href="mailto:${APPROVER_EMAIL}?subject=${subject}&body=${body}"
               class="px-4 py-2 rounded-xl bg-amber-600 hover:bg-amber-700 text-white text-xs font-bold transition shadow-sm">
                <i class="fa-solid fa-paper-plane"></i> שליחת בקשת אישור
            </a>
            <button id="guard-refresh" class="px-4 py-2 rounded-xl bg-sky-600 hover:bg-sky-700 text-white text-xs font-bold transition shadow-sm">
                <i class="fa-solid fa-rotate-right"></i> בדוק שוב
            </button>
            <button id="guard-signout" class="px-4 py-2 rounded-xl bg-white hover:bg-slate-50 text-slate-700 text-xs font-bold border border-slate-300 transition shadow-sm">
                התנתק
            </button>
        </div>`
    );
    document.getElementById('guard-refresh').onclick = () => location.reload();
    document.getElementById('guard-signout').onclick = () => signOutAndRedirect();
}

export { renderBlockingScreen };
