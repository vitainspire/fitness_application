import "./style.css";
import { elevenlabsSTT, elevenlabsTTS, stopElevenLabsAudio } from "./elevenlabs_tester.js";

// Backend origin. In dev, Vite reads this from .env.local (copy .env.example);
// in production it's set at build time via the platform's env vars (Vercel).
// Falls back to same-origin ("") if unset, so a same-domain deploy still works
// without any config.
const API_BASE = import.meta.env.VITE_API_URL || "";

// The chatbot (/v1/rehab/*) can be deployed as a SEPARATE backend instance
// from everything else — see app/platform/config.py's `service_role`. That
// split only takes effect once a second Railway service actually exists and
// this is pointed at it; until VITE_CHAT_API_URL is set, chat calls keep
// going to the same place as everything else, so nothing changes today.
// The whole point of the split: a slow AI reply can then never make login,
// the dashboard, or logging wait behind it, because they're no longer
// sharing the same small pool of server threads.
const CHAT_API_BASE = import.meta.env.VITE_CHAT_API_URL || API_BASE;

// Any backend-relative path (e.g. a self-hosted demo GIF served from the
// Flask app's own /static/) needs the backend origin prefixed once this page
// no longer lives on that same origin. An already-absolute URL (the public-
// domain demo frames, hosted on GitHub) is returned unchanged.
function assetUrl(path) {
  if (!path) return path;
  return /^https?:\/\//.test(path) ? path : API_BASE + path;
}

// A managed account only ever sees 2 of the 5 nav buttons (home + start
// workout — chat/videos/profile are hidden, see MANAGED_HIDDEN_SCREENS
// below), so justify-between would stretch them to the pill's opposite
// edges with a huge gap; justify-center keeps the visible ones together.
function navOnClass() {
  const justify = IS_MANAGED_ACCOUNT ? "justify-center gap-[56px]" : "justify-between";
  return `fixed bottom-4 left-1/2 -translate-x-1/2 w-[calc(100%-24px)] max-w-[400px] z-50 flex ${justify} items-center px-md py-sm bg-white/85 backdrop-blur-xl border border-white/80 shadow-lg rounded-full`;
}
const DEV = "web-" + (localStorage.getItem("did") || (()=>{const d=Math.random().toString(36).slice(2,10);localStorage.setItem("did",d);return d})());
let AT = localStorage.getItem("at"), RT = localStorage.getItem("rt"), EMAIL = localStorage.getItem("em") || "";
// Set once per session by route() below, from /v1/profile's is_managed_account.
// A managed account has nothing usable left in Settings (no real password to
// change it with, logout/delete already blocked) — confirmed directly: the
// whole screen is removed from their experience, not just those two rows.
let IS_MANAGED_ACCOUNT = false;

// Found live: two tabs sharing one login can each get a 401 and call
// /v1/auth/refresh with the SAME (stale) refresh token at nearly the same
// moment. The backend's reuse-grace window then treats the second tab's
// request as "my own retry of a lost response" and silently advances a
// DIFFERENT, brand-new token the first tab already holds but hasn't used
// yet — which can later look like token reuse/theft to the server and log
// an innocent user out everywhere. Rather than loosen that server-side
// theft check (risking it miss a real replay), fix it at the source: keep
// every tab's in-memory tokens in sync the instant any tab rotates them,
// so a tab that's about to refresh can first notice another tab already
// did it for it.
window.addEventListener("storage", (e) => {
  if (e.key === "at") AT = e.newValue;
  if (e.key === "rt") RT = e.newValue;
});
let HOME_ITEMS = [];
let HOME_CUSTOM_ITEMS = [];
let CACHED_PROGRESSION = [];
let ALL_TARGET_CHANGES = [];
let CURRENT_TARGET_FILTER = "all";
const TAX_UNIT_LOOKUP = {
  walk: "km",
  running: "km",
  neck_isometric: "sec",
  shoulder_circles: "reps",
  lower_back_side_stretch: "sec",
  glute_bridge: "reps",
  dead_bug: "reps",
  hamstring_stretch: "sec",
  quad_stretch: "sec",
  calf_stretch: "sec",
  ankle_circles: "reps",
  wrist_circles: "reps",
};
const $ = id => document.getElementById(id);

// User-supplied or model-generated text (a custom exercise name, a
// notification, a RAG chat answer) must never go into innerHTML or an inline
// event-handler attribute unescaped — that's a stored XSS hole. Shared by
// every render path in this file for exactly that reason — one definition,
// not a second copy that could silently drift or shadow this one.
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

const ICONS = {
  walk:"directions_walk", running:"directions_run", neck_isometric:"self_improvement",
  shoulder_circles:"sports_gymnastics", lower_back_side_stretch:"accessibility_new",
  glute_bridge:"fitness_center", dead_bug:"airline_seat_flat",
  hamstring_stretch:"airline_seat_recline_extra", quad_stretch:"accessibility",
  calf_stretch:"height", ankle_circles:"rotate_right", wrist_circles:"back_hand"
};
const icon = t => ICONS[t] || "fitness_center";

// In-app replacement for the browser's confirm(): resolves true on the confirm button,
// false on Cancel, Escape or a tap outside. A second call while one is open cancels the
// first (resolves it false) instead of stacking two dialogs.
let appConfirmResolve = null;

function appConfirm({ message, title = "", confirmText = "OK", cancelText = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    if (appConfirmResolve) appConfirmResolve(false);
    appConfirmResolve = resolve;
    const titleEl = $("app-confirm-title");
    titleEl.textContent = title;
    titleEl.classList.toggle("hidden", !title);
    $("app-confirm-msg").textContent = message;
    const ok = $("app-confirm-ok");
    ok.textContent = confirmText;
    ok.classList.toggle("bg-error", danger);
    ok.classList.toggle("text-white", danger);
    ok.classList.toggle("bg-primary", !danger);
    ok.classList.toggle("text-on-primary", !danger);
    $("app-confirm-cancel").textContent = cancelText;
    const m = $("app-confirm-modal");
    m.classList.remove("hidden");
    m.classList.add("flex");
    // A destructive question starts on Cancel so a stray Enter can't confirm it.
    (danger ? $("app-confirm-cancel") : ok).focus();
  });
}

function closeAppConfirm(result) {
  const m = $("app-confirm-modal");
  m.classList.add("hidden");
  m.classList.remove("flex");
  const resolve = appConfirmResolve;
  appConfirmResolve = null;
  if (resolve) resolve(result);
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && appConfirmResolve) closeAppConfirm(false);
});

function toast(msg, bad) {
  const t = $("toast");
  t.textContent = msg;
  t.style.background = bad ? "#ffdad6" : "#d4ff33";
  t.style.color = bad ? "#93000a" : "#5e7400";
  t.className = "toast show";
  setTimeout(() => t.className = "toast", 2600);
}

let refreshInFlight = null;

async function doRefreshToken() {
  const r = await fetch(API_BASE + "/v1/auth/refresh", {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Device-Id": DEV},
    body: JSON.stringify({refresh_token: RT}),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(d.error?.message || r.statusText), {status: r.status});
  // Refresh tokens rotate — the response carries a NEW refresh_token too, and
  // the old one is now invalid. Storing only the new access_token would make
  // the *next* refresh attempt fail with a stale, already-rotated-out token.
  AT = d.access_token; RT = d.refresh_token;
  localStorage.setItem("at", AT); localStorage.setItem("rt", RT);
}

// Serializes the actual refresh network call ACROSS EVERY TAB of this origin
// (the Web Locks API is shared by all tabs, unlike a plain JS variable) so
// two tabs never present the same refresh token to the server at once. If a
// tab wins the lock after another tab already refreshed, it skips calling
// the server again and just uses the tokens the storage listener above
// already picked up. Browsers without Web Locks fall back to the previous
// same-tab-only dedup — no worse than before this fix.
async function refreshTokenAcrossTabs() {
  const attemptedWithRT = RT;
  if (!("locks" in navigator)) {
    if (!refreshInFlight) refreshInFlight = doRefreshToken().finally(() => { refreshInFlight = null; });
    return refreshInFlight;
  }
  return navigator.locks.request("vita-auth-refresh", async () => {
    if (RT !== attemptedWithRT) return; // another tab already refreshed while we waited
    await doRefreshToken();
  });
}

async function api(method, path, body, _retried) {
  const h = {"X-Device-Id": DEV};
  if (body) h["Content-Type"] = "application/json";
  if (AT) h["Authorization"] = "Bearer " + AT;
  // Route the chatbot's own endpoints to its own backend instance (if one is
  // configured — see CHAT_API_BASE above); every other path is unaffected.
  const base = path.startsWith("/v1/rehab/") ? CHAT_API_BASE : API_BASE;
  const r = await fetch(base + path, {method, headers: h, body: body ? JSON.stringify(body) : undefined});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) {
    // Silent renewal (11.1) — the backend's own /v1/auth/refresh docstring
    // says "an active user never sees a login screen again after signup,"
    // but nothing on this side was ever actually calling it: an expired
    // access token just surfaced as a raw "Access token expired" toast,
    // mid-chat, with the user's message already gone from the input box.
    // One refresh-and-retry attempt per call, deduped across both this tab's
    // own parallel requests AND other open tabs (see refreshTokenAcrossTabs)
    // so several requests failing at once don't each rotate the refresh
    // token out from under the others.
    if (r.status === 401 && RT && !_retried) {
      try {
        await refreshTokenAcrossTabs();
        return api(method, path, body, true);
      } catch (_) {
        localStorage.clear(); AT = null; RT = null;
        go("auth");
        // Not toasted here — every existing caller already does
        // `catch (e) { toast(e.message, true) }` on failure; toasting inside
        // api() too would just show the same message to the user twice.
        throw Object.assign(new Error("Your session ended — please sign in again."), {status: 401});
      }
    }
    throw Object.assign(new Error(d.error?.message || r.statusText), {status: r.status});
  }
  return d;
}

// fetch() for the non-JSON voice proxy routes (/v1/voice/tts returns MP3 bytes,
// /v1/voice/stt takes raw audio), which api() can't carry because it always
// JSON-encodes the body and JSON-parses the reply. Same auth headers and the
// same one silent refresh-and-retry on a 401 as api(). Unlike api(), a failed
// refresh does NOT sign the user out or change screens: these calls are
// optional (the caller falls back to the free voice), and the next real api()
// call handles an ended session exactly as it always has.
async function authedFetch(path, init = {}, _retried) {
  const headers = {"X-Device-Id": DEV, ...(init.headers || {})};
  if (AT) headers["Authorization"] = "Bearer " + AT;
  const r = await fetch(API_BASE + path, {...init, headers});
  if (r.status === 401 && RT && !_retried) {
    await refreshTokenAcrossTabs();
    return authedFetch(path, init, true);
  }
  return r;
}

// A managed account (student/patient/child using someone else's invite
// link) has no use for Settings, the rehab chatbot, or the exercise
// browse library — the plan is built FOR them, not something they tune or
// explore on their own. Shared by go()'s redirect-away guard and its
// nav-button hiding below, so both stay in sync automatically.
const MANAGED_HIDDEN_SCREENS = ["profile", "chat", "videos"];

function go(name) {
  // Redirect away even if something (an old bookmark, the browser's back
  // button) tries to land here directly, not just hide the nav button that
  // normally gets here.
  if (IS_MANAGED_ACCOUNT && MANAGED_HIDDEN_SCREENS.includes(name)) { go("home"); return; }
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("on"));
  $("s-" + name).classList.add("on");
  $("nav").className = ["auth","onboard","player","gps","reset-password"].includes(name) ? "hidden" : navOnClass();
  document.querySelectorAll(".nav-btn").forEach(b => {
    const on = b.dataset.t === name;
    // Rebuilt every time (see above), so a hidden button's class set by
    // route() must be re-applied here too, not just once at login —
    // otherwise the very next go() call (home, anything) would wipe it
    // back to visible.
    const hide = MANAGED_HIDDEN_SCREENS.includes(b.dataset.t) && IS_MANAGED_ACCOUNT;
    b.className = (on
      ? "nav-btn flex flex-row items-center gap-xs bg-primary-container text-on-primary-container rounded-full px-md py-xs"
      : "nav-btn flex items-center justify-center text-secondary p-xs rounded-full")
      + " active:scale-90 duration-200" + (hide ? " hidden" : "");
  });
  window.scrollTo(0,0);
  // Every loader is fired and forgotten, so an unhandled rejection used to
  // leave the screen half-rendered and SILENT — no toast, no retry, nothing
  // to tell the user the request failed. The worst case is Settings: if
  // loadProfile() rejects, #p-body is never filled, so the user is left on
  // an empty screen with no Log out and no Delete account button, i.e. no
  // way out of the app at all. Surface the failure instead.
  const loaders = {
    home: loadHome, chat: loadChat, videos: loadVideos,
    profile: loadProfile, notifications: loadNotifications,
    relationships: loadRelationships,
  };
  const load = loaders[name];
  if (load) {
    Promise.resolve()
      .then(load)
      .catch(e => toast(e && e.message ? e.message : "Something went wrong. Please try again.", true));
  }
}

// ── notifications ──
// Placeholder client-side list: there is no real notification source yet
// (Module 8 - push/reminders - hasn't been built). This wires up the page,
// the badge, and the bell icon's link correctly now, ready to switch to a
// real fetched list the moment that backend exists.
let NOTIFICATIONS = [];

function renderNotifBadge() {
  const n = NOTIFICATIONS.length;
  const badge = $("h-notif-badge");
  badge.textContent = n > 9 ? "9+" : String(n);
  badge.classList.toggle("hidden", n === 0);
}

function loadNotifications() {
  // There is no notification backend at all yet (no model, route, sender,
  // or scheduler) — NOTIFICATIONS is always []. "No notifications yet" reads
  // as "the feature works, you just have none right now," which isn't true;
  // say plainly that it's not built yet instead of implying it is.
  $("n-body").innerHTML =
    `<div class="bg-surface-container-lowest ambient-shadow rounded-lg p-lg font-body-md text-on-surface-variant text-center">🔔 Notifications are coming soon!</div>`;
}
// The workout (dumbbell) nav button has no data-t — it launches the guided
// Player directly via its own inline onclick instead of navigating to a
// tab, so it's excluded here rather than overwritten with go(undefined).
document.querySelectorAll(".nav-btn[data-t]").forEach(b => b.onclick = () => go(b.dataset.t));

// ── auth mode & password helpers ──
let authMode = "login";
window.authMode = "login";

function setAuthMode(mode) {
  authMode = mode;
  window.authMode = mode;
  const isLogin = mode === "login";
  if ($("a-title")) $("a-title").textContent = isLogin ? "Welcome Back" : "Create Account";
  if ($("a-sub")) $("a-sub").textContent = isLogin
    ? "Sign in to access your workouts & recovery plan."
    : "Create an account to begin your personalized journey.";

  if ($("auth-tab-login")) {
    $("auth-tab-login").className = isLogin
      ? "flex-1 py-1.5 text-xs font-bold rounded-full transition-all bg-white text-on-surface shadow-xs"
      : "flex-1 py-1.5 text-xs font-bold rounded-full transition-all text-on-surface-variant hover:text-on-surface";
  }
  if ($("auth-tab-signup")) {
    $("auth-tab-signup").className = !isLogin
      ? "flex-1 py-1.5 text-xs font-bold rounded-full transition-all bg-white text-on-surface shadow-xs"
      : "flex-1 py-1.5 text-xs font-bold rounded-full transition-all text-on-surface-variant hover:text-on-surface";
  }
  if ($("a-submit-btn")) $("a-submit-btn").textContent = isLogin ? "Sign In" : "Create Account";
  if ($("a-toggle-btn")) {
    $("a-toggle-btn").textContent = isLogin
      ? "New to ViTAinspire? Create account"
      : "Already have an account? Sign in";
  }

  if ($("a-forgot-wrap")) $("a-forgot-wrap").classList.toggle("hidden", !isLogin);
  if ($("a-pw-rules-wrap")) $("a-pw-rules-wrap").classList.toggle("hidden", isLogin);
  if ($("a-err")) $("a-err").className = "hidden";
}

function toggleAuthMode() {
  setAuthMode(authMode === "login" ? "signup" : "login");
}

function submitAuthForm() {
  doAuth(authMode);
}

function togglePasswordVisibility(inputId, iconId) {
  const input = $(inputId);
  const icon = $(iconId);
  if (!input) return;
  const isPass = input.type === "password";
  input.type = isPass ? "text" : "password";
  if (icon) icon.textContent = isPass ? "visibility_off" : "visibility";
}

function checkPasswordRules(passInputId, confirmInputId, prefix) {
  const p = $(passInputId)?.value || "";
  const conf = confirmInputId ? ($(confirmInputId)?.value || "") : "";

  const rules = {
    upper: /[A-Z]/.test(p),
    lower: /[a-z]/.test(p),
    number: /[0-9]/.test(p),
    special: /[^a-zA-Z0-9]/.test(p),
    length: p.length >= 10,
    match: confirmInputId ? (p.length > 0 && p === conf) : true,
  };

  const updateBadge = (id, valid) => {
    const el = $(id);
    if (!el) return;
    const icon = el.querySelector(".material-symbols-outlined");
    if (valid) {
      el.className = "flex items-center gap-1.5 text-emerald-600 font-medium";
      if (icon) icon.textContent = "check_circle";
    } else {
      el.className = "flex items-center gap-1.5 text-secondary";
      if (icon) icon.textContent = "radio_button_unchecked";
    }
  };

  updateBadge(`${prefix}-rule-upper`, rules.upper);
  updateBadge(`${prefix}-rule-lower`, rules.lower);
  updateBadge(`${prefix}-rule-number`, rules.number);
  updateBadge(`${prefix}-rule-special`, rules.special);
  updateBadge(`${prefix}-rule-length`, rules.length);
  if (confirmInputId) {
    updateBadge(`${prefix}-rule-match`, rules.match);
  }

  return rules;
}

// ── forgot password modal ──
function openForgotPasswordModal() {
  const m = $("forgot-pw-modal");
  if (!m) return;
  if ($("fp-email")) $("fp-email").value = $("a-email")?.value?.trim() || EMAIL || "";
  if ($("fp-err")) $("fp-err").className = "hidden";
  if ($("fp-success")) $("fp-success").className = "hidden";
  const btn = $("fp-submit-btn");
  if (btn) {
    btn.disabled = false;
    btn.textContent = "Send Reset Link";
  }
  m.classList.remove("hidden");
  m.classList.add("flex");
}

function closeForgotPasswordModal() {
  const m = $("forgot-pw-modal");
  if (!m) return;
  m.classList.add("hidden");
  m.classList.remove("flex");
}

async function submitForgotPassword() {
  const email = $("fp-email")?.value?.trim();
  if (!email) {
    $("fp-err").textContent = "Please enter your email address.";
    $("fp-err").className = "rounded-lg px-md py-sm font-body-md bg-error-container text-on-error-container text-xs";
    return;
  }
  const btn = $("fp-submit-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Sending...";
  }
  if ($("fp-err")) $("fp-err").className = "hidden";
  if ($("fp-success")) $("fp-success").className = "hidden";

  try {
    await api("POST", "/v1/auth/password-reset/request", { email });
    $("fp-success").innerHTML = `
      <div class="space-y-1">
        <p class="font-bold text-emerald-950">Reset Email Dispatched!</p>
        <p>If an account exists for <span class="font-semibold underline">${email}</span>, a secure password reset link was sent from <strong class="text-emerald-950">ayush.kumar@vitainspire.com</strong>.</p>
        <p class="text-[11px] text-emerald-700 mt-1">Please check your inbox or spam folder. Click the link in the email to set your new password.</p>
      </div>
    `;
    $("fp-success").className = "rounded-lg px-md py-sm font-body-md bg-emerald-100 text-emerald-800 text-xs";
    if (btn) btn.textContent = "Link Sent";
  } catch (e) {
    $("fp-err").textContent = e.message || "Failed to send reset link.";
    $("fp-err").className = "rounded-lg px-md py-sm font-body-md bg-error-container text-on-error-container text-xs";
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Send Reset Link";
    }
  }
}

// ── reset password screen ──
async function submitNewPassword() {
  const token = $("r-token")?.value?.trim();
  const pass = $("r-pass")?.value || "";
  const confirm = $("r-pass-confirm")?.value || "";

  if (!token) {
    $("r-err").textContent = "Reset code / token is required. Please check your reset link or email.";
    $("r-err").className = "rounded-lg px-md py-sm font-body-md bg-error-container text-on-error-container text-xs";
    return;
  }
  if (!pass) {
    $("r-err").textContent = "Please enter a new password.";
    $("r-err").className = "rounded-lg px-md py-sm font-body-md bg-error-container text-on-error-container text-xs";
    return;
  }
  if (pass !== confirm) {
    $("r-err").textContent = "Passwords do not match. Please re-type your password.";
    $("r-err").className = "rounded-lg px-md py-sm font-body-md bg-error-container text-on-error-container text-xs";
    return;
  }

  const rules = checkPasswordRules("r-pass", "r-pass-confirm", "r");
  if (!rules.length || !rules.upper || !rules.lower || !rules.number || !rules.special) {
    $("r-err").textContent = "Password must have at least 1 capital letter, 1 small letter, 1 number, 1 special character, and be at least 10 characters long.";
    $("r-err").className = "rounded-lg px-md py-sm font-body-md bg-error-container text-on-error-container text-xs";
    return;
  }

  const btn = $("r-submit-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Updating...";
  }
  if ($("r-err")) $("r-err").className = "hidden";
  if ($("r-success")) $("r-success").className = "hidden";

  try {
    await api("POST", "/v1/auth/password-reset/confirm", {
      token,
      password: pass,
    });
    $("r-success").textContent = "Password updated successfully! Redirecting to sign in...";
    $("r-success").className = "rounded-lg px-md py-sm font-body-md bg-emerald-100 text-emerald-800 text-xs font-semibold";
    $("r-pass").value = "";
    $("r-pass-confirm").value = "";
    $("r-token").value = "";

    setTimeout(() => {
      setAuthMode("login");
      go("auth");
      toast("Password updated — please sign in with your new password");
    }, 1500);
  } catch (e) {
    $("r-err").textContent = e.message || "Failed to update password.";
    $("r-err").className = "rounded-lg px-md py-sm font-body-md bg-error-container text-on-error-container text-xs";
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Update Password";
    }
  }
}

// ── auth ──
async function doAuth(mode) {
  const email = $("a-email").value.trim();
  if (!email) {
    $("a-err").textContent = "Enter your email address.";
    $("a-err").className = "rounded-lg px-md py-sm font-body-md bg-error-container text-on-error-container text-xs";
    return;
  }
  try {
    const d = await api("POST", `/v1/auth/${mode}`, {email, password: $("a-pass").value});
    AT = d.access_token; RT = d.refresh_token; EMAIL = email;
    localStorage.setItem("at",AT); localStorage.setItem("rt",RT); localStorage.setItem("em",email);
    $("a-err").className = "hidden";
    await route();
  } catch (e) {
    $("a-err").textContent = e.message;
    $("a-err").className = "rounded-lg px-md py-sm font-body-md bg-error-container text-on-error-container text-xs";
  }
}

async function route() {
  const p = await api("GET", "/v1/profile");
  IS_MANAGED_ACCOUNT = !!p.is_managed_account; // read by go()'s own nav-button rendering
  renderNotifBadge();
  if (!p.onboarded) { await loadOnboarding(); go("onboard"); return; }
  if (p.referred) { $("refer-msg").textContent = "We can't suggest exercises based on what you selected."; go("refer"); $("nav").className = navOnClass(); return; }
  go("home");
}

// ── onboarding ──
const GOALS = [["health","Health"],["fitness","Fitness"],["flexibility","Flexibility"],["habit","Build a habit"]];
const BANDS = [["beginner","Beginner"],["1m","Getting Started"],["2m","Building Consistency"],["3m","Staying Consistent"],["experienced","Regularly Active"]];
const bandLabel = (v) => (BANDS.find(([code]) => code === v) || [null, v])[1];
let goal="health", band="beginner", picked=new Set();

function chip(label, on, fn, danger) {
  const b = document.createElement("button");
  b.textContent = label;
  b.className = "px-md py-sm rounded-full font-label-sm text-label-sm border transition-colors " + (on
    ? (danger ? "bg-error-container border-error text-on-error-container"
              : "bg-primary-fixed/20 border-primary-fixed text-on-primary-fixed-variant")
    : "bg-surface-container border-outline-variant text-on-surface-variant hover:bg-surface-variant");
  b.onclick = fn;
  return b;
}

async function loadOnboarding() {
  const g = $("o-goals"); g.innerHTML = "";
  GOALS.forEach(([v,l]) => g.appendChild(chip(l, goal===v, () => {goal=v; loadOnboarding();})));
  const bb = $("o-bands"); bb.innerHTML = "";
  BANDS.forEach(([v,l]) => bb.appendChild(chip(l, band===v, () => {band=v; loadOnboarding();})));
  const {conditions} = await api("GET", "/v1/conditions");
  const c = $("o-conds"); c.innerHTML = "";
  conditions.forEach(cd => {
    const refer = cd.bucket === "refer";
    c.appendChild(chip(cd.name + (refer ? " ⚠" : ""), picked.has(cd.code), () => {
      if (cd.code === "none") { picked.clear(); picked.add("none"); }
      else { picked.delete("none"); picked.has(cd.code) ? picked.delete(cd.code) : picked.add(cd.code); }
      loadOnboarding();
    }, refer));
  });
}

async function submitOnboarding() {
  try {
    const d = await api("POST", "/v1/profile/onboarding", {
      age: parseInt($("o-age").value,10), goal, experience_band: band, condition_codes:[...picked]});
    $("o-err").className = "hidden";
    if (d.referred) { $("refer-msg").textContent = d.message; go("refer"); $("nav").className = navOnClass(); return; }
    go("home");
  } catch (e) {
    $("o-err").textContent = e.message;
    $("o-err").className = "rounded-lg px-md py-sm font-body-md bg-error-container text-on-error-container";
  }
}

// ── stats: 4 stat cards + 12-month activity heatmap (replaces the old
// 7/15/30-day bar chart) ──
function renderStatCards(streak, yearDaily) {
  const monthStart = new Date();
  monthStart.setDate(1);
  const monthStartStr = monthStart.toISOString().slice(0, 10);
  const thisMonth = yearDaily.filter(d => d.date >= monthStartStr && d.count > 0).length;
  $("h-stat-workouts").textContent = streak.active_days;
  $("h-stat-month").textContent = thisMonth;
  $("h-stat-streak").textContent = streak.current;
  $("h-stat-best").textContent = streak.longest;
}

// One fetch (365 days) covers every period tab below — filtering client-side
// avoids a re-fetch each time the user switches tabs.
let activityYearData = [];
let activitySignupDate = null; // "YYYY-MM-DD", from /v1/dashboard/summary
let activityPeriodMonths = 3; // default tab — 1M made cells so big (to fill
// the width with only ~5 columns) that it blew up the card's height instead

const ACTIVITY_PERIODS = [[3, "3M"], [6, "6M"], [12, "12M"]];
const ACTIVITY_PERIOD_DAYS = {3: 91, 6: 182, 12: 365};

// A fixed-length window (not just "from N months ago to today"): if the
// account is younger than the period, the window starts at signup instead
// of before it (no pre-signup days rendered as if they were just inactive),
// and — since the window still keeps its full length — it then runs past
// today into the future rather than stopping short, so the grid always
// covers the full period end to end.
function activityWindowBounds(months) {
  const periodDays = ACTIVITY_PERIOD_DAYS[months];
  const today = new Date();
  const naturalStart = new Date(today);
  naturalStart.setDate(naturalStart.getDate() - periodDays + 1);
  let start = naturalStart;
  if (activitySignupDate) {
    const signup = new Date(activitySignupDate);
    if (signup > start) start = signup;
  }
  const end = new Date(start);
  end.setDate(end.getDate() + periodDays - 1);
  return {start, end, clampedToSignup: start > naturalStart};
}

function activityPeriodSlice(months) {
  const {start, end} = activityWindowBounds(months);
  const counts = new Map(activityYearData.map(d => [d.date, d.count]));
  const todayStr = new Date().toISOString().slice(0, 10);
  const days = [];
  for (const d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
    const dateStr = d.toISOString().slice(0, 10);
    // Future days have no log yet — same "empty" look as a past day with
    // no activity, rather than being left off the grid entirely.
    days.push({date: dateStr, count: dateStr > todayStr ? 0 : (counts.get(dateStr) || 0)});
  }
  return days;
}

function renderActivityPeriodTabs() {
  $("h-heatmap-period-tabs").innerHTML = ACTIVITY_PERIODS.map(([months, label]) => `
    <button onclick="selectActivityPeriod(${months})"
      class="px-2 py-0.5 rounded-full text-[10px] font-medium transition-all ${
        months === activityPeriodMonths
          ? "font-bold bg-primary-fixed text-on-primary-fixed shadow-sm"
          : "text-secondary hover:text-on-background"
      }">${label}</button>`).join("");
  const [, activeLabel] = ACTIVITY_PERIODS.find(([m]) => m === activityPeriodMonths);
  const {clampedToSignup} = activityWindowBounds(activityPeriodMonths);
  const span = clampedToSignup ? "since you joined" : `last ${activeLabel.replace("M", "")} months`;
  $("h-heatmap-period-label").textContent = `Activity frequency — ${span}`;
}

function selectActivityPeriod(months) {
  activityPeriodMonths = months;
  renderActivityPeriodTabs();
  renderActivityHeatmap(activityPeriodSlice(months));
}

// "You're most active on Thursdays" — computed client-side from the same
// 365-day trend already fetched for the heatmap, no extra request. Needs a
// real sample to mean anything — a brand-new account with only a couple of
// logged days would otherwise get a misleadingly confident-sounding insight
// from almost no data, so it stays hidden until there's enough history.
const MIN_ACTIVE_DAYS_FOR_INSIGHT = 10;
const WEEKDAY_NAMES = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"];

function mostActiveWeekdayInsight(daily) {
  const activeDays = daily.filter(d => d.count > 0);
  if (activeDays.length < MIN_ACTIVE_DAYS_FOR_INSIGHT) return null;
  const totals = [0, 0, 0, 0, 0, 0, 0];
  for (const d of activeDays) totals[new Date(d.date).getDay()] += d.count;
  const max = Math.max(...totals);
  const topDays = totals.map((t, i) => [t, i]).filter(([t]) => t === max).map(([, i]) => WEEKDAY_NAMES[i]);
  const joined = topDays.length > 1
    ? topDays.slice(0, -1).join(", ") + " & " + topDays[topDays.length - 1]
    : topDays[0];
  return `You're most active on ${joined}`;
}

// GitHub-contribution-style grid: one column per week (Mon-first), one row
// per weekday. daily is already in ascending date order (oldest first),
// one entry per day, sliced from the 365-day /v1/dashboard/trend fetch.
const HEATMAP_GAP = 3;
const HEATMAP_MIN_CELL = 10;
// Deliberately generous, not a "nice" cap — the point is to fill the card
// edge to edge for short periods (1M: ~5 columns, 3M: ~13), not to leave a
// gap on the right because the cells hit some arbitrary size limit first.
const HEATMAP_MAX_CELL = 72;

function renderActivityHeatmap(daily) {
  if (!daily.length) { $("h-heatmap-grid").innerHTML = ""; $("h-heatmap-months").innerHTML = ""; return; }
  const firstDow = (new Date(daily[0].date).getDay() + 6) % 7; // Sun=0..Sat=6 -> Mon=0..Sun=6
  const cells = Array(firstDow).fill(null).concat(daily);
  while (cells.length % 7 !== 0) cells.push(null);
  const weeks = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));

  // Fill the available width instead of always drawing fixed 10px cells —
  // a 1-month view (~5 columns) would otherwise leave most of the card
  // blank. A 12-month view (~53 columns) still hits the 10px floor and
  // scrolls horizontally (h-heatmap-scroll), same as before.
  const availableWidth = $("h-heatmap-scroll").clientWidth || 260;
  const rawCell = (availableWidth - (weeks.length - 1) * HEATMAP_GAP) / weeks.length;
  const cell = Math.max(HEATMAP_MIN_CELL, Math.min(HEATMAP_MAX_CELL, Math.floor(rawCell)));
  const colHeight = 7 * cell + 6 * HEATMAP_GAP;
  $("h-heatmap-weekday-rows").style.height = colHeight + "px";

  const max = Math.max(1, ...daily.map(d => d.count));
  function levelClass(count) {
    if (count == null) return "opacity-0";
    if (count === 0) return "bg-surface-variant/30";
    const ratio = count / max;
    if (ratio > 0.75) return "bg-[#1E6B24]";
    if (ratio > 0.5) return "bg-[#2E8B3B]";
    if (ratio > 0.25) return "bg-[#419E4B]/70";
    return "bg-[#419E4B]/35";
  }

  $("h-heatmap-grid").innerHTML = weeks.map(week => `
    <div class="flex flex-col gap-[3px] shrink-0" style="width:${cell}px">
      ${week.map(day => `<div class="rounded-[2px] ${levelClass(day ? day.count : null)}" style="width:${cell}px;height:${cell}px"${day ? ` title="${day.date}: ${day.count} logged"` : ""}></div>`).join("")}
    </div>`).join("");

  let lastMonth = null;
  $("h-heatmap-months").innerHTML = weeks.map(week => {
    const firstReal = week.find(d => d);
    if (!firstReal) return `<div class="shrink-0" style="width:${cell}px"></div>`;
    const m = new Date(firstReal.date).toLocaleDateString([], {month: "short"});
    const label = m !== lastMonth ? m : "";
    lastMonth = m;
    return `<div class="shrink-0" style="width:${cell}px">${label}</div>`;
  }).join("");
}

// ── custom exercises (4.4) — personal, fixed-target exercises, never suggested ──
let customExercises = [];
let customMeasurementType = "reps";
const CUSTOM_TYPES = [["reps","Reps"],["distance","Distance"],["duration","Time"]];
const CUSTOM_TYPE_UNIT = { reps: "count", distance: "km", duration: "min" };

// Shared by both custom-exercise forms (the self-service one here, and the
// managed-plan one in main.js's managed-add section) — distance/duration are
// a single effort (no sets), same convention as a built-in activity.
function updateCustomTargetFields(type, setsWrapId, amountLabelId) {
  const single = type === "distance" || type === "duration";
  $(setsWrapId).classList.toggle("hidden", single);
  $(amountLabelId).textContent = type === "reps" ? "Reps" : `${CUSTOM_TYPE_UNIT[type] === "km" ? "Distance" : "Duration"} (${CUSTOM_TYPE_UNIT[type]})`;
}

async function loadCustomExerciseSection() {
  $("custom-name").value = "";
  $("custom-err").className = "hidden";
  // One screen: the name, how it is counted, and the target — nothing is hidden
  // behind a Continue step any more (the options used to look like they didn't exist).
  customMeasurementType = "reps";
  $("custom-target-sets").value = "2";
  $("custom-target-amount").value = "";
  renderCustomTypeChips();
  updateCustomTargetFields("reps", "custom-target-sets-wrap", "custom-target-amount-label");
  $("custom-continue-btn").textContent = "Save";
  const d = await api("GET", "/v1/custom-exercises");
  customExercises = d.custom_exercises;
  renderCustomList();
}

function renderCustomList() {
  const box = $("custom-list");
  box.innerHTML = customExercises.length ? customExercises.map(c => `
    <div class="flex items-center justify-between bg-surface-container rounded-DEFAULT px-md py-sm">
      <span class="font-label-bold text-label-bold text-on-surface flex-1">${escapeHtml(c.name)}</span>
      <button onclick="renameCustomPrompt('${c.id}')" aria-label="Rename" class="text-on-surface-variant px-xs"><span class="material-symbols-outlined text-[18px]">edit</span></button>
      <button onclick="deleteCustomExercise('${c.id}')" aria-label="Delete" class="text-on-surface-variant px-xs"><span class="material-symbols-outlined text-[18px]">delete</span></button>
    </div>`).join("")
    : `<p class="font-body-md text-on-surface-variant">None yet.</p>`;
}

function renderCustomTypeChips() {
  const box = $("custom-type-chips");
  box.innerHTML = "";
  CUSTOM_TYPES.forEach(([v, l]) => box.appendChild(chip(l, customMeasurementType === v, () => {
    customMeasurementType = v;
    renderCustomTypeChips();
    updateCustomTargetFields(v, "custom-target-sets-wrap", "custom-target-amount-label");
  })));
}

// Add a New One only ever CREATES the exercise (mirrors exactly how adding a
// built-in exercise via addFromCatalogue just adds it, never logs it) — with
// a fixed target set up front, the same shape as a built-in activity's own
// default_sets/default_amount. Logging happens separately, whenever the user
// actually does it, via its card in the "Your Own Exercises" section on Home.
function customContinue() {
  const name = $("custom-name").value.trim();
  if (!name) { toast("Type a name first", true); return; }
  const match = customExercises.find(c => c.name.toLowerCase() === name.toLowerCase());
  if (match) { toast(`You already have "${match.name}" — log it from the Home screen`, true); return; }
  const amount = parseFloat($("custom-target-amount").value);
  if (!amount || amount <= 0) { toast("Enter a target greater than zero.", true); return; }
  // Distance and time are a single effort: their Sets box is hidden, so its stale
  // value (default 2) must never be sent.
  const single = customMeasurementType === "distance" || customMeasurementType === "duration";
  const sets = single ? 1 : (parseInt($("custom-target-sets").value, 10) || 1);
  submitCustomCreate(amount, sets);
}

async function submitCustomCreate(amount, sets) {
  try {
    await api("POST", "/v1/custom-exercises", {
      name: $("custom-name").value.trim(), measurement_type: customMeasurementType,
      target_amount: amount, target_sets: sets,
    });
    closeCatalogue();
    toast("Added — log it anytime from the Dashboard");
    loadHome();
  } catch (e) {
    $("custom-err").textContent = e.message;
    $("custom-err").className = "font-body-md text-on-error-container bg-error-container rounded px-sm py-xs";
  }
}

async function renameCustomPrompt(id) {
  // Looked up from the in-memory list rather than passed through the
  // onclick attribute's string — a freeform name embedded there is a stored
  // XSS risk even HTML-escaped, since the browser decodes entities before
  // treating the attribute as JS.
  const currentName = (customExercises.find(c => c.id === id) || {}).name || "";
  const name = window.prompt("Rename to:", currentName);
  if (!name || !name.trim() || name.trim() === currentName) return;
  try {
    await api("PUT", `/v1/custom-exercises/${id}`, {name: name.trim()});
    const d = await api("GET", "/v1/custom-exercises");
    customExercises = d.custom_exercises;
    renderCustomList();
    loadHome(); // keep the Dashboard's cards (renderCustomCard) in sync too
    toast("Renamed");
  } catch (e) { toast(e.message, true); }
}

async function deleteCustomExercise(id) {
  if (!(await appConfirm({ message: "Delete this exercise?", confirmText: "Delete", danger: true }))) return;
  try {
    await api("DELETE", `/v1/custom-exercises/${id}`);
    const d = await api("GET", "/v1/custom-exercises");
    customExercises = d.custom_exercises;
    renderCustomList();
    loadHome(); // keep the Dashboard's cards (renderCustomCard) in sync too
    toast("Deleted");
  } catch (e) { toast(e.message, true); }
}

function getActivityBadge(name) {
  const n = (name || "").toLowerCase();
  if (n.includes("walk")) {
    return { icon: "directions_walk", bg: "bg-emerald-500/15 text-emerald-700 border-emerald-500/30" };
  }
  if (n.includes("run")) {
    return { icon: "directions_run", bg: "bg-amber-500/15 text-amber-700 border-amber-500/30" };
  }
  if (n.includes("cycl") || n.includes("bike")) {
    return { icon: "directions_bike", bg: "bg-sky-500/15 text-sky-700 border-sky-500/30" };
  }
  if (n.includes("swim")) {
    return { icon: "pool", bg: "bg-cyan-500/15 text-cyan-700 border-cyan-500/30" };
  }
  if (n.includes("stretch") || n.includes("yoga")) {
    return { icon: "self_improvement", bg: "bg-purple-500/15 text-purple-700 border-purple-500/30" };
  }
  return { icon: "fitness_center", bg: "bg-primary/10 text-primary border-primary/20" };
}

function renderRecentActivities(entries) {
  const container = $("h-recent-activities-list");
  if (!container) return;
  if (!entries || !entries.length) {
    container.innerHTML = `
      <div class="bg-white/70 backdrop-blur-md border border-dashed border-outline-variant/60 rounded-2xl p-md text-center space-y-1">
        <p class="font-label-bold text-label-bold text-on-surface">No activities logged in the last 7 days</p>
        <p class="text-xs text-secondary">Track an outdoor walk/run with the GPS button below or tap the dumbbell to start a workout.</p>
      </div>`;
    return;
  }

  const recent = entries.slice(0, 5);
  const todayStr = new Date().toISOString().slice(0, 10);
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  const yesterdayStr = yesterday.toISOString().slice(0, 10);

  container.innerHTML = recent.map(e => {
    const badge = getActivityBadge(e.name);
    let dateLabel = e.date;
    if (e.date === todayStr) dateLabel = "Today";
    else if (e.date === yesterdayStr) dateLabel = "Yesterday";

    // "count" is reps-type's internal unit name, never meant for display —
    // same convention as openCatalogueAddEditor's own amount_unit handling.
    const unit = escapeHtml(e.unit === "count" ? "reps" : (e.unit || ""));
    // Real per-set numbers when they differ (e.g. 11, 13 instead of the
    // blended "2 x 12" average that was never the true figure for either set).
    const valDisplay = e.set_amounts
      ? `${e.set_amounts.join(", ")} ${unit}`
      : e.sets && e.sets > 1
      ? `${e.sets} × ${e.amount} ${unit}`
      : `${e.amount} ${unit}`;

    return `
      <div class="flex items-center justify-between bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-md hover:bg-white/95 transition-all">
        <div class="flex items-center gap-md">
          <div class="w-10 h-10 rounded-xl ${badge.bg} border flex items-center justify-center flex-shrink-0 shadow-xs">
            <span class="material-symbols-outlined text-[20px]">${badge.icon}</span>
          </div>
          <div>
            <p class="font-label-bold text-label-bold text-on-background">${escapeHtml(e.name)}</p>
            <p class="font-label-sm text-label-sm text-secondary">${dateLabel}</p>
          </div>
        </div>
        <div class="text-right">
          <p class="font-headline-sm text-headline-sm font-bold text-on-background">${valDisplay}</p>
        </div>
      </div>`;
  }).join("");
}

// ── dashboard screen (stats + Your Own Exercises; the program's exercise
// list itself — formerly "Recommended Today" — now lives entirely inside
// the guided Workout Player, since it showed exactly the same exercises) ──
async function loadHome() {
  $("h-avatar").textContent = (EMAIL[0] || "?").toUpperCase();
  $("h-hello").textContent = "Welcome back, " + EMAIL.split("@")[0] + "!";

  // Fired together, not sequentially: the summary's streak/suggestions,
  // 365-day trend, and recent activity logs for the dashboard feed.
  const [d, yearTrend, recentActivitiesRes] = await Promise.all([
    api("GET", "/v1/dashboard/summary"),
    api("GET", "/v1/dashboard/trend?days=365"),
    api("GET", "/v1/dashboard/activity-detail?days=7").catch(() => ({ entries: [] })),
  ]);
  HOME_ITEMS = d.suggestions.all_items || [];
  renderStatCards(d.streak, yearTrend.trend);
  activityYearData = yearTrend.trend;
  activitySignupDate = d.signup_date;
  renderActivityPeriodTabs();
  renderActivityHeatmap(activityPeriodSlice(activityPeriodMonths));
  const insight = mostActiveWeekdayInsight(activityYearData);
  $("h-heatmap-insight").textContent = insight || "";
  $("h-heatmap-insight").classList.toggle("hidden", !insight);
  renderRecentActivities(recentActivitiesRes.entries || []);

  // your own exercises — tracking-only, never suggested/programmed (4.4):
  // creating/logging here only ever calls custom-exercise endpoints, never
  // /v1/program/entries.
  const { custom_exercises } = await api("GET", "/v1/custom-exercises");
  HOME_CUSTOM_ITEMS = custom_exercises;
  $("h-custom-section").classList.toggle("hidden", !custom_exercises.length);
  $("h-custom-cards").innerHTML = custom_exercises.map(renderCustomCard).join("");

  const [waterData, sorenessData] = await Promise.all([
    api("GET", "/v1/water-intake/today"),
    api("GET", "/v1/soreness/status"),
  ]);
  $("h-water-count").textContent = waterData.glasses_count;
  const sorenessItems = sorenessData.items || [];
  if (sorenessItems.length === 0) {
    $("h-soreness-section").classList.add("hidden");
  } else {
    $("h-soreness-section").classList.remove("hidden");
    $("h-soreness-list").innerHTML = sorenessItems.map(item => `
      <div class="flex items-center justify-between bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-md">
        <p class="font-body-lg text-body-lg text-on-background">${escapeHtml(item.display_name)}</p>
        <button onclick="clearSoreness('${item.activity_type}')" class="font-label-bold text-label-bold text-primary">No longer sore</button>
      </div>
    `).join("");
  }

  const [{ activities: pbActivities }, { activities: taxActivities }] = await Promise.all([
    api("GET", "/v1/personal-bests"),
    api("GET", "/v1/taxonomy/activities"),
  ]);
  const pbMap = Object.assign({}, pbActivities || {});
  const taxLookup = {};
  const taxUnitLookup = {};
  (taxActivities || []).forEach(a => {
    taxLookup[a.activity_type] = a.display_name;
    taxUnitLookup[a.activity_type] = a.amount_unit;
  });

  // A PR is what the user actually achieved, one per exercise they have logged (the server derives it).
  // Target peaks from progression used to be merged in here too, which showed a PR for exercises
  // that were never done, so they were removed.

  const pbKeys = Object.keys(pbMap).filter(k => Array.isArray(pbMap[k]) && pbMap[k].length > 0);
  $("h-personal-bests-section").classList.toggle("hidden", !pbKeys.length);
  $("h-personal-bests-cards").innerHTML = pbKeys.map(k => {
    const best = pbMap[k][0];
    const name = best.display_name || taxLookup[k] || k;
    const dateStr = best.achieved_at ? new Date(best.achieved_at).toLocaleDateString() : "";
    const pbUnit = escapeHtml(best.unit === "count" ? "reps" : (best.unit || ""));
    const valDisplay = best.sets && best.sets > 1
      ? `${best.sets} x ${best.value} ${pbUnit}`
      : `${best.value} ${pbUnit}`;
    return `
      <div class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-md flex flex-col gap-xs">
        <div class="flex items-center justify-between">
          <p class="font-label-bold text-label-bold text-on-surface truncate">${escapeHtml(name)}</p>
          <span class="text-[10px] font-bold uppercase tracking-wider text-amber-600 bg-amber-500/10 px-1.5 py-0.5 rounded-md flex items-center gap-0.5">
            <span class="material-symbols-outlined text-[12px]">emoji_events</span> PR
          </span>
        </div>
        <p class="font-headline-sm text-headline-sm text-on-background font-bold">${valDisplay}</p>
        <p class="font-label-sm text-label-sm text-secondary">${dateStr}</p>
      </div>`;
  }).join("");


  const PROGRESSION_REASONS = {
    curve: "Gradual increase",
    hold: "Held steady",
    reduce: "Reduced",
    floor: "Kept at the minimum",
    escalation: "You confirmed a bigger increase",
    double_progression: "Extra increase for consistent performance",
    set_created: "Started tracking",
    set_boost: "Increased after a strong set",
    pro_level: "Adjusted for your experience level",
  };

  CACHED_PROGRESSION = d.progression || [];

  // Sync the voice-pipeline toggle to the stored per-device preference.
  const elevenlabsToggle = $("elevenlabs-toggle");
  if (elevenlabsToggle) elevenlabsToggle.checked = isElevenLabsEnabled();
}

// Re-fetches the program's item list and, if a workout is currently in
// progress, appends any newly-added exercise(s) to PLAYER_ITEMS so they
// show up immediately in the current session instead of requiring a
// restart. If no session is running (e.g. the program was empty and
// startWorkout() opened the catalogue directly), starts one now that
// there's something to do.
async function refreshPlayerItems() {
  const d = await api("GET", "/v1/dashboard/summary");
  HOME_ITEMS = d.suggestions.all_items || [];
  if (playerSessionId) {
    const existingTypes = new Set(PLAYER_ITEMS.map(i => i.activity_type));
    const added = HOME_ITEMS.filter(i => !existingTypes.has(i.activity_type));
    if (added.length) {
      const currentItem = PLAYER_ITEMS[playerIndex];
      PLAYER_ITEMS = PLAYER_ITEMS.concat(added).sort((a, b) => (a.priority_order ?? 99) - (b.priority_order ?? 99));
      playerIndex = Math.max(0, PLAYER_ITEMS.indexOf(currentItem));
      renderPlayerExercise();
    }
  } else {
    startWorkout();
  }
}

const CUSTOM_TYPE_LABEL = { reps: "Reps", distance: "Distance", duration: "Time" };

function renderCustomCard(c) {
  // One measurement type with a fixed target, set at creation — pre-filled
  // here exactly like a built-in activity's own default_sets/default_amount;
  // still editable before logging, same as before.
  const single = c.measurement_type === "distance" || c.measurement_type === "duration";
  const amountLabel = c.measurement_type === "reps" ? "Reps" : `${CUSTOM_TYPE_LABEL[c.measurement_type]} (${c.unit})`;
  const block = `
    <div class="grid grid-cols-2 gap-sm items-stretch">
      <div class="space-y-xs flex flex-col min-w-0">
        <label class="text-label-sm text-secondary truncate block h-4 mb-1">Sets</label>
        ${single
          ? `<div class="bg-surface-container-low rounded-DEFAULT h-10 flex items-center justify-center"><span class="font-body-md text-on-surface-variant text-xs">—</span></div>`
          : `<input id="cc-sets-${c.id}" type="number" min="1" value="${c.target_sets}"
               class="bg-surface-container-low border-none rounded-DEFAULT h-10 text-center font-body-md text-on-surface w-full px-xs">`}
      </div>
      <div class="space-y-xs flex flex-col min-w-0">
        <label class="text-label-sm text-secondary truncate block h-4 mb-1">${amountLabel}</label>
        <input id="cc-amount-${c.id}" type="number" step="any" value="${c.target_amount}"
          class="bg-surface-container-low border-none rounded-DEFAULT h-10 text-center font-body-md text-on-surface w-full px-xs">
      </div>
    </div>`;

  return `
  <div class="bg-surface-container-lowest ambient-shadow rounded-lg p-md space-y-md flex flex-col">
    <div class="space-y-xs">
      <div class="flex items-center justify-between h-4 mb-1">
        <label class="text-label-sm text-secondary">Activity Type</label>
        <button onclick="deleteCustomExercise('${c.id}')" aria-label="Remove"
          class="text-secondary hover:text-error">
          <span class="material-symbols-outlined text-[16px]">close</span>
        </button>
      </div>
      <div class="bg-surface-container-low rounded-DEFAULT flex items-center justify-center gap-xs h-10 px-md">
        <span class="material-symbols-outlined text-secondary text-[18px]">fitness_center</span>
        <span class="font-body-md text-on-surface text-sm truncate">${escapeHtml(c.name)}</span>
      </div>
    </div>
    ${block}
    <div class="flex-1"></div>
    <button onclick="logCustomCard('${c.id}')"
      class="w-full ${c.completed_today ? 'bg-surface-container-low text-on-surface-variant' : 'bg-primary text-on-primary'} py-md rounded-full font-label-bold text-label-bold hover:opacity-90">
      ${c.completed_today ? 'Logged today' : 'Save Activity'}
    </button>
  </div>`;
}

async function logCustomCard(id) {
  const c = HOME_CUSTOM_ITEMS.find(x => x.id === id);
  if (!c) return;
  const single = c.measurement_type === "distance" || c.measurement_type === "duration";
  const amount = parseFloat($(`cc-amount-${id}`).value);
  const sets = single ? 1 : (parseInt($(`cc-sets-${id}`).value, 10) || 1);
  if (!amount || amount <= 0) {
    toast(`Enter a ${CUSTOM_TYPE_LABEL[c.measurement_type].toLowerCase()} value`, true);
    return;
  }
  const entries = [{custom_exercise_id: id, amount, sets, unit: c.unit}];
  try {
    const d = await api("POST", "/v1/activities", {entries});
    const anyImplausible = d.logged.some(l => l.implausible);
    toast(anyImplausible
      ? "Saved — some of that looks unusual, so it won't count towards progress"
      : `Saved · ${d.streak.current} day streak`, anyImplausible);
    loadHome();
  } catch (e) { toast(e.message, true); }
}

async function send(type, amount, sets, workoutSessionId, setAmounts, replaceToday) {
  try {
    // set_amounts (per-set breakdown) replaces amount/sets entirely when
    // given — the backend derives sets_done/amount_per_set from the array
    // itself (see app.profile.service.log_activity), so sending both would
    // be redundant/conflicting.
    const body = setAmounts ? {activity_type: type, set_amounts: setAmounts}
                            : {activity_type: type, amount, sets};
    if (workoutSessionId) body.workout_session_id = workoutSessionId;
    // Workout Player only: it keeps ONE entry per exercise per day, so a
    // save replaces today's earlier one instead of adding on top of it.
    if (replaceToday) body.replace_today = true;
    const d = await api("POST", "/v1/activities", body);
    const l = d.logged[0];
    toast(l.implausible
      ? "Saved — that totals more than expected, so it won't count towards progress"
      : `Saved ${l.prescription} · ${d.streak.current} day streak`, l.implausible);
    // A workout-session log refreshes the dashboard itself when the player
    // returns home (finishPlayerSession/closePlayer) — doing it here too
    // would just be a wasted fetch while still mid-session.
    if (!workoutSessionId) loadHome();
    return l;
  } catch (e) { toast(e.message, true); return null; }
}

let voiceTranscript = "";
let voiceEntries = [];

let whisperWorker = null;

let isWhisperRecording = false;

// We force CPU WebAssembly because it works on 100% of devices!
let useWebGPU = false;

let voiceStopTime = 0;

// Standard MediaRecorder variables
let mediaRecorder = null;
let audioChunks = [];

// ── Voice pipeline choice: ElevenLabs (premium, costs API credits) vs. a
// completely free local pipeline (Whisper in a Web Worker for STT, the
// browser's own speech synthesis for TTS). A per-device preference, stored
// in localStorage — not account data, so it isn't sent to the backend.
function isElevenLabsEnabled() {
  return localStorage.getItem("useElevenLabs") !== "false"; // defaults ON
}

function setElevenLabsEnabled(enabled) {
  localStorage.setItem("useElevenLabs", enabled ? "true" : "false");
  toast(enabled ? "Premium voice (ElevenLabs) enabled" : "Using free local voice pipeline");
}

// One-shot request/response wrapper around whisperWorker.js — replaces an
// earlier streaming-chunk design that's no longer used anywhere (every
// recording is now a single manually-started, manually-stopped clip, not a
// continuous stream of chunks). The worker is created and loaded lazily, once,
// on first use, and reused for every later transcription.
let whisperReadyPromise = null;

function ensureWhisperWorker() {
  if (!whisperWorker) {
    whisperWorker = new Worker(new URL('./whisperWorker.js', import.meta.url), { type: 'module' });
  }
  if (!whisperReadyPromise) {
    toast("Loading free voice model (one-time download)...");
    whisperReadyPromise = new Promise((resolve, reject) => {
      const onMessage = (event) => {
        const data = event.data;
        if (data.status === 'ready') {
          whisperWorker.removeEventListener('message', onMessage);
          resolve();
        } else if (data.status === 'error') {
          whisperWorker.removeEventListener('message', onMessage);
          reject(new Error(data.error));
        } else if (data.status === 'progress' && data.data && data.data.progress) {
          toast(`Downloading voice model... ${Math.round(data.data.progress)}%`);
        }
      };
      whisperWorker.addEventListener('message', onMessage);
      whisperWorker.postMessage({ type: 'init', useWebGPU });
    });
  }
  return whisperReadyPromise;
}

async function transcribeWithWhisper(float32Array) {
  await ensureWhisperWorker();
  return new Promise((resolve, reject) => {
    const onMessage = (event) => {
      const data = event.data;
      if (data.status === 'done') {
        whisperWorker.removeEventListener('message', onMessage);
        resolve(data.text);
      } else if (data.status === 'error') {
        whisperWorker.removeEventListener('message', onMessage);
        reject(new Error(data.error));
      }
      // 'inferring' status is ignored here — nothing to show mid-transcription
      // for a single short clip the way there was for a continuous stream.
    };
    whisperWorker.addEventListener('message', onMessage);
    whisperWorker.postMessage({ type: 'transcribe', audioData: float32Array });
  });
}

// Browser-native speech synthesis — the free counterpart to elevenlabsTTS.
// Prefers a natural-sounding Google voice where available (Chrome/Android),
// falls back to any English voice, falls back to the browser's own default.
function speakWithBrowserTTS(text) {
  return new Promise((resolve) => {
    if (!window.speechSynthesis) { resolve(); return; } // unsupported — never block the flow on this
    window.speechSynthesis.cancel(); // stop any previous utterance first
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.0;
    utterance.pitch = 1.0;
    const voices = window.speechSynthesis.getVoices();
    utterance.voice = voices.find(v => v.name.includes("Google") && v.lang.startsWith("en"))
      || voices.find(v => v.lang.startsWith("en"))
      || null;
    utterance.onend = () => resolve();
    utterance.onerror = () => resolve(); // a TTS glitch should never strand the user mid-flow
    window.speechSynthesis.speak(utterance);
  });
}

// Unified entry points — every call site in this file goes through these two
// instead of calling elevenlabsTTS/elevenlabsSTT or the whisper/browser
// functions directly, so the pipeline choice lives in exactly one place.
// Both fall through to the free local pipeline if ElevenLabs is enabled but
// actually fails (network issue, exhausted quota, bad key) — the user should
// never be stuck with no voice response just because a paid API hiccuped.
async function speakVitaText(text) {
  if (isElevenLabsEnabled()) {
    try { await elevenlabsTTS(text, authedFetch); return; }
    catch (e) { console.error("ElevenLabs TTS failed, falling back to free voice:", e); }
  }
  await speakWithBrowserTTS(text);
}

// The server upload is the original compressed recording (a few KB/s), not the
// decoded PCM — 16-bit PCM is ~32 KB/s and would hit the API's 1 MiB request
// cap after ~30 s of speech. A clip too big even compressed, or one with no
// original blob, simply uses the free Whisper path below, which needs only the
// decoded samples.
const MAX_PREMIUM_UPLOAD_BYTES = 900 * 1024;

async function transcribeAudio(float32Array, sampleRate, recordedBlob) {
  if (isElevenLabsEnabled() && recordedBlob && recordedBlob.size <= MAX_PREMIUM_UPLOAD_BYTES) {
    try { return await elevenlabsSTT(recordedBlob, authedFetch); }
    catch (e) {
      console.error("ElevenLabs STT failed, falling back to free transcription:", e);
      toast("Premium voice unavailable — using free transcription instead", true);
    }
  }
  return await transcribeWithWhisper(float32Array);
}

// Set while toggleWhisperRecording is acquiring the mic and speaking Vita's
// greeting — before isWhisperRecording flips true. Without it, a second tap in
// that window passed the "already recording?" check and started a second,
// overlapping MediaRecorder.
let isStartingRecording = false;

// Identifies the current voice attempt. Starting one takes a new number; the
// cross button (cancelVoiceLog) bumps it. Every async step below compares the
// number it started with, so a cancelled attempt is never transcribed, sent for
// extraction, rendered or spoken — whichever stage it was in when cancelled.
let voiceRunId = 0;

// Puts the mic button back to its idle look. The processing path leaves it on
// the hourglass, and on a successful extraction nothing ever restored it.
function resetMicFab() {
    const icon = $("main-mic-icon");
    const fab = $("main-mic-fab");
    if (icon) icon.textContent = "mic";
    if (fab) {
        fab.classList.remove("bg-error", "bg-surface-variant");
        fab.classList.add("bg-primary-fixed");
    }
}

// Safari/iOS records MP4/AAC, not WebM, so the recorder's real type is used
// instead of a hardcoded "audio/webm". undefined lets the browser choose.
function pickRecorderMimeType() {
    if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) return undefined;
    return ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]
        .find(t => MediaRecorder.isTypeSupported(t));
}

// Decodes a recorded clip to the 16 kHz mono Float32Array both transcribers
// need. Some browsers reject a custom AudioContext sample rate or ignore it
// when decoding, so fall back to a default context and resample explicitly.
async function decodeRecordingTo16k(blob) {
    const arrayBuffer = await blob.arrayBuffer();
    const Ctx = window.AudioContext || window.webkitAudioContext;
    let ctx;
    try { ctx = new Ctx({ sampleRate: 16000 }); }
    catch (_) { ctx = new Ctx(); }
    try {
        const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
        if (audioBuffer.sampleRate === 16000) return audioBuffer.getChannelData(0);
        const offline = new OfflineAudioContext(1, Math.max(1, Math.ceil(audioBuffer.duration * 16000)), 16000);
        const src = offline.createBufferSource();
        src.buffer = audioBuffer;
        src.connect(offline.destination);
        src.start();
        const rendered = await offline.startRendering();
        return rendered.getChannelData(0);
    } finally {
        if (ctx.close) ctx.close().catch(() => {});
    }
}

function stopWhisperRecording() {
    isWhisperRecording = false;
    if (mediaRecorder && mediaRecorder.state !== "inactive") {
        mediaRecorder.stop();
    }
    $("main-mic-icon").textContent = "mic";
    $("main-mic-fab").classList.remove("bg-error");
    $("main-mic-fab").classList.add("bg-primary-fixed");
}

function finalizeVoiceSubmit(runId = voiceRunId) {
    // Whisper returns punctuation-only text like "." for silence. Sending that
    // would spend one of the user's daily voice attempts (the server counts the
    // attempt before it runs the model) for a guaranteed empty result.
    if (/[\p{L}\p{N}]/u.test(voiceTranscript || "")) {
        toast("Processing final transcript...");
        voiceSubmitTranscript(voiceTranscript, runId);
    } else {
        // Used to only toast, leaving the modal on "Processing..." and the mic
        // button on the hourglass with no way forward.
        showVoiceFallback("No speech detected. Please tap Try Again and speak after the greeting.");
    }
}

// ── Phase 0: Vita greeting bank ─────────────────────────────────────────────
// Spoken aloud when the user taps the mic button, before recording begins.
// Randomly selected each time — no API call needed.
const VITA_GREETINGS = [
  "Vita online. Welcome back, sir. What did you conquer today?",
  "Ah, you're back. I was beginning to wonder. Tell me everything.",
  "There you are. I've been patient. What did you do today?",
  "Good of you to return. Now — what did we accomplish?",
  "Vita at your service. The floor is yours.",
  "Vita here. Whenever you're ready, I'm listening.",
  "Ah, excellent timing. I'm listening. What happened today?",
  "Vita reporting for duty. What did you do today?",
  "Back again. Good. What are we logging today?",
  "Good to have you back. I'm all ears — take your time.",
  "Vita here. You have my full attention — what did you do today?",
  "Welcome back. I've kept the lights on for you. Go ahead.",
  "There you are. No rush — just tell me what happened.",
  "Vita online. I've cleared my schedule. What did you do today?",
  "Vita activated. I'm ready when you are. What's the report?",
  "Glad you're here. Something tells me today was a good one — am I right?",
  "Vita on standby. I'm ready — go ahead.",
  "Good to see you again. Tell me — what did you do today?",
  "Welcome back. The session is yours. What did we conquer?",
  "Vita here. I've been waiting. Go ahead — tell me everything.",
];

async function vitaGreet() {
  const text = VITA_GREETINGS[Math.floor(Math.random() * VITA_GREETINGS.length)];
  const avatar = $("avatar-container");
  if (avatar) {
      avatar.style.transform = "scale(1.05)";
      avatar.classList.add("animate-talk");
  }
  try {
    await speakVitaText(text);
  } catch (e) {
    console.error("Vita greeting TTS failed", e);
  } finally {
      if (avatar) {
          avatar.style.transform = "scale(1)";
          avatar.classList.remove("animate-talk");
      }
  }
}

// Manual start/stop only — no auto-stop-on-silence. Found live, across
// several rounds of trying to tune a volume-based silence timer: it either
// cut users off mid-sentence or, on some mics/rooms, never detected silence
// at all and just ran the full fixed duration regardless of whether the
// user was actually talking. A real VAD needs more than a raw volume
// threshold to be reliable (proper speech-vs-noise classification, not just
// "louder than X") — not worth building for this feature. Manual control
// (click the mic again, or the dedicated Done button) is simple and never
// gets this wrong.
async function toggleWhisperRecording() {
    if (isWhisperRecording) {
        voiceStopTime = Date.now(); // UX-latency timer shown on the confirmation screen
        stopWhisperRecording(); // triggers the async mediaRecorder.onstop handler below

        $("voice-modal-title").textContent = "Processing...";
        toast("Transcribing...");
        $("main-mic-icon").textContent = "hourglass_empty";
        $("main-mic-fab").classList.remove("bg-error");
        $("main-mic-fab").classList.add("bg-surface-variant");
        $("voice-done-btn")?.classList.add("hidden");
        return;
    }

    // Start recording
    if (isStartingRecording) return; // mic is still opening / greeting is playing
    isStartingRecording = true;
    const myRun = ++voiceRunId;
    let stream = null;
    try {
        voiceTranscript = "";
        audioChunks = [];

        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const recordingStream = stream;
        const mimeType = pickRecorderMimeType();
        mediaRecorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);

        mediaRecorder.ondataavailable = (e) => {
            if (e.data.size > 0) audioChunks.push(e.data);
        };

        mediaRecorder.onstop = async () => {
            // Everything from decoding onward is inside the try. Decoding used
            // to sit outside it, so a decode failure (empty clip, a format the
            // browser can't decode) threw into nowhere and left the UI stuck
            // on "Processing..." forever.
            try {
                if (myRun !== voiceRunId) return;   // cancelled while recording: discard the audio
                const blob = new Blob(audioChunks, { type: (mediaRecorder && mediaRecorder.mimeType) || mimeType || 'audio/webm' });
                if (blob.size === 0) throw new Error("No audio was captured.");
                const float32Array = await decodeRecordingTo16k(blob);
                if (myRun !== voiceRunId) return;   // cancelled while decoding
                const transcript = await transcribeAudio(float32Array, 16000, blob);
                if (myRun !== voiceRunId) return;   // cancelled while transcribing: drop the result
                voiceTranscript = transcript;
                finalizeVoiceSubmit(myRun);
            } catch (e) {
                if (myRun !== voiceRunId) return;   // an error from a cancelled attempt is not shown
                console.error("Voice processing error:", e);
                showVoiceFallback("Couldn't process that recording (" + (e.message || "unknown error") + "). Please tap Try Again.");
            } finally {
                // Always release the mic, including on failure.
                recordingStream.getTracks().forEach(t => t.stop());
            }
        };

        // Phase 0: Speak Vita greeting, THEN start recording
        $("voice-modal").classList.remove("hidden");
        $("voice-tiles").innerHTML = "";
        $("voice-tiles").classList.add("hidden");
        $("voice-ai-reply").classList.add("hidden");
        $("voice-raw-transcript").classList.add("hidden");

        // Hide the confirm/edit buttons during recording
        const btnGroup = $("voice-edit-btn")?.parentElement;
        if (btnGroup) btnGroup.classList.add("hidden");

        $("voice-modal-title").textContent = "Vita is here...";
        await vitaGreet();

        // The user may have closed the modal while the greeting was playing;
        // don't start recording behind a hidden modal.
        if (myRun !== voiceRunId || $("voice-modal").classList.contains("hidden")) {
            stream.getTracks().forEach(t => t.stop());
            isStartingRecording = false;
            return;
        }

        mediaRecorder.start();
        isWhisperRecording = true;
        isStartingRecording = false;

        // Show recording icon constantly
        $("main-mic-icon").textContent = "graphic_eq";
        $("main-mic-fab").classList.remove("bg-primary-fixed");
        $("main-mic-fab").classList.add("bg-error");
        $("voice-modal-title").textContent = "Listening...";
        $("voice-done-btn")?.classList.remove("hidden");
        toast("Recording... tap the mic or \"Done\" when you're finished");

        // Volume-reactive avatar ring — purely visual feedback, no longer
        // tied to any stop/silence decision (see the function docstring above).
        const vizAudioContext = new window.AudioContext();
        const analyser = vizAudioContext.createAnalyser();
        const microphone = vizAudioContext.createMediaStreamSource(stream);
        microphone.connect(analyser);
        analyser.fftSize = 256;
        const bufferLength = analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);

        function animateRing() {
            if (!isWhisperRecording) {
                vizAudioContext.close();
                return;
            }
            analyser.getByteFrequencyData(dataArray);
            let sum = 0;
            for (let i = 0; i < bufferLength; i++) sum += dataArray[i];
            const average = sum / bufferLength;

            const ring = $("avatar-ring");
            if (ring) {
                const scale = 1 + (average / 60);
                ring.style.transform = `scale(${scale})`;
                ring.style.borderColor = `rgba(163, 230, 53, ${Math.min(1, average/30)})`; // Lime green glow
            }
            requestAnimationFrame(animateRing);
        }
        animateRing();

    } catch (e) {
        console.error("Mic Init Error:", e);
        isStartingRecording = false;
        if (stream) stream.getTracks().forEach(t => t.stop());
        showVoiceFallback("Microphone Error: " + (e.message || "Unknown error"));
    }
}

async function voiceSubmitTranscript(transcript, runId = voiceRunId) {
  toast("Extracting...");
  try {
    const res = await api("POST", "/v1/voice/extract", { transcript });
    if (runId !== voiceRunId) return;   // cancelled while extracting: don't render or speak the reply
    if (res.failed || !res.entries || res.entries.length === 0) {
      showVoiceFallback(`I heard "${transcript.trim().slice(0, 80)}" but couldn't find an exercise with an amount in it. Try again, e.g. "20 pushups" or "walked 2 kilometers", or log it manually.`);
      return;
    }
    voiceEntries = res.entries;
    const replyText = res.reply_text || "Got it!";
    $("voice-modal-title").textContent = "Vita";
    renderVoiceConfirmation(replyText);

    const avatar = $("avatar-container");
    if (avatar) {
        avatar.style.transform = "scale(1.05)";
        avatar.classList.add("animate-talk");
    }

    try {
        await speakVitaText(replyText);
    } catch (e) {
        console.error("Vita reply TTS failed", e);
    } finally {
        if (avatar) {
            avatar.style.transform = "scale(1)";
            avatar.classList.remove("animate-talk");
        }
    }
  } catch (e) {
    if (runId !== voiceRunId) return;
    showVoiceFallback(e.message || "Extraction failed. Please type instead.");
  }
}

function buildHumanSummary(entries) {
  return entries.map(e => {
    const name = e.activity_name || e.activity_type;
    const sets = e.set_amounts;
    const setsText = sets.length > 1
      ? `${sets.length} sets: ${sets.join(", ")}`
      : `${sets[0]}`;
    const unitText = e.unit && e.unit !== "count" ? ` ${e.unit}` : (sets.length === 1 ? " reps" : "");
    const durationText = e.duration_minutes ? ` in ${e.duration_minutes} minutes` : "";
    return `${name}: ${setsText}${unitText}${durationText}`;
  }).join(". ");
}

let voiceReplyText = "";

function renderVoiceConfirmation(replyText) {
  if (replyText) voiceReplyText = replyText;
  resetMicFab();
  $("voice-modal").classList.remove("hidden");
  $("voice-input-fallback").classList.add("hidden");

  // Vita AI reply bubble
  const replyEl = $("voice-ai-reply");
  if (replyEl) {
    replyEl.textContent = voiceReplyText;
    replyEl.classList.toggle("hidden", !voiceReplyText);
  }
  
  const rawEl = $("voice-raw-transcript");
  rawEl.textContent = `"${voiceTranscript}"`;
  rawEl.classList.remove("hidden");
  
  if (voiceStopTime > 0) {
      const waitTimeSec = ((Date.now() - voiceStopTime) / 1000).toFixed(1);
      const totalWords = voiceTranscript.trim().split(/\s+/).length;
      const speed = (totalWords / waitTimeSec).toFixed(1);
      const statsEl = $("voice-speed-stats");
      if (statsEl) {
          statsEl.textContent = `UX Latency: ${waitTimeSec}s | Streaming Feel: ${speed} words/sec`;
          statsEl.classList.remove("hidden");
      }
  }
  
  $("voice-tiles").classList.remove("hidden");
  // Found live: hidden at recording-start (toggleWhisperRecording) to keep
  // them off-screen while listening, but nothing ever un-hid the group once
  // a result came back — Confirm/Edit were fully wired up, just invisible.
  const btnGroup = $("voice-edit-btn")?.parentElement;
  if (btnGroup) btnGroup.classList.remove("hidden");
  $("voice-edit-btn").textContent = "Speak Again";
  $("voice-edit-btn").onclick = editVoiceLog;
  $("voice-confirm-btn").textContent = "Confirm";
  $("voice-confirm-btn").onclick = confirmVoiceLog;
  
  $("voice-tiles").innerHTML = voiceEntries.map((e, i) => {
    const name = escapeHtml(e.activity_name || e.activity_type);
    // Display words only — e.unit itself (count / km / min / sec) is what gets saved.
    const unitLabel = ({count: "reps", km: "km", min: "min", sec: "sec"})[e.unit] || e.unit || "";
    const estimated = !!e.estimated_from_minutes;   // distance worked out from a time (e.g. "walked 30 minutes")
    return e.set_amounts.map((target, idx) => `
      <div class="flex items-center gap-1.5 cursor-pointer select-none" onclick="editVoiceValue(${i}, ${idx})" title="Tap to edit this value">
        <div class="w-14 h-11 rounded-xl flex items-center justify-center shrink-0 shadow-sm transition-all bg-white/70 border border-black/10">
          <span class="material-symbols-outlined text-[20px] text-secondary">edit</span>
        </div>
        <div class="flex-1 h-11 rounded-xl px-sm flex items-center justify-between shadow-sm border transition-all bg-white/75 border-black/5">
          <span class="text-xs font-bold text-on-background">${name} - Set ${idx + 1}</span>
          <span class="text-xs font-bold text-secondary">${estimated ? "≈ " : ""}${target} ${unitLabel}${estimated ? ` (from ${e.estimated_from_minutes} min)` : ""}</span>
        </div>
      </div>`).join("");
  }).join("");
}

function editVoiceValue(entryIdx, setIdx) {
  const e = voiceEntries[entryIdx];
  const current = e.set_amounts[setIdx];
  const name = e.activity_name || e.activity_type;
  const val = window.prompt(`Edit value for ${name} - Set ${setIdx + 1}:`, current);
  if (!val) return;
  const num = parseFloat(val);
  if (!isNaN(num) && num > 0) {
    e.set_amounts[setIdx] = num;
    renderVoiceConfirmation(); // replyText preserved via voiceReplyText
  } else {
    toast("Invalid number", true);
  }
}

function showVoiceFallback(msg) {
  const fallbackMsgEl = $("voice-fallback-msg");
  if (fallbackMsgEl) fallbackMsgEl.textContent = msg || "We couldn't hear you. Please try again or log manually in the workout player.";

  $("voice-modal").classList.remove("hidden");
  $("voice-modal-title").textContent = "Vita";
  resetMicFab();
  $("voice-done-btn")?.classList.add("hidden");
  $("voice-tiles").classList.add("hidden");
  $("voice-raw-transcript").classList.add("hidden");
  $("voice-speed-stats").classList.add("hidden");
  $("voice-input-fallback").classList.remove("hidden");
  // Same fix as renderVoiceConfirmation — the button group is hidden at
  // recording-start and must be explicitly shown again here too.
  const fallbackBtnGroup = $("voice-edit-btn")?.parentElement;
  if (fallbackBtnGroup) fallbackBtnGroup.classList.remove("hidden");

  // "Try Again" — re-opens the mic
  $("voice-edit-btn").textContent = "Try Again";
  $("voice-edit-btn").onclick = () => {
    cancelVoiceLog();
    toggleWhisperRecording();
  };

  // "Workout Player" — just closes the modal
  $("voice-confirm-btn").textContent = "Workout Player";
  $("voice-confirm-btn").disabled = false;
  $("voice-confirm-btn").onclick = cancelVoiceLog;
}

async function confirmVoiceLog() {
  $("voice-confirm-btn").disabled = true;
  $("voice-confirm-btn").textContent = "Saving...";
  try {
    const customLookup = await api("GET", "/v1/custom-exercises");
    const existingByName = new Map(
      customLookup.custom_exercises.map(c => [c.name.toLowerCase(), c.id])
    );
    const entries = [];
    for (const e of voiceEntries) {
      if (!e.is_custom) {
        entries.push({
          activity_type: e.activity_type,
          set_amounts: e.set_amounts,
          unit: e.unit,
          source: "voice",
        });
        continue;
      }
      const sets = e.set_amounts.length;
      const amount = e.set_amounts.reduce((a, b) => a + b, 0) / sets;
      let customId = existingByName.get(e.activity_name.toLowerCase());
      if (!customId) {
        // First time this spoken name is seen: there's no explicit
        // target-setting step in voice logging, so what was just said
        // becomes the exercise's target going forward — same as any other
        // custom exercise still needing one of these to exist at all.
        const measurementType = e.unit === "km" ? "distance" : e.unit === "min" ? "duration" : "reps";
        const created = await api("POST", "/v1/custom-exercises", {
          name: e.activity_name, measurement_type: measurementType,
          target_amount: amount, target_sets: sets,
        });
        customId = created.id;
      }
      entries.push({
        custom_exercise_id: customId, amount, sets, unit: e.unit, source: "voice",
      });
    }
    const result = await api("POST", "/v1/activities", { entries });
    toast(`Saved · ${result.streak.current} day streak`);
    cancelVoiceLog();
    if (document.getElementById("s-home").classList.contains("on")) loadHome();
  } catch (err) {
    toast(err.message || "Failed to save");
    $("voice-confirm-btn").disabled = false;
    $("voice-confirm-btn").textContent = "Confirm";
  }
}

// Speak Again: close this confirmation and record afresh — the same two steps the
// Try Again button uses. (This used to call startVoiceLogging(), a function that
// has never existed, so the popup closed and nothing recorded.)
function editVoiceLog() {
  cancelVoiceLog();
  toggleWhisperRecording();
}

function cancelVoiceLog() {
  voiceRunId++;   // whatever the old attempt is doing (recording / transcribing / extracting / speaking) is now stale
  $("voice-modal").classList.add("hidden");
  voiceEntries = [];
  voiceTranscript = "";
  if (isWhisperRecording) {
    // Used to call toggleWhisperRecording(), which STOPPED the recording and so
    // still transcribed and submitted it. Now the mic is just switched off; the
    // recorder's onstop sees a stale run id and discards the audio.
    stopWhisperRecording();
  }
  if (window.speechSynthesis) window.speechSynthesis.cancel();   // stop a greeting/reply being spoken
  stopElevenLabsAudio();                                         // ...including the premium voice, immediately
  resetMicFab();
  $("voice-confirm-btn").disabled = false;
  $("voice-confirm-btn").textContent = "Confirm";
}

// ── workout player (guided, one exercise at a time) ──────────────────────
// Reuses the exact same /v1/activities log call as the dashboard cards —
// each "Mark Done" logs exactly the prescribed sets/amount, tagged with the
// real workout_sessions record so its duration/count are server-computed,
// never this screen's own live clock (that's cosmetic feedback only).
let PLAYER_ITEMS = [];
let playerIndex = 0;
let playerSessionId = null;
let playerStartedAt = null;
let playerClockHandle = null;
let playerRestHandle = null;
let playerRestRemaining = 0;
let playerPaused = false;
let playerPausedAt = null;
// Set after an exercise is saved; cleared when the soreness question is
// answered or the next exercise renders. Keeps soreness + rest fully decoupled:
// the question only appears once the rest period is over, never simultaneously.
let playerPendingSorenessType = null;
// Called by dismissSorenessPrompt / reportExerciseSoreness after the user
// responds. For mid-workout exercises this is () => playerGo(1); for the
// last exercise it is finishPlayerSession so the done-view only shows AFTER
// the soreness question is answered — same experience as every other exercise.
let playerPendingSorenessAction = null;

function formatClock(totalSeconds) {
  const m = Math.floor(totalSeconds / 60), s = Math.max(0, Math.floor(totalSeconds % 60));
  return `${m}:${s < 10 ? "0" : ""}${s}`;
}

function startPlayerClock() {
  clearInterval(playerClockHandle);
  playerClockHandle = setInterval(() => {
    $("pl-clock").textContent = formatClock((Date.now() - playerStartedAt.getTime()) / 1000);
  }, 1000);
}

function startRestTicker() {
  clearInterval(playerRestHandle);
  playerRestHandle = setInterval(() => {
    playerRestRemaining--;
    if (playerRestRemaining <= 0) { playerSkipRest(); return; }
    $("pl-rest-counter").textContent = formatClock(playerRestRemaining);
  }, 1000);
}

// Real pause (unlike the mockup's own cosmetic pause icon): stops both the
// elapsed-time clock and any running rest countdown, and on resume shifts
// playerStartedAt forward by the pause duration so the displayed elapsed
// time doesn't jump by however long the pause lasted.
function playerTogglePause() {
  playerPaused = !playerPaused;
  $("pl-pause-icon").textContent = playerPaused ? "play_arrow" : "pause";
  if (playerPaused) {
    playerPausedAt = Date.now();
    clearInterval(playerClockHandle);
    clearInterval(playerRestHandle);
  } else {
    playerStartedAt = new Date(playerStartedAt.getTime() + (Date.now() - playerPausedAt));
    startPlayerClock();
    if (!$("pl-rest").classList.contains("hidden")) startRestTicker();
  }
}

async function startWorkout() {
  // Always show the full plan, not just what's left — an already-completed
  // exercise still appears (marked done), so the player reflects the whole
  // day's plan rather than hiding anything once it's logged.
  const toPlay = HOME_ITEMS;
  if (!toPlay.length) {
    // Program is completely empty — there is no separate exercise-list
    // screen to point at any more (it moved entirely into this Player), so
    // open the add-exercise catalogue directly instead of a dead-end toast.
    // addFromCatalogue's refreshPlayerItems() starts the workout automatically
    // once something's actually been added.
    openCatalogue();
    return;
  }
  const sorted = toPlay.slice().sort((a, b) => (a.priority_order ?? 99) - (b.priority_order ?? 99));
  try {
    const started = await api("POST", "/v1/workout-sessions",
      {activity_types: sorted.map(i => i.activity_type)});
    PLAYER_ITEMS = sorted;
    playerIndex = 0;
    playerSessionId = started.id;
    playerStartedAt = new Date(started.started_at);
    playerPaused = false;
    $("pl-pause-icon").textContent = "pause";
    go("player");
    renderPlayerExercise();
    startPlayerClock();
  } catch (e) { toast(e.message, true); }
}

// Which sets of the CURRENT exercise have been tapped done, this screen
// visit — reset every time renderPlayerExercise runs. Index-aligned with
// PLAYER_ITEMS[playerIndex].set_targets (one real target per set, which can
// genuinely differ set to set once progression has diverged them — see
// PER_SET_PROGRESSION_PLAN.md — never assumed uniform).
let playerSetDone = [];
let playerDemoFrameTimer = null;
let playerSetActual = [];
let playerActualAmount = null;

function playerUseTarget() {
  const i = PLAYER_ITEMS[playerIndex];
  if (!i) return;
  playerActualAmount = i.amount;
  if ($("pl-distance-input")) $("pl-distance-input").value = (i.amount || 1).toFixed(2);
  updatePlayerDoneButtonText();
}

function playerSetDistance(val) {
  const parsed = parseFloat(val);
  if (!isNaN(parsed) && parsed >= 0) {
    playerActualAmount = Math.round(parsed * 100) / 100;
  }
  updatePlayerDoneButtonText();
}

function playerAdjustDistance(delta) {
  const current = playerActualAmount != null ? playerActualAmount : 0;
  const next = Math.max(0, Math.round((current + delta) * 100) / 100);
  playerActualAmount = next;
  if ($("pl-distance-input")) $("pl-distance-input").value = next.toFixed(2);
  updatePlayerDoneButtonText();
}

// An exercise the player already saved today is shown locked (values visible,
// not editable) with an Edit button; Edit opens it for changes, and Mark Done
// then OVERWRITES today's entry and locks it again. Per item, never carried
// to the next exercise or the next day (the server only returns player_log
// for today).
function playerEntryLocked(i) {
  return !!(i && i.player_log && !i._editing);
}

function enterPlayerEdit(i) {
  i._editing = true;
  renderPlayerExercise();
}

function updatePlayerDoneButtonText() {
  const i = PLAYER_ITEMS[playerIndex];
  if (!i) return;
  if (playerEntryLocked(i)) { $("pl-done-btn").textContent = "Edit"; return; }
  const isDistOrDur = i.prescription_type === "distance" || i.prescription_type === "duration";
  if (isDistOrDur) {
    const amt = playerActualAmount != null ? playerActualAmount : 0;
    $("pl-done-btn").textContent = `Mark Done (${amt.toFixed(2)} ${i.unit || "km"})`;
  } else {
    $("pl-done-btn").textContent = "Mark Done";
  }
}

function renderPlayerExercise() {
  const total = PLAYER_ITEMS.length;
  const i = PLAYER_ITEMS[playerIndex];
  $("pl-progress").innerHTML = PLAYER_ITEMS.map((item, idx) =>
    `<div class="flex-1 h-full bg-black/10 rounded-full overflow-hidden">
       <div class="h-full rounded-full bg-primary-fixed transition-all duration-300" style="width:${idx <= playerIndex || item.completed ? 100 : 0}%"></div>
     </div>`
  ).join("");
  $("pl-counter").textContent = `${playerIndex + 1} of ${total}`;
  $("pl-icon").textContent = icon(i.activity_type);
  $("pl-title").textContent = i.display_name;
  $("pl-done-badge").classList.toggle("hidden", !i.completed);
  $("pl-prescription").textContent = i.prescription
    + (i.est_minutes ? ` · about ${i.est_minutes} min` : "")
    + (i.rest_seconds ? ` · ${i.rest_seconds}s rest after` : "");
  $("pl-caution").textContent = i.caution || "";
  $("pl-caution").classList.toggle("hidden", !i.caution);
  // Demo video card, matching Exercise workout Player.html's own — only
  // shown when this exercise actually has one (no fabricated video for
  // ones that don't).
  const hasDemo = !!(i.demo_gif || (i.demo_images && i.demo_images.length));
  $("pl-video-card").classList.toggle("hidden", !hasDemo);
  if (hasDemo) {
    $("pl-video-footer-label").textContent = i.display_name;
    $("pl-video-img").src = assetUrl(i.demo_gif || i.demo_images[0]);
    // No real animated GIF for this one — alternate its 2 still photos,
    // same idea as the GIF's own looping, so it isn't just a frozen image.
    clearInterval(playerDemoFrameTimer);
    if (!i.demo_gif && i.demo_images && i.demo_images.length > 1) {
      const frames = i.demo_images.map(assetUrl);
      let frame = 0;
      playerDemoFrameTimer = setInterval(() => {
        frame = 1 - frame;
        $("pl-video-img").src = frames[frame];
      }, 900);
    }
  } else {
    clearInterval(playerDemoFrameTimer);
  }
  $("pl-prev").disabled = playerIndex === 0;
  $("pl-next").disabled = playerIndex === total - 1;
  $("pl-rest").classList.add("hidden");
  $("pl-exercise-view").classList.remove("hidden");
  $("pl-done-view").classList.add("hidden");
  playerPendingSorenessType = null;
  playerPendingSorenessAction = null;
  if ($("pl-soreness-prompt")) $("pl-soreness-prompt").classList.add("hidden");
  if ($("pl-done-soreness-prompt")) $("pl-done-soreness-prompt").classList.add("hidden");

  // Single-effort activities (distance/duration — Walking, Running: already
  // locked to exactly 1 "set" server-side) keep the old one-tap button.
  // Everything else (reps/hold) gets the real per-set slide table instead.
  const singleEffort = i.prescription_type === "distance" || i.prescription_type === "duration";
  const locked = playerEntryLocked(i);
  // Reps/hold exercises have no button until they've been saved: then it is
  // the Edit / Mark Done control for correcting that saved entry.
  $("pl-done-btn").classList.toggle("hidden", !(singleEffort || i.player_log));
  $("pl-sets-table").classList.toggle("hidden", singleEffort);
  $("pl-gps-btn").classList.toggle("hidden", !(i.activity_type === "walk" || i.activity_type === "running"));

  if ($("pl-distance-editor")) {
    $("pl-distance-editor").classList.toggle("hidden", !singleEffort);
    if (singleEffort) {
      if (locked) {
        playerActualAmount = i.player_log.amount; // always show what was saved
      } else if (playerActualAmount == null) {
        playerActualAmount = i.player_log ? i.player_log.amount : 0.0;
      }
      $("pl-distance-editor").classList.toggle("pointer-events-none", locked);
      $("pl-distance-editor").classList.toggle("opacity-60", locked);
      if ($("pl-target-val")) $("pl-target-val").textContent = `${i.amount} ${i.unit || "km"}`;
      if ($("pl-distance-input")) $("pl-distance-input").value = playerActualAmount.toFixed(2);
      if ($("pl-distance-unit")) $("pl-distance-unit").textContent = i.unit || "km";
    }
  }

  if (singleEffort) {
    $("pl-done-btn").disabled = false;
    updatePlayerDoneButtonText();
  } else {
    // Already saved today: show exactly what was saved (all sets ticked).
    const saved = playerSavedSetValues(i);
    const targets = saved || i.set_targets || Array(i.sets).fill(i.amount);
    playerSetDone = targets.map(() => !!saved);
    // What actually gets logged per set — starts equal to the target, but
    // editable before marking done, for a set done with more or less than
    // planned (e.g. only managed 9 of a planned 11 reps).
    playerSetActual = targets.slice();
    renderSetsTable(i);
    if (saved) {
      $("pl-done-btn").disabled = false;
      $("pl-done-btn").textContent = locked ? "Edit" : "Mark Done";
    }
  }
}

// The per-set values saved today by the player, or null if none.
function playerSavedSetValues(i) {
  if (!i.player_log) return null;
  return i.player_log.set_amounts || Array(i.player_log.sets).fill(i.player_log.amount);
}

function renderSetsTable(i) {
  const targets = playerSavedSetValues(i) || i.set_targets || Array(i.sets).fill(i.amount);
  const unit = i.prescription_type === "hold" ? "s" : "";
  // Row shape matches Exercise workout Player.html's own sets table: the
  // tile on the left marks the set done (reading whatever's currently in
  // that row's own box); the amount itself is always a live editable field,
  // no separate edit button or mode needed — type a different number
  // directly, then tap the tile, same single step either way. Locked
  // (read-only) once marked done, so it can't drift after being logged;
  // tapping the tile again to un-mark re-enables it.
  $("pl-sets-table").innerHTML = targets.map((target, idx) => {
    const done = playerSetDone[idx];
    const actual = playerSetActual[idx] ?? target;
    return `
    <div class="flex items-center gap-1.5 select-none">
      <div onclick="playerToggleSet(${idx})" title="Tap to mark Set ${idx + 1} done"
        class="w-14 h-11 rounded-xl flex items-center justify-center shrink-0 shadow-sm transition-all cursor-pointer
                  ${done ? "bg-primary-fixed" : "bg-white/70 border border-black/10"}">
        <span class="material-symbols-outlined text-[20px] ${done ? "text-on-primary-fixed" : "text-secondary"}">${done ? "check" : "chevron_right"}</span>
      </div>
      <div class="flex-1 h-11 rounded-xl pl-sm pr-xs flex items-center justify-between gap-xs shadow-sm border transition-all
                  ${done ? "bg-white/80 border-primary/50" : "bg-white/75 border-black/5"}">
        <span class="text-xs font-bold text-on-background shrink-0">Set ${idx + 1}</span>
        <div class="flex items-center gap-1">
          <input type="number" step="any" value="${actual}" ${done && !i._editing ? "disabled" : ""}
            onchange="updatePlayerSetActual(${idx}, this.value)"
            class="no-spinner w-14 text-center text-sm font-bold rounded-lg py-1 border-none outline-none
                   ${done ? "bg-transparent text-primary" : "bg-surface-container-low text-on-background"}">
          ${unit ? `<span class="text-xs font-bold text-secondary shrink-0">${unit}</span>` : ""}
        </div>
      </div>
    </div>`;
  }).join("");
}

function updatePlayerSetActual(idx, value) {
  const parsed = parseFloat(value);
  if (!parsed || parsed <= 0) { toast("Enter an amount greater than zero.", true); return; }
  playerSetActual[idx] = parsed;
}

function playerToggleSet(idx) {
  if (!$("pl-rest").classList.contains("hidden")) return;
  if (PLAYER_ITEMS[playerIndex].player_log) return; // locked, or editing via Mark Done
  playerSetDone[idx] = !playerSetDone[idx];
  const i = PLAYER_ITEMS[playerIndex];
  renderSetsTable(i);
  if (playerSetDone.every(Boolean)) playerSubmitSets();
}

async function playerSubmitSets() {
  const i = PLAYER_ITEMS[playerIndex];
  const targets = i.set_targets || Array(i.sets).fill(i.amount);
  // Lock the table to all-done immediately so the user can't re-tap a set
  // and un-mark it while the save is in flight (fixes the double-tap bug).
  playerSetDone = targets.map(() => true);
  renderSetsTable(i);
  // Log what was actually edited per set (defaults to the target for any
  // set left unedited), not blindly the planned targets.
  const result = await send(i.activity_type, null, null, playerSessionId, playerSetActual, true);
  if (!result) {
    // Save failed — reset table so the user can retry.
    playerSetDone = targets.map(() => false);
    renderSetsTable(i);
    return;
  }
  playerRememberSaved(i, playerSetActual.length, playerSetActual.slice());
  if (playerIndex === PLAYER_ITEMS.length - 1) {
    // Last exercise: show soreness on this page (same UX as every other
    // exercise), then call finishPlayerSession once user responds.
    playerPendingSorenessType = i.activity_type;
    playerPendingSorenessAction = finishPlayerSession;
    showSorenessPrompt(i.activity_type);
  } else if (i.rest_seconds) {
    // Has rest: store type + action, show soreness AFTER rest ends.
    playerPendingSorenessType = i.activity_type;
    playerPendingSorenessAction = () => playerGo(1);
    playerStartRest(i.rest_seconds);
  } else {
    // No rest: show soreness now; dismiss/report handlers will navigate.
    playerPendingSorenessType = i.activity_type;
    playerPendingSorenessAction = () => playerGo(1);
    showSorenessPrompt(i.activity_type);
  }
}

function playerGo(delta) {
  const next = playerIndex + delta;
  if (next < 0 || next >= PLAYER_ITEMS.length) return;
  if (PLAYER_ITEMS[playerIndex]) PLAYER_ITEMS[playerIndex]._editing = false; // abandoned edit -> locked again
  playerIndex = next;
  playerActualAmount = null;
  renderPlayerExercise();
}

// Remembers what the player just saved for this exercise (same shape the
// server returns as player_log) so it shows locked from now on.
function playerRememberSaved(i, sets, setAmounts) {
  const total = setAmounts.reduce((a, b) => a + b, 0);
  i.player_log = {sets, amount: total / sets, set_amounts: setAmounts.length > 1 || sets > 1 ? setAmounts : null};
  i.completed = true;
  i._editing = false;
}

// Mark Done while editing a saved entry: overwrites it, stays on this
// exercise (no rest timer / soreness prompt again), and locks it.
async function playerSaveEdit(i) {
  const singleEffort = i.prescription_type === "distance" || i.prescription_type === "duration";
  let result;
  if (singleEffort) {
    const amt = playerActualAmount;
    if (!(amt > 0)) { toast("Enter an amount greater than zero.", true); return; }
    $("pl-done-btn").disabled = true;
    result = await send(i.activity_type, amt, i.sets, playerSessionId, null, true);
    if (result) { i.player_log = {sets: 1, amount: amt, set_amounts: null}; i.completed = true; i._editing = false; }
  } else {
    const vals = Array.from(document.querySelectorAll("#pl-sets-table input"), el => parseFloat(el.value));
    if (!vals.length || vals.some(v => !Number.isFinite(v) || v <= 0)) {
      toast("Enter an amount greater than zero for every set.", true);
      return;
    }
    $("pl-done-btn").disabled = true;
    result = await send(i.activity_type, null, null, playerSessionId, vals, true);
    if (result) playerRememberSaved(i, vals.length, vals);
  }
  $("pl-done-btn").disabled = false;
  renderPlayerExercise();
}

async function playerMarkDone() {
  const i = PLAYER_ITEMS[playerIndex];
  if (playerEntryLocked(i)) { enterPlayerEdit(i); return; }   // Edit tapped
  if (i._editing) { await playerSaveEdit(i); return; }         // Mark Done after Edit
  const singleEffort = i.prescription_type === "distance" || i.prescription_type === "duration";
  const amountToLog = singleEffort ? (playerActualAmount != null ? playerActualAmount : 0) : i.amount;

  $("pl-done-btn").disabled = true;
  $("pl-done-btn").textContent = "Saving…";

  // If user didn't move (0.00 km): Mark Done works cleanly and advances without error
  if (singleEffort && amountToLog <= 0) {
    toast("Marked done (0.00 km)", false);
    if (playerIndex === PLAYER_ITEMS.length - 1) {
      playerPendingSorenessType = i.activity_type;
      playerPendingSorenessAction = finishPlayerSession;
      showSorenessPrompt(i.activity_type);
    } else if (i.rest_seconds) {
      playerPendingSorenessType = i.activity_type;
      playerPendingSorenessAction = () => playerGo(1);
      playerStartRest(i.rest_seconds);
    } else {
      playerPendingSorenessType = i.activity_type;
      playerPendingSorenessAction = () => playerGo(1);
      showSorenessPrompt(i.activity_type);
    }
    return;
  }

  const result = await send(i.activity_type, amountToLog, i.sets, playerSessionId, null, true);
  if (!result) {
    $("pl-done-btn").disabled = false;
    updatePlayerDoneButtonText();
    return;
  }
  i.player_log = {sets: 1, amount: amountToLog, set_amounts: null};
  i.completed = true;
  if (playerIndex === PLAYER_ITEMS.length - 1) {
    playerPendingSorenessType = i.activity_type;
    playerPendingSorenessAction = finishPlayerSession;
    showSorenessPrompt(i.activity_type);
  } else if (i.rest_seconds) {
    playerPendingSorenessType = i.activity_type;
    playerPendingSorenessAction = () => playerGo(1);
    playerStartRest(i.rest_seconds);
  } else {
    playerPendingSorenessType = i.activity_type;
    playerPendingSorenessAction = () => playerGo(1);
    showSorenessPrompt(i.activity_type);
  }
}

function playerStartRest(seconds) {
  playerRestRemaining = seconds;
  $("pl-rest").classList.remove("hidden");
  // Do NOT hide pl-sets-table — completed sets stay visible (read-only, since
  // playerToggleSet already blocks taps while the rest pill is shown) so the
  // exercise card never looks empty during the rest period.
  $("pl-done-btn").classList.add("hidden");
  $("pl-rest-counter").textContent = formatClock(playerRestRemaining);
  startRestTicker();
}

function playerAddRestTime() {
  playerRestRemaining += 15;
  $("pl-rest-counter").textContent = formatClock(playerRestRemaining);
}

function playerSkipRest() {
  clearInterval(playerRestHandle);
  $("pl-rest").classList.add("hidden");
  if (playerPendingSorenessType) {
    // Rest is over — NOW show the soreness question.
    // playerPendingSorenessAction stays set so the dismiss/report handler
    // knows what to do (go to next exercise or finish the session).
    showSorenessPrompt(playerPendingSorenessType);
    playerPendingSorenessType = null;
  } else {
    playerGo(1);
  }
}

async function finishPlayerSession() {
  clearInterval(playerClockHandle);
  clearInterval(playerRestHandle);
  clearInterval(playerDemoFrameTimer);
  const sid = playerSessionId;
  // Cleared before the call resolves: a second tap on the close button
  // while this is in flight, or a tap right after it succeeds, must not
  // fire a second /finish call against a session that's already ended.
  playerSessionId = null;
  if (!sid) { go("home"); return; }
  try {
    const result = await api("POST", `/v1/workout-sessions/${sid}/finish`);
    $("pl-exercise-view").classList.add("hidden");
    $("pl-rest").classList.add("hidden");
    $("pl-done-view").classList.remove("hidden");
    $("pl-summary").textContent =
      `${result.completed_activity_count} of ${result.planned_activity_count} exercises done `
      + `in ${formatClock(result.duration_seconds)}.`;
  } catch (e) {
    toast(e.message, true);
    go("home");
  }
}

function closePlayer() {
  finishPlayerSession();
}

let PE_SET_VALUES = [];

function playerEditCurrent() {
  const i = PLAYER_ITEMS[playerIndex];
  const single = i.prescription_type === "distance" || i.prescription_type === "duration";
  $("pe-title").textContent = `Edit ${i.display_name}`;
  $("pe-sets-wrap").classList.toggle("hidden", single);
  $("pe-amount-only-wrap").classList.toggle("hidden", !single);
  if (single) {
    $("pe-amount-only-label").textContent = `Amount${i.unit ? " (" + i.unit + ")" : ""}`;
    $("pe-amount-only").value = i.amount;
  } else {
    $("pe-amount-label").textContent = `Amount${i.unit ? " (" + i.unit + ")" : ""}`;
    $("pe-sets").value = i.sets;
    $("pe-amount").value = i.amount;
  }
  $("pe-per-set-toggle-wrap").classList.toggle("hidden", single);
  $("pe-per-set-toggle-label").textContent =
    i.prescription_type === "reps" ? "Different reps for each set"
    : i.prescription_type === "hold" ? "Different hold time for each set"
    : "Different amount for each set";
  $("pe-per-set-toggle").checked = false;
  $("pe-per-set-inputs").classList.add("hidden");
  $("pe-auto").checked = i.auto_progress !== false;
  $("pe-auto").disabled = false;
  $("pe-auto-wrap").classList.remove("opacity-50");
  $("pl-edit-modal").classList.remove("hidden");
  $("pl-edit-modal").classList.add("flex");
}

function closePlayerEditModal() {
  $("pl-edit-modal").classList.add("hidden");
  $("pl-edit-modal").classList.remove("flex");
}

function togglePePerSet() {
  const on = $("pe-per-set-toggle").checked;
  $("pe-sets-wrap").classList.toggle("hidden", on);
  $("pe-per-set-inputs").classList.toggle("hidden", !on);
  const i = PLAYER_ITEMS[playerIndex];
  if (on) {
    const n = Math.max(1, parseInt($("pe-sets").value, 10) || i.sets);
    PE_SET_VALUES = (i.set_targets && i.set_targets.length === n)
      ? i.set_targets.slice() : Array.from({length: n}, () => i.amount);
    renderPePerSetInputs();
  }
}

function flushPeSetInputs() {
  const current = $("pe-per-set-inputs").querySelectorAll("input");
  current.forEach((el, idx) => { PE_SET_VALUES[idx] = parseFloat(el.value) || PE_SET_VALUES[idx]; });
}

function renderPePerSetInputs() {
  $("pe-per-set-inputs").innerHTML = PE_SET_VALUES.map((v, idx) => `
    <div class="flex items-center justify-between gap-sm">
      <span class="font-label-bold text-label-bold text-on-surface w-14 shrink-0">Set ${idx + 1}</span>
      <input type="number" step="any" value="${v}" onchange="updatePeSetValue(${idx}, this.value)"
        class="flex-1 bg-surface-container-low border-none rounded-lg px-md py-sm font-body-md text-body-md text-center">
      ${PE_SET_VALUES.length > 1 ? `
      <button onclick="removePeSetRow(${idx})" aria-label="Remove set" class="shrink-0 text-secondary hover:text-error">
        <span class="material-symbols-outlined text-[20px]">close</span>
      </button>` : `<span class="w-5 shrink-0"></span>`}
    </div>`).join("")
    + `<button onclick="addPeSetRow()" class="flex items-center justify-center gap-1 text-primary font-label-bold text-label-bold py-xs">
         <span class="material-symbols-outlined text-[18px]">add</span> Add Set
       </button>`;
}

function updatePeSetValue(idx, value) {
  PE_SET_VALUES[idx] = parseFloat(value) || 0;
}

function addPeSetRow() {
  flushPeSetInputs();
  PE_SET_VALUES.push(PE_SET_VALUES[PE_SET_VALUES.length - 1]);
  renderPePerSetInputs();
}

function removePeSetRow(idx) {
  flushPeSetInputs();
  PE_SET_VALUES.splice(idx, 1);
  renderPePerSetInputs();
}

async function submitPlayerEdit() {
  const i = PLAYER_ITEMS[playerIndex];
  const single = i.prescription_type === "distance" || i.prescription_type === "duration";
  const perSet = !single && $("pe-per-set-toggle").checked;
  let sets, amount, setAmounts;
  if (perSet) {
    flushPeSetInputs();
    setAmounts = PE_SET_VALUES.slice();
    if (!setAmounts.length || setAmounts.some(v => !v || v <= 0)) {
      toast("Enter an amount greater than zero for every set.", true);
      return;
    }
  } else {
    sets = single ? 1 : parseInt($("pe-sets").value, 10);
    amount = parseFloat(single ? $("pe-amount-only").value : $("pe-amount").value);
    if (!single && (!Number.isFinite(sets) || sets < 1)) { toast("Enter a valid number of sets", true); return; }
    if (!Number.isFinite(amount) || amount <= 0) { toast("Enter a valid amount", true); return; }
  }
  const autoProgress = $("pe-auto").checked;
  try {
    const body = perSet
      ? { set_amounts: setAmounts, auto_progress: autoProgress }
      : { target_sets: sets, target_amount: amount, auto_progress: autoProgress };
    const r = await api("PUT", `/v1/program/entries/${i.activity_type}/target`, body);
    i.sets = r.target_sets;
    i.amount = r.target_amount;
    i.set_targets = r.set_targets || null;
    i.auto_progress = r.auto_progress;
    i.prescription = single ? `${r.target_amount} ${i.unit || ""}`.trim()
                             : `${r.target_sets} x ${r.target_amount}${i.unit ? " " + i.unit : ""}`;
    closePlayerEditModal();
    renderPlayerExercise();
    toast("Updated");
  } catch (e) { toast(e.message, true); }
}

async function playerRemoveCurrent() {
  const i = PLAYER_ITEMS[playerIndex];
  if (!(await appConfirm({ message: `Remove ${i.display_name} from today's plan?`, confirmText: "Remove", danger: true }))) return;
  try {
    await api("DELETE", `/v1/program/entries/${i.activity_type}`);
  } catch (e) { toast(e.message, true); return; }
  PLAYER_ITEMS.splice(playerIndex, 1);
  if (!PLAYER_ITEMS.length) { await finishPlayerSession(); return; }
  if (playerIndex >= PLAYER_ITEMS.length) playerIndex = PLAYER_ITEMS.length - 1;
  renderPlayerExercise();
  toast("Removed");
}

// ── outdoor GPS run/walk tracker — third logging method alongside the
// guided Workout Player and voice. Reuses the same workout_sessions
// start/finish the Player already uses; the final distance logs through the
// normal /v1/activities path with source="gps". No route is saved anywhere
// (v1 scope: live tracking only, no route history) and there is no claim of
// background tracking — both iOS and (often) Android suspend a web page's
// location updates once it's no longer the foreground tab, so this only
// works reliably with the screen on and the tab in front. ──
let gpsActivityType = null;
let gpsWatchId = null;
let gpsMap = null;
let gpsPolyline = null;
let gpsSessionId = null;
let gpsStartedAt = null;
let gpsClockHandle = null;
let gpsDistanceKm = 0;
let gpsLastFix = null; // {lat, lon, t} of the last ACCEPTED fix
let gpsPlayerLinked = false; // started from the Workout Player, not the Dashboard shortcut

// Real-world GPS fixes jitter even when stationary, and the error is worse
// than the jitter once you try to tell "walked 3m" from "GPS drifted 3m"
// apart with a fixed cutoff — especially at walking speed. Instead: reject a
// fix outright if the device itself reports poor confidence, then reject a
// fix if the IMPLIED speed since the last accepted fix is physically
// implausible for a foot-powered activity (a GPS jump, not real movement).
const GPS_MAX_ACCURACY_M = 30;
const GPS_MAX_PLAUSIBLE_KMH = 25; // generous ceiling above even a fast sprint

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371;
  const toRad = d => d * Math.PI / 180;
  const dLat = toRad(lat2 - lat1), dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function openGpsTracker() {
  gpsPlayerLinked = false;
  gpsActivityType = null;
  gpsDistanceKm = 0;
  gpsLastFix = null;
  $("gps-pick-walk").className = "py-md rounded-xl border-2 border-outline-variant/40 font-label-bold text-label-bold text-on-surface";
  $("gps-pick-running").className = "py-md rounded-xl border-2 border-outline-variant/40 font-label-bold text-label-bold text-on-surface";
  $("gps-start-btn").disabled = true;
  $("gps-setup").classList.remove("hidden");
  $("gps-active").classList.add("hidden");
  go("gps");
}

// Reached from the Workout Player's "Track with GPS instead" button, only
// shown for the current exercise when it's Walking/Running (renderPlayerExercise).
// Logs against the PLAYER's own already-active session instead of starting a
// second one — this is the same workout, just measured with real GPS distance
// instead of the player's one-tap "Mark Done".
function openGpsTrackerForPlayer() {
  const i = PLAYER_ITEMS[playerIndex];
  gpsPlayerLinked = true;
  gpsActivityType = i.activity_type;
  gpsSessionId = playerSessionId;
  gpsDistanceKm = 0;
  gpsLastFix = null;
  $("gps-setup").classList.add("hidden");
  $("gps-active").classList.remove("hidden");
  go("gps");
  beginGpsWatch();
}

function closeGpsTracker() {
  if (gpsWatchId !== null) {
    navigator.geolocation.clearWatch(gpsWatchId);
    gpsWatchId = null;
    if (gpsClockHandle) { clearInterval(gpsClockHandle); gpsClockHandle = null; }
    if (!gpsPlayerLinked) {
      // Leaving mid-track without hitting Finish — stop watching and abandon
      // the session server-side rather than leave it dangling as "active"
      // forever (same self-healing concept start_workout_session already
      // relies on, just triggered immediately instead of on the next start).
      if (gpsSessionId) api("POST", `/v1/workout-sessions/${gpsSessionId}/finish`, {}).catch(() => {});
    }
  }
  if (gpsPlayerLinked) {
    playerActualAmount = Math.round(gpsDistanceKm * 100) / 100;
  }
  go(gpsPlayerLinked ? "player" : "home");
  if (gpsPlayerLinked) {
    renderPlayerExercise();
  }
}

function selectGpsActivity(type) {
  gpsActivityType = type;
  $("gps-pick-walk").className = `py-md rounded-xl border-2 font-label-bold text-label-bold ${type === "walk" ? "border-primary bg-primary-fixed/20 text-on-primary-fixed-variant" : "border-outline-variant/40 text-on-surface"}`;
  $("gps-pick-running").className = `py-md rounded-xl border-2 font-label-bold text-label-bold ${type === "running" ? "border-primary bg-primary-fixed/20 text-on-primary-fixed-variant" : "border-outline-variant/40 text-on-surface"}`;
  $("gps-start-btn").disabled = false;
}

async function startGpsTracking() {
  if (!gpsActivityType) return;
  if (!navigator.geolocation) {
    toast("This device/browser doesn't support location tracking.", true);
    return;
  }
  try {
    const started = await api("POST", "/v1/workout-sessions", {activity_types: [gpsActivityType]});
    gpsSessionId = started.id;
    $("gps-setup").classList.add("hidden");
    $("gps-active").classList.remove("hidden");
    beginGpsWatch();
  } catch (e) { toast(e.message, true); }
}

// Shared by both entry points (Dashboard shortcut, once its own session is
// started above; Workout Player, whose session already exists by the time
// this is called) — everything from here on is identical either way.
function beginGpsWatch() {
  gpsStartedAt = new Date();
  gpsDistanceKm = 0;
  gpsLastFix = null;
  $("gps-distance").textContent = "0.00";
  $("gps-duration").textContent = "00:00";
  $("gps-pace").textContent = "--";
  $("gps-signal-note").textContent = "Finding your GPS signal…";

  setTimeout(() => {
    if (!gpsMap) {
      gpsMap = L.map("gps-map");
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19, attribution: "© OpenStreetMap",
      }).addTo(gpsMap);
      gpsPolyline = L.polyline([], {color: "#2563eb", weight: 4}).addTo(gpsMap);
    }
    gpsMap.hasLaidFirstView = false;
    gpsPolyline.setLatLngs([]);
    gpsMap.invalidateSize();
  }, 50);

  gpsClockHandle = setInterval(updateGpsClock, 1000);
  gpsWatchId = navigator.geolocation.watchPosition(onGpsPosition, onGpsError, {
    enableHighAccuracy: true, maximumAge: 2000, timeout: 15000,
  });
}

function onGpsError(err) {
  $("gps-signal-note").textContent = err.code === err.PERMISSION_DENIED
    ? "Location permission denied — enable it in your phone's settings to keep tracking."
    : "Waiting for a GPS signal…";
}

function onGpsPosition(pos) {
  const {latitude: lat, longitude: lon, accuracy} = pos.coords;
  const t = pos.timestamp;

  if (!gpsMap.hasLaidFirstView) {
    gpsMap.setView([lat, lon], 17);
    gpsMap.hasLaidFirstView = true;
  }
  L.circleMarker([lat, lon], {radius: 5, color: "#2563eb"}).addTo(gpsMap);

  if (accuracy > GPS_MAX_ACCURACY_M) {
    $("gps-signal-note").textContent = "Weak GPS signal — move to open sky for a better reading.";
    return;
  }

  if (gpsLastFix) {
    const segKm = haversineKm(gpsLastFix.lat, gpsLastFix.lon, lat, lon);
    const dtHours = (t - gpsLastFix.t) / 3_600_000;
    const impliedKmh = dtHours > 0 ? segKm / dtHours : 0;
    if (impliedKmh > GPS_MAX_PLAUSIBLE_KMH) {
      // A physically impossible jump -- GPS noise, not real movement.
      // Keep this fix as the new reference point without adding its
      // distance, so a genuine one-off glitch doesn't poison every
      // subsequent segment's speed calculation too.
      gpsLastFix = {lat, lon, t};
      return;
    }
    gpsDistanceKm += segKm;
    gpsPolyline.addLatLng([lat, lon]);
    gpsMap.panTo([lat, lon]);
  } else {
    gpsPolyline.addLatLng([lat, lon]);
  }
  gpsLastFix = {lat, lon, t};
  $("gps-signal-note").textContent = "";
  $("gps-distance").textContent = gpsDistanceKm.toFixed(2);
  updateGpsPace();
}

function updateGpsClock() {
  const elapsed = Math.floor((Date.now() - gpsStartedAt.getTime()) / 1000);
  const m = String(Math.floor(elapsed / 60)).padStart(2, "0");
  const s = String(elapsed % 60).padStart(2, "0");
  $("gps-duration").textContent = `${m}:${s}`;
  updateGpsPace();
}

function updateGpsPace() {
  if (gpsDistanceKm < 0.05) { $("gps-pace").textContent = "--"; return; }
  const elapsedMin = (Date.now() - gpsStartedAt.getTime()) / 60000;
  const paceMinPerKm = elapsedMin / gpsDistanceKm;
  const m = Math.floor(paceMinPerKm);
  const s = String(Math.round((paceMinPerKm - m) * 60)).padStart(2, "0");
  $("gps-pace").textContent = `${m}:${s}`;
}

async function finishGpsTracking() {
  if (gpsWatchId !== null) {
    navigator.geolocation.clearWatch(gpsWatchId);
    gpsWatchId = null;
  }
  if (gpsClockHandle) { clearInterval(gpsClockHandle); gpsClockHandle = null; }

  if (gpsPlayerLinked) {
    // When linked to Workout Player: NO automatic completion!
    // Bring the exact real GPS distance back to the player card so the user
    // can review it, do extra if they want, and explicitly click "Mark Done"!
    playerActualAmount = Math.round(gpsDistanceKm * 100) / 100;
    go("player");
    renderPlayerExercise();
    toast(`GPS recorded ${playerActualAmount.toFixed(2)} km. Review and tap Mark Done to save.`);
    return;
  }

  if (gpsDistanceKm <= 0) {
    // Standalone GPS tracking on desktop/indoor: let the user enter a
    // manual distance via the popup instead of a native prompt().
    const actLabel = gpsActivityType === "running" ? "run" : "walk";
    $("gm-message").textContent = `GPS recorded 0.00 km. Enter the distance to log this ${actLabel}, or discard it.`;
    $("gm-distance").value = "1.0";
    $("gps-manual-modal").classList.remove("hidden");
    $("gps-manual-modal").classList.add("flex");
    return;
  }
  await saveGpsActivity();
}

async function discardGpsManualDistance() {
  $("gps-manual-modal").classList.add("hidden");
  $("gps-manual-modal").classList.remove("flex");
  if (gpsSessionId) {
    await api("POST", `/v1/workout-sessions/${gpsSessionId}/finish`, {}).catch(() => {});
  }
  toast("Stopped — activity discarded.");
  go("home");
}

async function submitGpsManualDistance() {
  const parsed = parseFloat($("gm-distance").value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    toast("Enter a distance greater than zero.", true);
    return;
  }
  gpsDistanceKm = parsed;
  $("gps-manual-modal").classList.add("hidden");
  $("gps-manual-modal").classList.remove("flex");
  await saveGpsActivity();
}

async function saveGpsActivity() {
  try {
    const d = await api("POST", "/v1/activities", {
      entries: [{
        activity_type: gpsActivityType, amount: Math.round(gpsDistanceKm * 100) / 100,
        sets: 1, unit: "km", source: "gps", workout_session_id: gpsSessionId,
      }],
    });
    const anyImplausible = d.logged.some(l => l.implausible);
    toast(anyImplausible
      ? "Saved — that distance looks unusual, so it won't count towards progress"
      : `Saved · ${gpsDistanceKm.toFixed(2)} km logged`, anyImplausible);
    await api("POST", `/v1/workout-sessions/${gpsSessionId}/finish`, {});
  } catch (e) {
    toast(e.message, true);
  }
  go("home");
  loadHome();
}

// ── manual program control (14.7): add from the catalogue, remove any entry ──
async function openCatalogue() {
  // Referred users (13.3b) are hard-blocked from the built-in catalogue
  // server-side (403) - manual logging must still work for them, so this
  // section fails gracefully into a message instead of blocking the modal.
  let catalogue = [];
  let referredBlocked = false;
  try {
    ({ catalogue } = await api("GET", "/v1/program/catalogue"));
  } catch (e) {
    if (e.status === 403) {
      referredBlocked = true;
    } else {
      toast(e.message || "Couldn't load the exercise catalogue. Please try again.", true);
      return;
    }
  }
  try {
    await loadCustomExerciseSection();
  } catch (e) {
    toast(e.message || "Couldn't load your custom exercises. Please try again.", true);
    return;
  }
  $("catalogue-list").innerHTML = referredBlocked
    ? `<p class="font-body-md text-on-surface-variant text-center py-lg">Speak to a doctor before adding built-in exercises to your program. You can still add your own below.</p>`
    : catalogue.length ? catalogue.map(c => `
    <div class="bg-surface-container-low rounded-DEFAULT p-md flex items-center gap-sm">
      <div class="w-10 h-10 rounded-full bg-surface-variant flex items-center justify-center text-secondary shrink-0">
        <span class="material-symbols-outlined">${icon(c.activity_type)}</span></div>
      <div class="flex-1 min-w-0">
        <h4 class="font-label-bold text-label-bold text-on-surface">${c.display_name}</h4>
        ${c.caution ? `<p class="font-label-sm text-label-sm ${c.verdict === 'avoid' ? 'text-error' : 'text-on-surface-variant'}">${c.verdict === 'avoid' ? '⚠ ' : ''}${c.caution}</p>` : ""}
      </div>
      <button onclick="openCatalogueAddEditor('${c.activity_type}')"
        class="shrink-0 ${c.verdict === 'avoid' ? 'bg-error-container text-on-error-container' : 'bg-primary text-on-primary'} px-md py-xs rounded-full font-label-bold text-label-bold hover:opacity-90">
        ${c.verdict === 'avoid' ? 'Blocked — add anyway' : 'Add'}</button>
    </div>`).join("")
    : `<p class="font-body-md text-on-surface-variant text-center py-lg">Everything is already on your dashboard.</p>`;
  CATALOGUE_CACHE = catalogue;
  const m = $("catalogue-modal");
  m.classList.remove("hidden");
  m.classList.add("flex");
}

function closeCatalogue() {
  const m = $("catalogue-modal");
  m.classList.add("hidden");
  m.classList.remove("flex");
}

let CATALOGUE_CACHE = [];
let CA_CURRENT_ACTIVITY = null;

// Shows editable defaults before adding, and the same fixed-vs-auto-upgrade
// choice a manager already gets for someone else's plan — a self-service
// user gets it for their own now too, instead of silently always getting
// the taxonomy default locked to auto-progress with no say in it.
function openCatalogueAddEditor(activityType) {
  const c = CATALOGUE_CACHE.find(x => x.activity_type === activityType);
  if (!c) return;
  CA_CURRENT_ACTIVITY = activityType;
  const single = c.prescription_type === "distance" || c.prescription_type === "duration";
  $("ca-title").textContent = `Add ${c.display_name}`;
  $("ca-sets-wrap").classList.toggle("hidden", single);
  $("ca-sets").value = c.default_sets;
  // amount_unit is "count" for reps internally — show "Reps" to the user
  // instead of that raw unit name, same convention the sets table elsewhere
  // in this file already uses for a reps-type exercise.
  $("ca-amount-label").textContent =
    c.prescription_type === "reps" ? "Reps per set"
    : c.prescription_type === "hold" ? `Hold (${c.amount_unit}) per set`
    : `Amount (${c.amount_unit})`;
  $("ca-amount").value = c.default_amount;
  $("ca-auto").checked = true;
  $("ca-auto").disabled = false;
  $("ca-auto-wrap").classList.remove("opacity-50");
  // Different-amount-per-set only makes sense for reps/hold exercises (a
  // single distance/duration effort has no "sets" to vary) — see
  // set_own_target's docstring in app/profile/service.py.
  $("ca-per-set-toggle-wrap").classList.toggle("hidden", single);
  $("ca-per-set-toggle-label").textContent =
    c.prescription_type === "reps" ? "Different reps for each set"
    : c.prescription_type === "hold" ? "Different hold time for each set"
    : "Different amount for each set";
  $("ca-per-set-toggle").checked = false;
  $("ca-uniform-wrap").classList.remove("hidden");
  $("ca-per-set-inputs").classList.add("hidden");
  const m = $("catalogue-add-modal");
  m.classList.remove("hidden");
  m.classList.add("flex");
}

function closeCatalogueAddEditor() {
  const m = $("catalogue-add-modal");
  m.classList.add("hidden");
  m.classList.remove("flex");
}

// Per-set mode keeps its own row count independent of the (hidden, while
// this is on) uniform "Sets" field — the number of sets IS what's being
// edited here, via Add/Remove Set rather than a separate numeric input.
let CA_SET_VALUES = [];

function toggleCaPerSet() {
  const on = $("ca-per-set-toggle").checked;
  $("ca-uniform-wrap").classList.toggle("hidden", on);
  $("ca-per-set-inputs").classList.toggle("hidden", !on);
  const c = CATALOGUE_CACHE.find(x => x.activity_type === CA_CURRENT_ACTIVITY);
  if (on) {
    const n = Math.max(1, parseInt($("ca-sets").value, 10) || c.default_sets);
    CA_SET_VALUES = Array.from({length: n}, () => c.default_amount);
    renderCaPerSetInputs();
  }
}

// Reads whatever's currently typed in the DOM into CA_SET_VALUES, without
// re-rendering. Must run BEFORE a splice/push, never after — reading the
// (still old-length) DOM post-splice would write a stale value straight
// back into the index that was just removed, undoing the removal.
function flushCaSetInputs() {
  const current = $("ca-per-set-inputs").querySelectorAll("input");
  current.forEach((el, idx) => { CA_SET_VALUES[idx] = parseFloat(el.value) || CA_SET_VALUES[idx]; });
}

function renderCaPerSetInputs() {
  $("ca-per-set-inputs").innerHTML = CA_SET_VALUES.map((v, idx) => `
    <div class="flex items-center justify-between gap-sm">
      <span class="font-label-bold text-label-bold text-on-surface w-14 shrink-0">Set ${idx + 1}</span>
      <input type="number" step="any" value="${v}" onchange="updateCaSetValue(${idx}, this.value)"
        class="flex-1 bg-surface-container-low border-none rounded-lg px-md py-sm font-body-md text-body-md text-center">
      ${CA_SET_VALUES.length > 1 ? `
      <button onclick="removeCaSetRow(${idx})" aria-label="Remove set" class="shrink-0 text-secondary hover:text-error">
        <span class="material-symbols-outlined text-[20px]">close</span>
      </button>` : `<span class="w-5 shrink-0"></span>`}
    </div>`).join("")
    + `<button onclick="addCaSetRow()" class="flex items-center justify-center gap-1 text-primary font-label-bold text-label-bold py-xs">
         <span class="material-symbols-outlined text-[18px]">add</span> Add Set
       </button>`;
}

function updateCaSetValue(idx, value) {
  CA_SET_VALUES[idx] = parseFloat(value) || 0;
}

function addCaSetRow() {
  flushCaSetInputs();
  CA_SET_VALUES.push(CA_SET_VALUES[CA_SET_VALUES.length - 1]);
  renderCaPerSetInputs();
}

function removeCaSetRow(idx) {
  flushCaSetInputs();
  CA_SET_VALUES.splice(idx, 1);
  renderCaPerSetInputs();
}

async function submitCatalogueAdd() {
  const c = CATALOGUE_CACHE.find(x => x.activity_type === CA_CURRENT_ACTIVITY);
  if (!c) return;
  const single = c.prescription_type === "distance" || c.prescription_type === "duration";
  const perSet = !single && $("ca-per-set-toggle").checked;

  let sets, amount, setAmounts;
  if (perSet) {
    flushCaSetInputs(); // pick up any just-typed value still sitting in the DOM
    setAmounts = CA_SET_VALUES.slice();
    if (!setAmounts.length || setAmounts.some(v => !v || v <= 0)) {
      toast("Enter an amount greater than zero for every set.", true);
      return;
    }
  } else {
    sets = single ? 1 : (parseInt($("ca-sets").value, 10) || c.default_sets);
    amount = parseFloat($("ca-amount").value);
    if (!amount || amount <= 0) { toast("Enter an amount greater than zero.", true); return; }
  }
  await addFromCatalogue(CA_CURRENT_ACTIVITY, false, sets, amount, $("ca-auto").checked, setAmounts);
}

async function addFromCatalogue(activityType, overrideConfirmed, sets, amount, autoProgress, setAmounts) {
  try {
    const d = await api("POST", "/v1/program/entries", {
      activity_type: activityType, override_confirmed: !!overrideConfirmed,
      auto_progress: autoProgress !== false,
    });
    if (d.needs_confirmation) {
      const ok = await appConfirm({
        message: d.warning_text + "\n\nOnly continue if a doctor has advised you to do this. " +
          "Keep blocked leaves it out of your plan.",
        confirmText: "Add anyway", cancelText: "Keep blocked", danger: true,
      });
      if (ok) await addFromCatalogue(activityType, true, sets, amount, autoProgress, setAmounts);
      return;
    }
    if (setAmounts !== undefined) {
      await api("PUT", `/v1/program/entries/${activityType}/target`, { set_amounts: setAmounts });
    } else if (sets !== undefined && amount !== undefined) {
      await api("PUT", `/v1/program/entries/${activityType}/target`,
        { target_sets: sets, target_amount: amount });
    }
    closeCatalogueAddEditor();
    closeCatalogue();
    toast("Added to your plan");
    await refreshPlayerItems();
  } catch (e) { toast(e.message, true); }
}

// ── edit conditions (first-layer overlay, opened from Profile) ──
let editPicked = new Set();
let editProfile = null;

let editGoal = "health", editBand = "beginner", editConditions = [];

async function openProfileEditor() {
  const [p, c] = await Promise.all([api("GET", "/v1/profile"), api("GET", "/v1/conditions")]);
  editProfile = p;
  editGoal = p.goal;
  editBand = p.experience_band;
  editConditions = c.conditions;
  editPicked = new Set(p.conditions.map(x => x.code));
  if (!editPicked.size) editPicked.add("none");
  $("pm-age").value = p.age;
  renderProfileEditor();
  $("cm-err").className = "hidden";
  const m = $("cond-modal");
  m.classList.remove("hidden");
  m.classList.add("flex");
}

function renderProfileEditor() {
  const g = $("pm-goals"); g.innerHTML = "";
  GOALS.forEach(([v,l]) => g.appendChild(chip(l, editGoal===v, () => {editGoal=v; renderProfileEditor();})));
  const bb = $("pm-bands"); bb.innerHTML = "";
  BANDS.forEach(([v,l]) => bb.appendChild(chip(l, editBand===v, () => {editBand=v; renderProfileEditor();})));
  const list = $("cm-list");
  list.innerHTML = "";
  editConditions.forEach(cd => {
    const refer = cd.bucket === "refer";
    list.appendChild(chip(cd.name + (refer ? " ⚠" : ""), editPicked.has(cd.code), () => {
      if (cd.code === "none") { editPicked.clear(); editPicked.add("none"); }
      else { editPicked.delete("none"); editPicked.has(cd.code) ? editPicked.delete(cd.code) : editPicked.add(cd.code); }
      renderProfileEditor();
    }, refer));
  });
}

async function saveProfileEditor() {
  try {
    const newAge = parseInt($("pm-age").value, 10);
    if (Number.isNaN(newAge)) { toast("Enter your age", true); return; }
    const originalCodes = new Set(editProfile.conditions.map(x => x.code));
    // editPicked is normalized to {"none"} when empty (see openProfileEditor) —
    // originalCodes must be too, or a zero-condition user always looks like
    // they "changed" conditions (1 item vs. 0) just by editing age/goal, which
    // wrongly routes every trivial edit through the full onboarding rebuild
    // instead of the lightweight PUT, and can resurrect a manually-removed
    // program entry in the process.
    if (!originalCodes.size) originalCodes.add("none");
    const conditionsChanged = editPicked.size !== originalCodes.size
      || [...editPicked].some(c => !originalCodes.has(c));

    // Changing the experience level rescales the plan on the server. Moving DOWN
    // comes back with needs_confirmation first (nothing saved yet); on OK it is
    // sent again with confirm_rescale. An increase applies straight away.
    const submit = async (confirmRescale) => {
      if (conditionsChanged) {
        // Condition changes need the full safety-grid re-check (14.4), so this
        // one case still goes through onboarding rather than the quick-edit path.
        return api("POST", "/v1/profile/onboarding", {
          age: newAge, goal: editGoal, experience_band: editBand,
          unit_preference: editProfile.unit_preference, condition_codes: [...editPicked],
          ...(confirmRescale ? { confirm_rescale: true } : {}),
        });
      }
      // No condition change — use the lightweight edit (3.4), only send what moved.
      const changed = {};
      if (newAge !== editProfile.age) changed.age = newAge;
      if (editGoal !== editProfile.goal) changed.goal = editGoal;
      if (editBand !== editProfile.experience_band) changed.experience_band = editBand;
      if (!Object.keys(changed).length) return {};
      if (confirmRescale) changed.confirm_rescale = true;
      return api("PUT", "/v1/profile", changed);
    };
    let resp = await submit(false);
    if (resp.needs_confirmation) {
      // stay on the editor if they decline; nothing was saved
      if (!(await appConfirm({ message: resp.message, confirmText: "Continue" }))) return;
      resp = await submit(true);
    }
    // The PUT's reply is a profile summary, not an onboarding result — only the
    // onboarding path can mean "referred".
    const d = conditionsChanged ? resp : {};
    const rescaled = resp.plan_rescaled || [];
    closeProfileEditor();
    if (d.referred) {
      $("refer-msg").textContent = d.message;
      go("refer");
      $("nav").className = navOnClass();
      return;
    }
    await loadProfile();
    toast(rescaled.length
      ? `Profile updated · ${rescaled.length} exercise${rescaled.length === 1 ? "" : "s"} adjusted for your new level`
      : "Profile updated");
  } catch (e) {
    $("cm-err").textContent = e.message;
    $("cm-err").className = "font-body-md text-on-error-container bg-error-container rounded px-sm py-xs";
  }
}

function closeProfileEditor() {
  const m = $("cond-modal");
  m.classList.add("hidden");
  m.classList.remove("flex");
}

// ── change password (first block, name card) ──
let pwForgotMode = false;

function openPasswordEditor() {
  $("pw-current").value = "";
  $("pw-new").value = "";
  if ($("pw-confirm")) $("pw-confirm").value = "";
  $("pw-reset-code").value = "";
  $("pw-err").className = "hidden";
  pwForgotMode = false;
  $("pw-current-wrap").classList.remove("hidden");
  $("pw-reset-wrap").classList.add("hidden");
  $("pw-forgot-link").textContent = "Forgot your password?";
  checkPasswordRules("pw-new", "pw-confirm", "p");
  const m = $("pw-modal");
  m.classList.remove("hidden");
  m.classList.add("flex");
}

function closePasswordEditor() {
  const m = $("pw-modal");
  m.classList.add("hidden");
  m.classList.remove("flex");
}

function toggleForgotPassword() {
  closePasswordEditor();
  openForgotPasswordModal();
}

async function savePassword() {
  const newPass = $("pw-new")?.value || "";
  const confPass = $("pw-confirm")?.value || "";

  if (!pwForgotMode && newPass !== confPass) {
    $("pw-err").textContent = "New passwords do not match. Please re-enter.";
    $("pw-err").className = "font-body-md text-on-error-container bg-error-container rounded px-sm py-xs text-xs";
    return;
  }

  const rules = checkPasswordRules("pw-new", "pw-confirm", "p");
  if (!rules.length || !rules.upper || !rules.lower || !rules.number || !rules.special) {
    $("pw-err").textContent = "Password must have at least 1 capital letter, 1 small letter, 1 number, 1 special character, and be at least 10 characters long.";
    $("pw-err").className = "font-body-md text-on-error-container bg-error-container rounded px-sm py-xs text-xs";
    return;
  }

  try {
    if (pwForgotMode) {
      await api("POST", "/v1/auth/password-reset/confirm", {
        token: $("pw-reset-code").value.trim(),
        password: newPass,
      });
    } else {
      await api("POST", "/v1/auth/change-password", {
        current_password: $("pw-current").value,
        new_password: newPass,
      });
    }
    closePasswordEditor();
    // Either path revokes every session server-side (1.9 / this device's own
    // change) - the current session is dead now too, so return to login.
    localStorage.clear();
    AT = null; RT = null;
    setAuthMode("login");
    go("auth");
    toast("Password updated — please log in again");
  } catch (e) {
    $("pw-err").textContent = e.message;
    $("pw-err").className = "font-body-md text-on-error-container bg-error-container rounded px-sm py-xs text-xs";
  }
}

// ── exercise demo modal (first-layer overlay, opened from a dashboard card) ──
let dmFrameTimer = null;

function openDemo(activityType) {
  const i = HOME_ITEMS.find(x => x.activity_type === activityType);
  if (!i) return;
  $("dm-title").textContent = i.display_name;
  $("dm-steps").innerHTML = (i.instructions || []).map(s => `<li>${s}</li>`).join("");

  clearInterval(dmFrameTimer);
  if (i.demo_gif) {
    $("dm-img").src = assetUrl(i.demo_gif);
  } else if (i.demo_images && i.demo_images.length) {
    const frames = i.demo_images.map(assetUrl);
    let n = 0;
    $("dm-img").src = frames[0];
    dmFrameTimer = setInterval(() => {
      n = 1 - n;
      $("dm-img").src = frames[n % frames.length];
    }, 900);
  }

  const m = $("demo-modal");
  m.classList.remove("hidden");
  m.classList.add("flex");
}

function closeDemo() {
  clearInterval(dmFrameTimer);
  dmFrameTimer = null;
  const m = $("demo-modal");
  m.classList.add("hidden");
  m.classList.remove("flex");
  $("dm-img").src = "";
}

// ── videos ──
async function loadVideos() {
  // YouTube-curated Video Library removed for now (product decision) — see
  // index.html's s-videos screen. Browse Exercises (ExerciseDB) is the only
  // thing this screen shows today.
  loadExerciseBrowse();
}

// ── exercise browse (ExerciseDB reference library, separate from the plan's
// curated video_library above) ──
let EXERCISE_BROWSE_CACHE = [];
async function loadExerciseBrowse() {
  const d = await api("GET", "/v1/exercise-library");
  EXERCISE_BROWSE_CACHE = d.exercises || [];
  renderExerciseBrowse();
}

function renderExerciseBrowse() {
  const q = ($("vb-search")?.value || "").trim().toLowerCase();
  const items = q
    ? EXERCISE_BROWSE_CACHE.filter(e =>
        e.name.toLowerCase().includes(q) ||
        (e.body_part || "").toLowerCase().includes(q) ||
        (e.target || "").toLowerCase().includes(q) ||
        (e.equipment || "").toLowerCase().includes(q))
    : EXERCISE_BROWSE_CACHE;
  $("vb-grid").innerHTML = items.slice(0, 60).map(e => `
    <article class="bg-surface-container-lowest rounded-lg p-3 ambient-shadow flex flex-col gap-sm">
      <div class="relative w-full aspect-video rounded-[14px] overflow-hidden bg-surface-container-low">
        <img src="${assetUrl(e.gif_url)}" alt="${e.name}" class="w-full h-full object-contain" loading="lazy">
      </div>
      <div class="px-1 flex flex-col gap-2">
        <div class="flex gap-2 flex-wrap">
          ${e.body_part ? `<span class="px-2 py-1 bg-primary-fixed/20 text-on-primary-fixed-variant font-label-sm text-label-sm rounded-full">${e.body_part}</span>` : ""}
          ${e.target ? `<span class="px-2 py-1 bg-surface-variant text-on-surface-variant font-label-sm text-label-sm rounded-full">${e.target}</span>` : ""}
          ${e.equipment ? `<span class="px-2 py-1 bg-surface-variant text-on-surface-variant font-label-sm text-label-sm rounded-full">${e.equipment}</span>` : ""}
        </div>
        <h3 class="font-headline-sm text-headline-sm">${e.name}</h3>
        ${e.instructions && e.instructions.length ? `
          <ol class="font-body-md text-body-md text-on-surface-variant list-decimal pl-5 space-y-1">
            ${e.instructions.map(i => `<li>${i}</li>`).join("")}
          </ol>` : ""}
      </div>
    </article>`).join("") || `<p class="font-body-md text-on-surface-variant">No matches.</p>`;
}

let demoTimer = null;
function startDemoLoop(videos) {
  if (demoTimer) clearInterval(demoTimer);
  const framed = videos.filter(v => !v.demo_gif && v.demo_images && v.demo_images.length > 1);
  if (!framed.length) return;
  let i = 0;
  demoTimer = setInterval(() => {
    if (document.hidden) return;
    i = 1 - i;
    framed.forEach(v => {
      const el = $("demo-" + v.activity_type);
      if (el) el.src = assetUrl(v.demo_images[i % v.demo_images.length]);
    });
  }, 900);
}

// ── chat (Module 5) ──
let CHAT_INITED = false;

async function loadChat() {
  let q;
  try {
    q = await api("GET", "/v1/rehab/quota");
  } catch (e) {
    toast(e.message, true);
    return;
  }
  renderQuota(q);

  if (q.referred) {
    $("c-referred-msg").textContent = q.referral_message;
    $("c-referred").className = "mt-lg bg-surface-container-lowest ambient-shadow rounded-lg p-lg";
    $("c-composer").className = "hidden";
  } else {
    $("c-referred").className = "hidden";
    $("c-composer").className = "fixed bottom-[70px] left-0 right-0 z-40 bg-transparent border-t border-outline-variant/20";
  }

  // One continuous conversation per account, not per login: load it once per
  // page load, not on every re-visit to this tab (which would just duplicate
  // every bubble already on screen from the first load or from this session).
  if (!CHAT_INITED) {
    try {
      const h = await api("GET", "/v1/rehab/history");
      for (const turn of h.history) {
        appendChatBubble(turn.question, true);
        appendChatBubble(turn.answer, false);
      }
    } catch (e) { toast(e.message, true); }
  }

  CHAT_INITED = true;
}

function renderQuota(q) {
  $("c-quota-text").textContent = `${q.remaining}/${q.limit} questions`;
}

// Minimal, escape-first markdown-lite formatter for bot replies — the LLM's
// output uses **bold** headers, "- " bullet lists, and blank-line paragraph
// breaks, none of which survive plain textContent (HTML collapses newlines).
// Escaping BEFORE any tag-generation means nothing in the model's own text can
// ever inject real markup, no matter what it contains. Uses the shared
// escapeHtml() defined near the top of this script (not a separate copy) —
// two same-named function declarations would otherwise silently collide,
// with whichever one is textually last in the file winning everywhere.
function formatBotText(text) {
  const bold = escapeHtml(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  return bold.split(/\n\s*\n/).map(formatBotBlock).join("");
}

function formatBotBlock(block) {
  const lines = block.split("\n").map(l => l.trim()).filter(Boolean);
  if (!lines.length) return "";
  const bulletIdx = lines.findIndex(l => /^[-*]\s+/.test(l));
  if (bulletIdx === -1) return `<p class="mb-2">${lines.join("<br>")}</p>`;

  const before = lines.slice(0, bulletIdx);
  const bulletLines = lines.slice(bulletIdx).filter(l => /^[-*]\s+/.test(l));
  const after = lines.slice(bulletIdx + bulletLines.length).filter(l => !/^[-*]\s+/.test(l));

  let html = "";
  if (before.length) html += `<p class="mb-1">${before.join("<br>")}</p>`;
  html += `<ul class="list-disc pl-5 space-y-1 mb-2">${
    bulletLines.map(l => `<li>${l.replace(/^[-*]\s+/, "")}</li>`).join("")}</ul>`;
  if (after.length) html += `<p class="mb-2">${after.join("<br>")}</p>`;
  return html;
}

// Honest status text, not fake progress theater — the backend genuinely does
// retrieve reviewed sources, then prepare the safety-constrained prompt, then
// generate through the LLM (the slow part — free-tier generation alone can
// take 30-90s+), then finish up (the safety keyword check + saving memory).
// Each message fires ONCE, in order, then holds — it must never cycle back to
// "Looking up..." after reaching the end, which reads as broken/stuck on a
// slow request. The answer itself is never streamed (see the safety
// backstop's own comment: it has to see the WHOLE answer before deciding
// whether to show it, so partial-reveal would let a leaked medication/surgery
// name slip onto the screen before the check catches it).
const THINKING_SCHEDULE = [
  {text: "Looking up reviewed medical sources…", at: 0},
  {text: "Checking your safety guidelines…", at: 2000},
  {text: "Generating your answer…", at: 5000},
  {text: "Finishing up…", at: 25000},
];

function showThinkingBubble() {
  const wrap = document.createElement("div");
  wrap.className = "self-start max-w-[90%]";
  wrap.innerHTML = `
    <div class="flex items-end gap-xs mb-1">
      <div class="w-6 h-6 rounded-full bg-primary flex items-center justify-center">
        <span class="material-symbols-outlined text-on-primary text-[14px]" style="font-variation-settings:'FILL' 1">smart_toy</span>
      </div>
      <span class="font-label-sm text-label-sm text-on-surface-variant">Coach AI</span>
    </div>
    <div class="bg-surface-container-lowest ambient-shadow rounded-2xl rounded-tl-sm p-md flex items-center gap-sm">
      <span class="flex gap-1 shrink-0">
        <span class="w-1.5 h-1.5 rounded-full bg-on-surface-variant animate-bounce" style="animation-delay:0ms"></span>
        <span class="w-1.5 h-1.5 rounded-full bg-on-surface-variant animate-bounce" style="animation-delay:150ms"></span>
        <span class="w-1.5 h-1.5 rounded-full bg-on-surface-variant animate-bounce" style="animation-delay:300ms"></span>
      </span>
      <span class="thinking-text font-body-md text-body-md text-on-surface-variant"></span>
    </div>`;
  $("c-thread").appendChild(wrap);
  wrap.scrollIntoView({behavior: "smooth", block: "end"});

  const textEl = wrap.querySelector(".thinking-text");
  textEl.textContent = THINKING_SCHEDULE[0].text;
  const timers = THINKING_SCHEDULE.slice(1).map(step =>
    setTimeout(() => { textEl.textContent = step.text; }, step.at)
  );

  return {wrap, timers};
}

function removeThinkingBubble(handle) {
  if (!handle) return;
  handle.timers.forEach(clearTimeout);
  handle.wrap.remove();
}

function appendChatBubble(text, fromUser) {
  const wrap = document.createElement("div");
  wrap.className = fromUser ? "self-end max-w-[85%]" : "self-start max-w-[90%]";
  wrap.innerHTML = fromUser
    ? `<div class="bg-primary-container text-on-primary-container rounded-2xl rounded-tr-sm px-md py-sm">
         <p class="font-body-md text-body-md"></p></div>`
    : `<div class="flex items-end gap-xs mb-1">
         <div class="w-6 h-6 rounded-full bg-primary flex items-center justify-center">
           <span class="material-symbols-outlined text-on-primary text-[14px]" style="font-variation-settings:'FILL' 1">smart_toy</span>
         </div>
         <span class="font-label-sm text-label-sm text-on-surface-variant">Coach AI</span>
       </div>
       <div class="bg-surface-container-lowest ambient-shadow rounded-2xl rounded-tl-sm p-md">
         <div class="chat-content font-body-md text-body-md text-on-surface-variant"></div>
       </div>`;
  if (fromUser) {
    wrap.querySelector("p").textContent = text;
  } else {
    wrap.querySelector(".chat-content").innerHTML = formatBotText(text);
  }
  $("c-thread").appendChild(wrap);
  wrap.scrollIntoView({behavior: "smooth", block: "end"});
}

async function sendChat() {
  const input = $("c-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  $("c-send").disabled = true;
  appendChatBubble(question, true);
  await runQuery(question, null);
  $("c-send").disabled = false;
}

// confirmation is null for a fresh message, or {code: "..."|null} when the
// user just tapped a condition-choice chip (5.6) — null code means "decline,
// answer anyway without adding a condition."
async function runQuery(question, confirmation) {
  const thinking = showThinkingBubble();
  try {
    const body = confirmation !== null
      ? {question, condition_confirmation: confirmation}
      : {question};
    const d = await api("POST", "/v1/rehab/query", body);
    removeThinkingBubble(thinking);
    renderChatResponse(d);
  } catch (e) {
    removeThinkingBubble(thinking);
    if (e.status === 429) {
      // Two distinct 429s: real quota exhaustion ("come back tomorrow") vs the
      // LLM provider being temporarily rate-limited ("try again in a moment,
      // not counted against you"). The server already picks the right message
      // for which one happened — show that, not a guess.
      appendChatBubble(e.message, false);
      // The error body carries no quota field, so re-fetch it directly rather
      // than leave the badge showing a stale count until the next page load.
      try { renderQuota(await api("GET", "/v1/rehab/quota")); } catch (_) {}
    }
  }
}

function renderChatResponse(d) {

  if (d.quota) renderQuota(d.quota);

  if (d.referred) {
    appendChatBubble(d.message, false);
    $("c-referred-msg").textContent = d.message;
    $("c-referred").className = "mt-lg bg-surface-container-lowest ambient-shadow rounded-lg p-lg";
    $("c-composer").className = "hidden";
    return;
  }

  if (d.needs_condition_selection) {
    appendChatBubble(d.condition_options.prompt_text, false);
    appendConditionChoices(d.condition_options);
    return;
  }

  appendChatBubble(d.answer, false);

  if (d.needs_program_update_confirmation && d.suggested_activities?.length) {
    appendProgramSuggestionChip(d.suggested_activities);
  }
  if (d.needs_revert_confirmation && d.revert_candidates?.length) {
    appendRevertChip(d.revert_candidates);
  }
}

// Found live (user request): after the chatbot names specific approved
// exercises, offer a one-tap way to actually add them to the program,
// instead of the user having to go find and add each one manually on the
// dashboard. Confirming replaces the program's active set with exactly
// these activities — anything currently active that ISN'T in this list gets
// hidden (never deleted, see service.py's set_program_to_activities), so
// switching focus to a new sport can't destroy progress on something the
// user might want back later.
function appendProgramSuggestionChip(activities) {
  const wrap = document.createElement("div");
  wrap.className = "self-start max-w-[90%] mt-1";

  const names = activities.map(a => a.display_name).join(", ");
  const btn = document.createElement("button");
  // Honest about the consequence, not just "add": confirming this REPLACES
  // the active program with exactly this list — a deliberate "switch focus"
  // reset (see set_program_to_activities), not a pure addition. The old
  // "+ Add these to my program" wording promised something this button
  // never actually did, so a user confirming it could lose exercises from
  // their dashboard with no warning.
  btn.textContent = `↻ Switch my program to: ${names} (other exercises will be paused, not deleted)`;
  btn.className = "px-md py-sm rounded-2xl font-label-sm text-label-sm text-left border border-primary-fixed bg-primary-fixed/10 text-on-primary-fixed-variant active:scale-95 transition-transform disabled:opacity-40";

  btn.onclick = async () => {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "Updating your program…";
    try {
      const activity_types = activities.map(a => a.activity_type);
      const result = await api("POST", "/v1/rehab/query", {
        program_confirmation: {activity_types, confirmed: true},
      });
      appendChatBubble(result.message, false);
      wrap.remove();
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
      btn.textContent = originalText;
    }
  };

  wrap.appendChild(btn);
  $("c-thread").appendChild(wrap);
  wrap.scrollIntoView({behavior: "smooth", block: "end"});
}

// Found live (user request): "revert what I did previously" via the chat —
// the chatbot looks up what a PAST sport-suggestion confirmation actually
// hid (never a manual removal, see service.py's _hidden_by_sport_focus_change)
// and offers a one-tap way to bring it back, same button pattern as
// appendProgramSuggestionChip above.
function appendRevertChip(candidates) {
  const wrap = document.createElement("div");
  wrap.className = "self-start max-w-[90%] mt-1";

  const names = candidates.map(a => a.display_name).join(", ");
  const btn = document.createElement("button");
  btn.textContent = `↺ Bring these back: ${names}`;
  btn.className = "px-md py-sm rounded-2xl font-label-sm text-label-sm text-left border border-primary-fixed bg-primary-fixed/10 text-on-primary-fixed-variant active:scale-95 transition-transform disabled:opacity-40";

  btn.onclick = async () => {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "Restoring…";
    try {
      const activity_types = candidates.map(a => a.activity_type);
      const result = await api("POST", "/v1/rehab/query", {
        revert_confirmation: {activity_types, confirmed: true},
      });
      appendChatBubble(result.message, false);
      wrap.remove();
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
      btn.textContent = originalText;
    }
  };

  wrap.appendChild(btn);
  $("c-thread").appendChild(wrap);
  wrap.scrollIntoView({behavior: "smooth", block: "end"});
}

// 5.6 — the model only ever suggests a condition match; recording it always
// requires this explicit tap, never happening automatically from the message.
function appendConditionChoices(opts) {
  const wrap = document.createElement("div");
  wrap.className = "self-start max-w-[90%] flex flex-wrap gap-xs mt-1";

  const makeChip = (label, code) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = "px-md py-sm rounded-full font-label-sm text-label-sm border border-primary-fixed bg-primary-fixed/10 text-on-primary-fixed-variant active:scale-95 transition-transform disabled:opacity-40";
    b.onclick = async () => {
      wrap.querySelectorAll("button").forEach(x => x.disabled = true);
      await runQuery(opts.original_question, {code});
    };
    return b;
  };

  wrap.appendChild(makeChip(opts.matched_label, opts.matched_code));
  wrap.appendChild(makeChip("Something else / not sure", null));
  $("c-thread").appendChild(wrap);
  wrap.scrollIntoView({behavior: "smooth", block: "end"});
}

// ── profile ──
async function loadProfile() {
  const p = await api("GET", "/v1/profile");
  $("p-body").innerHTML = `
    <!-- Profile card — matches Settings.html's own card: avatar, name, a
         stats badge row, Edit action. No "Verified"/"PRO" badges (no such
         concepts in this app) and no weight badge (not a tracked metric) —
         real data only, styled in the mockup's language. -->
    <section class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg flex flex-col gap-md">
      <div class="flex items-center gap-md">
        <div class="w-20 h-20 rounded-full bg-primary-container border-2 border-primary-fixed flex items-center justify-center font-display-lg-mobile text-on-primary-container shrink-0 shadow-sm">${escapeHtml((EMAIL[0]||"?").toUpperCase())}</div>
        <div class="min-w-0 flex-1">
          <h2 class="font-headline-sm text-headline-sm text-on-background truncate">${escapeHtml(EMAIL.split("@")[0])}</h2>
          <div class="flex items-center gap-1.5 mt-1">
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-bold bg-black/5 text-secondary">
              <span class="material-symbols-outlined text-[13px]">cake</span> ${p.age} yrs
            </span>
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-bold bg-black/5 text-secondary capitalize">
              <span class="material-symbols-outlined text-[13px]">flag</span> ${escapeHtml(p.goal)}
            </span>
          </div>
        </div>
      </div>
      <button onclick="openProfileEditor()" class="flex items-center justify-center gap-xs font-label-bold text-label-bold text-on-background bg-black/5 hover:bg-black/10 rounded-full py-sm transition-colors">
        <span class="material-symbols-outlined text-[18px]">edit</span> Edit Profile
      </button>
    </section>

    <!-- Health Conditions — real safety-grid data, no mockup equivalent for
         this (the mockup has no medical-condition concept at all), kept as
         its own card in the same visual language. -->
    <section class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg flex flex-col gap-sm">
      <div class="flex items-center gap-sm">
        <span class="material-symbols-outlined text-primary">health_and_safety</span>
        <h2 class="font-headline-sm text-headline-sm text-on-background">Health Conditions</h2>
      </div>
      <p class="font-label-sm text-label-sm text-secondary -mt-1">Experience: ${bandLabel(p.experience_band)}</p>
      ${p.conditions.map(c => `<p class="font-body-md ${c.bucket==='refer'?'text-on-error-container':'text-on-background'}">${c.name}${c.bucket==='refer'?' ⚠':''}</p>`).join("") || '<p class="font-body-md text-secondary">None</p>'}
    </section>

    <!-- App & Account — matches Settings.html's section header pattern and
         chevron rows for Change Password / Logout. -->
    <section class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg flex flex-col gap-1">
      <div class="flex items-center gap-sm mb-xs">
        <span class="text-base">⚙️</span>
        <div>
          <h2 class="font-headline-sm text-headline-sm text-on-background">App &amp; Account</h2>
          <p class="text-[11px] font-semibold text-secondary">Security settings</p>
        </div>
      </div>
      ${p.is_managed_account ? "" : `
      <button onclick="go('relationships')" class="flex items-center justify-between py-sm text-left border-t border-black/5">
        <span class="flex items-center gap-sm font-body-lg text-body-lg text-on-background"><span class="material-symbols-outlined text-[20px] text-secondary">supervisor_account</span> Manage Plans for Others</span>
        <span class="material-symbols-outlined text-secondary">chevron_right</span>
      </button>`}
      <button onclick="openActivityLogHistory()" class="flex items-center justify-between py-sm text-left border-t border-black/5">
        <span class="flex items-center gap-sm font-body-lg text-body-lg text-on-background"><span class="material-symbols-outlined text-[20px] text-secondary">history</span> Activity Log History</span>
        <span class="material-symbols-outlined text-secondary">chevron_right</span>
      </button>
      <button onclick="openTargetChangesHistory()" class="flex items-center justify-between py-sm text-left border-t border-black/5">
        <span class="flex items-center gap-sm font-body-lg text-body-lg text-on-background"><span class="material-symbols-outlined text-[20px] text-secondary">trending_up</span> Recent Target Changes</span>
        <span class="material-symbols-outlined text-secondary">chevron_right</span>
      </button>
      <button onclick="openPasswordEditor()" class="flex items-center justify-between py-sm text-left border-t border-black/5">
        <span class="flex items-center gap-sm font-body-lg text-body-lg text-on-background"><span class="material-symbols-outlined text-[20px] text-secondary">lock_reset</span> Change Password</span>
        <span class="material-symbols-outlined text-secondary">chevron_right</span>
      </button>
      ${p.is_managed_account ? "" : `
      <button onclick="doLogout()" class="flex items-center justify-between py-sm text-left border-t border-black/5">
        <span class="flex items-center gap-sm font-body-lg text-body-lg text-primary font-semibold"><span class="material-symbols-outlined text-[20px]">logout</span> Log out</span>
        <span class="material-symbols-outlined text-secondary">chevron_right</span>
      </button>`}
    </section>

    <!-- Danger Zone — centered red pill, matching the mockup exactly.
         Hidden for a managed account: only whoever created the invite link
         can delete it (server-enforced too, not just hidden here — see
         app/auth/service.py's delete_account). -->
    ${p.is_managed_account ? "" : `
    <button onclick="delAccount()" class="flex items-center justify-center gap-xs mx-auto mt-sm px-lg py-sm rounded-full text-error font-label-bold text-label-bold hover:bg-error-container/40 transition-colors">
      <span class="material-symbols-outlined text-[18px]">delete_forever</span> Delete Account
    </button>`}`;
}

async function openActivityLogHistory() {
  const { entries } = await api("GET", "/v1/dashboard/activity-detail?days=90");
  $("activity-history-list").innerHTML = entries.length
    ? entries.map(e => `
      <div class="flex items-center justify-between bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-md">
        <div>
          <p class="font-body-lg text-body-lg text-on-background">${escapeHtml(e.name)}</p>
          <p class="font-label-sm text-label-sm text-secondary">${e.date}</p>
        </div>
        <p class="font-label-bold text-label-bold text-on-background">${e.set_amounts ? e.set_amounts.join(", ") : `${e.sets} x ${e.amount}`} ${escapeHtml(e.unit)}</p>
      </div>`).join("")
    : `<p class="font-body-md text-on-surface-variant text-center py-lg">No activity logged in the last 90 days.</p>`;
  go("activity-history");
}

function getProgressionDetails(item) {
  const isUp = item.pct > 0.05;
  const isDown = item.pct < -0.05;
  const isFlat = !isUp && !isDown;
  const adh = item.adherence != null ? Math.round(item.adherence) : null;
  const absPct = Math.abs(item.pct).toFixed(1);
  const unitStr = item.unit ? ` ${item.unit}` : "";

  let reasonTitle = "";
  let reasonDesc = "";
  let badgeText = "";
  let badgeClass = "";
  let iconName = "";
  let whyBoxBg = "";
  let whyIcon = "";
  let whyIconColor = "";
  let whyTitleColor = "";
  let whyTextColor = "";

  if (isUp) {
    badgeText = `+${absPct}% Growth`;
    badgeClass = "bg-emerald-500/15 text-emerald-800 border-emerald-500/30";
    iconName = "trending_up";
    whyBoxBg = "bg-emerald-50/70 border-emerald-200/60";
    whyIcon = "trending_up";
    whyIconColor = "text-emerald-600";
    whyTitleColor = "text-emerald-900";
    whyTextColor = "text-emerald-950/80";

    if (item.reason === "double_progression") {
      reasonTitle = "Consistency Bonus Step-Up";
      reasonDesc = `Target stepped up by <strong>+${absPct}%</strong> (from ${item.old_target}${unitStr} to ${item.new_target}${unitStr}) because you consistently completed all required sets and reps.`;
    } else if (item.reason === "escalation") {
      reasonTitle = "Chat-Confirmed Target Increase";
      reasonDesc = `Target increased by <strong>+${absPct}%</strong> to ${item.new_target}${unitStr} following your confirmed goal adjustment during coaching chat.`;
    } else if (item.reason === "set_boost") {
      reasonTitle = "Workout Performance Boost";
      reasonDesc = `Target boosted by <strong>+${absPct}%</strong> to ${item.new_target}${unitStr} after an exceptional workout session.`;
    } else if (item.reason === "pro_level") {
      reasonTitle = "Experience Level Adjustment";
      reasonDesc = `Target increased by <strong>+${absPct}%</strong> to align with your updated fitness experience band.`;
    } else {
      const adhText = adh != null ? `your weekly adherence reached <strong>${adh}%</strong> (exceeding the 80% progression goal)` : "of strong and consistent weekly workout adherence";
      reasonTitle = `Target Increased (+${absPct}% Overload)`;
      reasonDesc = `Target grew by <strong>+${absPct}%</strong> (from ${item.old_target}${unitStr} to ${item.new_target}${unitStr}) because ${adhText}. Progressive overload safely advances your fitness.`;
    }
  } else if (isDown) {
    badgeText = `-${absPct}% Reduction`;
    badgeClass = "bg-rose-500/15 text-rose-800 border-rose-500/30";
    iconName = "trending_down";
    whyBoxBg = "bg-rose-50/70 border-rose-200/60";
    whyIcon = "healing";
    whyIconColor = "text-rose-600";
    whyTitleColor = "text-rose-900";
    whyTextColor = "text-rose-950/80";

    if (item.reason === "deload") {
      reasonTitle = "Scheduled Recovery Deload";
      reasonDesc = `Target temporarily reduced by <strong>-${absPct}%</strong> to ${item.new_target}${unitStr} to give muscles and connective tissue time to recover.`;
    } else if (item.reason === "floor") {
      reasonTitle = "Adjusted to Safe Baseline Minimum";
      reasonDesc = `Target was lowered by <strong>-${absPct}%</strong> to ${item.new_target}${unitStr}, reaching the safe foundational floor for this exercise.`;
    } else {
      const adhText = adh != null ? `weekly adherence was <strong>${adh}%</strong> (below the 50% target threshold)` : "recent consistency was lower than expected";
      reasonTitle = `Target Reduced (-${absPct}%) for Recovery`;
      reasonDesc = `Target was safely reduced by <strong>-${absPct}%</strong> (from ${item.old_target}${unitStr} to ${item.new_target}${unitStr}) because ${adhText}. Reducing volume prevents injury and fatigue, making workouts manageable again.`;
    }
  } else {
    badgeText = "0.0% Maintained";
    badgeClass = "bg-slate-500/10 text-slate-700 border-slate-500/20";
    iconName = "pause_circle";
    whyBoxBg = "bg-slate-50 border-slate-200/60";
    whyIcon = "balance";
    whyIconColor = "text-slate-600";
    whyTitleColor = "text-slate-900";
    whyTextColor = "text-slate-950/80";

    if (item.reason === "floor") {
      reasonTitle = "Maintained at Safe Minimum Baseline";
      reasonDesc = `Target kept steady at <strong>${item.new_target}${unitStr}</strong>. This is the safe foundational minimum — it will not decrease further to ensure you maintain core fitness benefits.`;
    } else if (item.reason === "set_created") {
      reasonTitle = "Baseline Target Established";
      reasonDesc = `Started tracking with a baseline target of <strong>${item.new_target}${unitStr}</strong>.`;
    } else {
      const adhText = adh != null ? `weekly adherence was <strong>${adh}%</strong> (in the 50%–79% maintenance range)` : "consistency is currently in the steady maintenance zone";
      reasonTitle = "Target Maintained Steady";
      reasonDesc = `Target held steady at <strong>${item.new_target}${unitStr}</strong> because ${adhText}. Maintaining steady volume builds habit stability before the next increase.`;
    }
  }

  let adherenceBadge = "";
  if (adh != null) {
    if (adh >= 80) {
      adherenceBadge = `<span class="px-1.5 py-0.5 rounded-full text-[10px] font-bold bg-emerald-100 text-emerald-800 border border-emerald-200">High (≥80%)</span>`;
    } else if (adh >= 50) {
      adherenceBadge = `<span class="px-1.5 py-0.5 rounded-full text-[10px] font-bold bg-amber-100 text-amber-800 border border-amber-200">Moderate (50-79%)</span>`;
    } else {
      adherenceBadge = `<span class="px-1.5 py-0.5 rounded-full text-[10px] font-bold bg-rose-100 text-rose-800 border border-rose-200">Low (&lt;50%)</span>`;
    }
  }

  return {
    reasonTitle,
    reasonDesc,
    badgeText,
    badgeClass,
    iconName,
    whyBoxBg,
    whyIcon,
    whyIconColor,
    whyTitleColor,
    whyTextColor,
    adherenceBadge,
    isUp,
    isDown,
    isFlat,
  };
}

function filterTargetChangesHistory(filter) {
  CURRENT_TARGET_FILTER = filter;
  renderTargetChangesHistory();
}

function renderTargetChangesHistory() {
  const container = $("target-history-list");
  const filtersContainer = $("target-history-filters");
  if (!container) return;

  const upCount = ALL_TARGET_CHANGES.filter(i => i.pct > 0.05).length;
  const downCount = ALL_TARGET_CHANGES.filter(i => i.pct < -0.05).length;
  const flatCount = ALL_TARGET_CHANGES.filter(i => i.pct >= -0.05 && i.pct <= 0.05).length;
  const totalCount = ALL_TARGET_CHANGES.length;

  if (filtersContainer) {
    const filters = [
      { id: "all", label: "All", count: totalCount, icon: "list" },
      { id: "growth", label: "Growth", count: upCount, icon: "trending_up", activeColor: "text-emerald-700 bg-emerald-500/10 border-emerald-500/30" },
      { id: "reduce", label: "Recovery", count: downCount, icon: "trending_down", activeColor: "text-rose-700 bg-rose-500/10 border-rose-500/30" },
      { id: "steady", label: "Maintained", count: flatCount, icon: "pause_circle", activeColor: "text-slate-700 bg-slate-500/10 border-slate-500/30" },
    ];
    filtersContainer.innerHTML = filters.map(f => {
      const isActive = CURRENT_TARGET_FILTER === f.id;
      const activeClass = isActive
        ? (f.activeColor || "bg-primary text-white font-bold shadow-xs")
        : "bg-white/70 text-secondary hover:bg-white border-black/5";
      return `
        <button onclick="filterTargetChangesHistory('${f.id}')"
          class="px-2.5 py-1 rounded-full text-[11px] font-semibold border flex items-center gap-1 transition-all shrink-0 ${activeClass}">
          <span class="material-symbols-outlined text-[13px]">${f.icon}</span>
          ${f.label} (${f.count})
        </button>`;
    }).join("");
  }

  let filtered = ALL_TARGET_CHANGES;
  if (CURRENT_TARGET_FILTER === "growth") {
    filtered = ALL_TARGET_CHANGES.filter(i => i.pct > 0.05);
  } else if (CURRENT_TARGET_FILTER === "reduce") {
    filtered = ALL_TARGET_CHANGES.filter(i => i.pct < -0.05);
  } else if (CURRENT_TARGET_FILTER === "steady") {
    filtered = ALL_TARGET_CHANGES.filter(i => i.pct >= -0.05 && i.pct <= 0.05);
  }

  if (!filtered.length) {
    container.innerHTML = `
      <div class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg text-center flex flex-col items-center gap-2">
        <span class="material-symbols-outlined text-secondary/40 text-[36px]">event_note</span>
        <p class="font-headline-sm text-headline-sm text-on-background font-bold">No Changes in this Category</p>
        <p class="font-body-md text-secondary text-[12px] max-w-xs">
          ${ALL_TARGET_CHANGES.length ? "Try selecting another filter above to view your target history." : "Complete your active workouts this week, and your targets will automatically adjust based on your consistency and recovery!"}
        </p>
      </div>`;
    return;
  }

  container.innerHTML = filtered.map(item => {
    const details = getProgressionDetails(item);
    const exerciseIcon = icon(item.activity_type);

    return `
      <div class="bg-white/85 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-md flex flex-col gap-sm hover:shadow-md transition-shadow">
        <!-- Header row -->
        <div class="flex items-start justify-between gap-sm">
          <div class="flex items-center gap-sm">
            <div class="w-10 h-10 rounded-xl flex items-center justify-center bg-black/[0.03] border border-black/5 shrink-0">
              <span class="material-symbols-outlined text-[22px] text-primary">${exerciseIcon}</span>
            </div>
            <div>
              <p class="font-body-lg text-body-lg text-on-background font-bold">${escapeHtml(item.display_name)}</p>
              <p class="text-[11px] font-medium text-secondary">
                ${item.date ? item.date : "Recent Evaluation"} · <span class="capitalize">${escapeHtml(item.reason_raw || item.reason_label || "Weekly review")}</span>
              </p>
            </div>
          </div>
          <!-- Growth Percentage Pill -->
          <div class="flex flex-col items-end gap-1 shrink-0">
            <span class="text-[12px] font-black px-2.5 py-1 rounded-full border flex items-center gap-1 ${details.badgeClass}">
              <span class="material-symbols-outlined text-[15px] font-bold">${details.iconName}</span>
              ${details.badgeText}
            </span>
          </div>
        </div>

        <!-- Target Comparison: Old Target -> New Target -->
        <div class="bg-black/[0.025] rounded-xl p-sm border border-black/5 flex items-center justify-between">
          <div class="flex flex-col">
            <span class="text-[10px] uppercase font-bold tracking-wider text-secondary">Previous Target</span>
            <span class="font-headline-sm text-[15px] font-bold text-on-background">${item.old_target} ${escapeHtml(item.unit)}</span>
          </div>
          <div class="flex items-center gap-1 text-secondary/60">
            <span class="material-symbols-outlined text-[18px]">trending_flat</span>
          </div>
          <div class="flex flex-col items-end text-right">
            <span class="text-[10px] uppercase font-bold tracking-wider text-secondary">New Target</span>
            <span class="font-headline-sm text-[15px] font-black ${details.isUp ? 'text-emerald-700' : details.isDown ? 'text-rose-700' : 'text-primary'}">${item.new_target} ${escapeHtml(item.unit)}</span>
          </div>
        </div>

        <!-- Why This Changed: Explanation Block -->
        <div class="rounded-xl p-sm border ${details.whyBoxBg}">
          <div class="flex items-center gap-1.5 mb-1">
            <span class="material-symbols-outlined text-[16px] ${details.whyIconColor}">${details.whyIcon}</span>
            <span class="text-[12px] font-bold ${details.whyTitleColor}">${escapeHtml(details.reasonTitle)}</span>
          </div>
          <p class="text-[11.5px] leading-relaxed ${details.whyTextColor}">
            ${details.reasonDesc}
          </p>
        </div>

        <!-- Metrics Row: Adherence & Date -->
        <div class="flex items-center justify-between pt-1 border-t border-black/5 text-[11px] text-secondary">
          <div class="flex items-center gap-1.5 flex-wrap">
            <span class="material-symbols-outlined text-[14px]">tune</span>
            <span>Adherence: <strong class="text-on-background">${item.adherence != null ? `${Math.round(item.adherence)}%` : 'N/A'}</strong></span>
            ${details.adherenceBadge}
          </div>
          <div class="flex items-center gap-1 shrink-0">
            <span class="material-symbols-outlined text-[14px]">calendar_today</span>
            <span>${item.date}</span>
          </div>
        </div>
      </div>`;
  }).join("");
}

async function openTargetChangesHistory() {
  let progression = CACHED_PROGRESSION;
  if (!progression || !progression.length) {
    try {
      const d = await api("GET", "/v1/dashboard/summary");
      progression = d.progression || [];
      CACHED_PROGRESSION = progression;
    } catch {}
  }

  ALL_TARGET_CHANGES = [];
  (progression || []).forEach(act => {
    (act.points || []).forEach(pt => {
      const oldVal = parseFloat(pt.old_target) || 0;
      const newVal = parseFloat(pt.new_target) || 0;
      const unit = pt.unit || act.unit || TAX_UNIT_LOOKUP[act.activity_type] || "";
      let pct = 0;
      if (oldVal > 0) {
        pct = ((newVal - oldVal) / oldVal) * 100;
      } else if (newVal > 0) {
        pct = 100;
      }

      ALL_TARGET_CHANGES.push({
        activity_type: act.activity_type,
        display_name: act.display_name || act.activity_type,
        unit: unit,
        old_target: pt.old_target,
        new_target: pt.new_target,
        pct: pct,
        reason: pt.reason,
        reason_raw: pt.reason,
        adherence: pt.adherence_pct != null ? parseFloat(pt.adherence_pct) : null,
        curve_pct: pt.curve_pct != null ? parseFloat(pt.curve_pct) : null,
        date: pt.applied_at ? new Date(pt.applied_at).toLocaleDateString() : "",
        timestamp: pt.applied_at ? new Date(pt.applied_at).getTime() : 0,
      });
    });
  });

  ALL_TARGET_CHANGES.sort((a, b) => b.timestamp - a.timestamp);
  CURRENT_TARGET_FILTER = "all";
  renderTargetChangesHistory();
  go("target-history");
}

async function doLogout() {
  try { await api("POST","/v1/auth/logout",{refresh_token:RT}); } catch {}
  // RT too, not just AT — every other sign-out path (savePassword,
  // delAccount) clears both, and leaving the in-memory refresh token
  // pointing at a now-revoked row means the next 401 spends a doomed
  // refresh round-trip before giving up.
  localStorage.clear(); AT=null; RT=null; go("auth");
}
async function delAccount() {
  if (!(await appConfirm({ message: "Delete your account?", confirmText: "Delete", danger: true }))) return;
  // DELETE /v1/account now requires re-confirming the password (found live,
  // third-pass code audit — the most destructive action in the app used to
  // need only a valid session, no re-authentication).
  const password = window.prompt("Confirm your password to delete your account:");
  if (!password) return;
  // Only sign out if the delete actually succeeded. Swallowing the error and
  // clearing storage regardless (what this used to do) told the user their
  // account was gone when a wrong password or a 500 meant it very much was
  // not — they'd be logged out, believing it was deleted, with the account
  // still live. doLogout's empty catch above is fine for the opposite reason:
  // there, ending the local session is the whole point either way.
  try {
    await api("DELETE", "/v1/account", {password});
  } catch (e) {
    toast(e.message || "Could not delete your account. Please try again.", true);
    return;
  }
  localStorage.clear(); AT=null; RT=null; go("auth");
}

// ── Managed plans (teacher/doctor/parent) ──
// See MANAGED_PLANS_TEACHER_DOCTOR_PARENT_PLAN.md. No separate account type —
// this is a feature every account already has, reached from Settings.
const RELATION_LABELS = {child: "Child", student: "Student", patient: "Patient", other: "Other"};
const RELATION_ICONS = {child: "family_restroom", student: "school", patient: "stethoscope", other: "person"};

// Captured once at load: an invite link looks like ?invite=<token>. Kept in
// sessionStorage so it survives the signup/login/onboarding screens between
// when the link is opened and when the person is actually authenticated.
let PENDING_INVITE_TOKEN = new URLSearchParams(location.search).get("invite")
  || sessionStorage.getItem("pendingInvite") || null;
if (PENDING_INVITE_TOKEN) sessionStorage.setItem("pendingInvite", PENDING_INVITE_TOKEN);

// The actual mechanism confirmed in conversation: the managed person never
// signs up or logs in — opening the link itself grants access. This calls
// /v1/relationships/open directly (no auth needed, the token IS the
// credential) and stores the real token pair it returns, exactly as a normal
// login would. Returns true if it actually opened something, so the
// bootstrap IIFE below knows to skip the normal "no token -> show auth
// screen" check entirely.
async function openPendingInviteIfAny() {
  if (!PENDING_INVITE_TOKEN) return false;
  const token = PENDING_INVITE_TOKEN;
  sessionStorage.removeItem("pendingInvite");
  PENDING_INVITE_TOKEN = null;
  try {
    const d = await api("POST", "/v1/relationships/open", {invite_token: token});
    AT = d.access_token; RT = d.refresh_token;
    localStorage.setItem("at", AT); localStorage.setItem("rt", RT);
    return true;
  } catch (e) {
    toast(e.message || "That link didn't work — it may have been revoked.", true);
    return false;
  }
}

let RELATIONSHIPS_CACHE = [];

async function loadRelationships() {
  const { relationships } = await api("GET", "/v1/relationships");
  RELATIONSHIPS_CACHE = relationships;
  const revokedCount = relationships.filter(r => r.status === "revoked").length;

  if (!relationships.length) {
    $("rel-list").innerHTML = `<p class="font-body-md text-on-surface-variant text-center py-lg">You're not managing anyone's plan yet. Tap "New" to invite a child, student, or patient.</p>`;
    return;
  }

  const itemsHtml = relationships.map(r => {
    const isRevoked = r.status === "revoked";
    const openFn = r.status === "active" ? `openRelationshipDetail('${r.id}')`
      : r.status === "pending" ? `openPendingBuild('${r.id}', '${r.invite_token}')` : null;
    const clickable = openFn ? `cursor-pointer" onclick="${openFn}` : "";
    const rawName = r.name || RELATION_LABELS[r.relation_type] || r.relation_type;
    const displayName = escapeHtml(rawName);
    const safeName = rawName.replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/"/g, '&quot;');

    return `
    <div class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-md flex items-center gap-sm">
      <div class="w-10 h-10 rounded-full ${isRevoked ? 'bg-black/5 text-secondary' : 'bg-primary-container text-on-primary-container'} flex items-center justify-center shrink-0 ${clickable}">
        <span class="material-symbols-outlined">${RELATION_ICONS[r.relation_type] || 'person'}</span>
      </div>
      <div class="flex-1 min-w-0 ${clickable}">
        <h4 class="font-label-bold text-label-bold ${isRevoked ? 'text-secondary/70' : 'text-on-surface'}">${displayName}</h4>
        <p class="font-label-sm text-label-sm text-secondary">${RELATION_LABELS[r.relation_type] || r.relation_type} · ${r.status === 'pending' ? 'Not accepted yet — tap to set up their plan' : isRevoked ? 'Revoked' : 'Active'}</p>
      </div>
      ${r.status === 'pending' ? `<button type="button" onclick="event.stopPropagation(); copyInviteLink('${r.invite_token}')" class="shrink-0 bg-primary text-on-primary px-md py-xs rounded-full font-label-bold text-label-bold hover:opacity-95 active:scale-95 transition-all">Copy link</button>` : ''}
      ${!isRevoked ? `
        <button type="button" onclick="event.stopPropagation(); event.preventDefault(); promptRevokeRelationship('${r.id}', '${safeName}', '${r.status}')" title="${r.status === 'pending' ? 'Cancel invite' : 'Revoke plan'}" aria-label="Revoke" class="w-10 h-10 rounded-full flex items-center justify-center text-secondary hover:text-error hover:bg-error/10 active:scale-90 transition-all shrink-0 cursor-pointer">
          <span class="material-symbols-outlined text-[22px] pointer-events-none">close</span>
        </button>
      ` : `
        <button type="button" onclick="event.stopPropagation(); event.preventDefault(); promptDeleteRelationship('${r.id}', '${safeName}')" title="Remove from list" aria-label="Remove" class="w-10 h-10 rounded-full flex items-center justify-center text-secondary/60 hover:text-error hover:bg-error/10 active:scale-90 transition-all shrink-0 cursor-pointer">
          <span class="material-symbols-outlined text-[20px] pointer-events-none">delete_outline</span>
        </button>
      `}
    </div>`;
  }).join("");

  const clearBar = revokedCount > 0 ? `
    <div class="pt-2 text-center">
      <button type="button" onclick="clearAllRevokedPlans()" class="text-xs font-semibold text-secondary hover:text-error transition-colors px-3 py-1.5 rounded-full hover:bg-error/5 cursor-pointer">
        Clear all revoked plans (${revokedCount})
      </button>
    </div>` : "";

  $("rel-list").innerHTML = itemsHtml + clearBar;
}

function copyInviteLink(token) {
  const link = `${location.origin}${location.pathname}?invite=${token}`;
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(link).then(
      () => toast("Link copied — send it to them to get started"),
      () => window.prompt("Copy this link and send it to them:", link),
    );
  } else {
    window.prompt("Copy this link and send it to them:", link);
  }
}

function openCreateInviteModal() {
  $("invite-name").value = "";
  const m = $("invite-modal"); m.classList.remove("hidden"); m.classList.add("flex");
}
function closeCreateInviteModal() {
  const m = $("invite-modal"); m.classList.add("hidden"); m.classList.remove("flex");
}

async function createRelationshipInvite(relationType) {
  const name = $("invite-name").value.trim();
  if (!name) { toast("Enter their name first.", true); return; }
  try {
    const r = await api("POST", "/v1/relationships", {relation_type: relationType, name});
    closeCreateInviteModal();
    // Build the plan FIRST, get the link after — so the moment it's
    // claimed, the person sees it already set up instead of an empty
    // account waiting on a second trip back from the manager.
    openPendingBuild(r.id, r.invite_token, r.name);
  } catch (e) { toast(e.message, true); }
}

let CURRENT_INVITE_TOKEN = null;
let CURRENT_RELATIONSHIP_NAME = "";

async function openPendingBuild(relationshipId, inviteToken, name, preloaded) {
  CURRENT_RELATIONSHIP = relationshipId;
  CURRENT_IS_PENDING = true;
  CURRENT_INVITE_TOKEN = inviteToken;
  // Passed directly when called right after creating a new invite (real JS
  // value, no quoting risk); looked up from the roster when reopening an
  // existing pending one from an inline onclick, which can't safely carry
  // arbitrary manager-typed text itself.
  const cached = preloaded || RELATIONSHIPS_CACHE.find(r => r.id === relationshipId) || {};
  CURRENT_RELATIONSHIP_NAME = name || cached.name || "";
  CURRENT_MANAGED_USER_ID = null;
  // Every time the plan is opened, the info step comes first (pre-filled
  // with whatever was saved before) -- confirmed product behaviour: saving
  // it (even unchanged) is what moves the manager into the activities step,
  // not a one-time setup that's then skipped on later visits.
  PENDING_PROFILE_CONDITIONS = new Set(cached.pending_condition_codes || []);
  PENDING_PROFILE_AGE = cached.pending_age || 1;
  PENDING_PROFILE_BAND = cached.pending_experience_band || "beginner";
  PENDING_STEP = "profile";
  await renderPendingBuild();
  go("relationship-detail");
}

let PENDING_PROFILE_CONDITIONS = new Set();
let PENDING_PROFILE_AGE = 1;
let PENDING_PROFILE_BAND = "beginner";
let PENDING_STEP = "profile"; // "profile" | "activities"

async function renderPendingBuild() {
  $("rd-title").textContent = CURRENT_RELATIONSHIP_NAME
    ? `Set Up ${CURRENT_RELATIONSHIP_NAME}'s Plan` : "Set Up Their Plan";

  if (PENDING_STEP === "profile") {
    // Asked of the MANAGER, never the managed person. Only actually matters
    // if at least one activity ends up set to auto-update (needed for the
    // safety grid); left blank for an all-fixed plan, where it's simply
    // never used. Shown every time the plan is opened, pre-filled with
    // whatever was saved before -- Save is what moves on to Activities.
    const { conditions } = await api("GET", "/v1/conditions");
    $("rd-body").innerHTML = `
      <section class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg flex flex-col gap-sm">
        <h3 class="font-headline-sm text-headline-sm text-on-background">A couple of questions about them</h3>
        <p class="font-label-sm text-label-sm text-secondary -mt-1">Only needed if you set any activity to auto-update — the app uses this to keep it safe for them. Leave it if everything will be fixed.</p>
        <div>
          <label class="font-label-bold text-label-bold text-on-surface">Their age</label>
          <input id="pp-age" type="number" min="1" value="${PENDING_PROFILE_AGE}"
            class="w-full mt-xs bg-surface-container-low border-none rounded-lg px-md py-sm font-body-md text-body-md">
        </div>
        <div>
          <label class="font-label-bold text-label-bold text-on-surface">Health conditions</label>
          <div id="pp-conditions" class="flex flex-wrap gap-xs mt-xs">
            ${conditions.map(c => `
              <button type="button" onclick="togglePendingCondition('${c.code}')"
                class="pp-cond-chip px-md py-sm rounded-full font-label-sm text-label-sm border transition-colors ${PENDING_PROFILE_CONDITIONS.has(c.code) ? 'bg-primary-fixed/20 border-primary-fixed text-on-primary-fixed-variant' : 'bg-surface-container border-outline-variant text-on-surface-variant'}"
                data-code="${c.code}">${escapeHtml(c.name)}${c.bucket === 'refer' ? ' ⚠' : ''}</button>
            `).join("")}
          </div>
        </div>
        <div>
          <label class="font-label-bold text-label-bold text-on-surface">Their exercise experience</label>
          <p class="font-label-sm text-label-sm text-secondary -mt-1 mb-1">Used to size the suggested starting numbers below — more experience, a bigger starting point.</p>
          <select id="pp-band" class="w-full bg-surface-container-low border-none rounded-lg px-md py-sm font-body-md text-body-md">
            ${BANDS.map(([v, l]) => `<option value="${v}" ${PENDING_PROFILE_BAND === v ? "selected" : ""}>${l}</option>`).join("")}
          </select>
        </div>
        <button onclick="savePendingProfile()" class="bg-primary text-on-primary rounded-full py-sm font-label-bold text-label-bold">Save &amp; Continue</button>
      </section>`;
    return;
  }

  const [{ entries }, { exercises: customEntries }, { suggestions }] = await Promise.all([
    api("GET", `/v1/relationships/${CURRENT_RELATIONSHIP}/pending-entries`),
    api("GET", `/v1/relationships/${CURRENT_RELATIONSHIP}/pending-custom-exercises`),
    api("GET", `/v1/relationships/${CURRENT_RELATIONSHIP}/suggested-exercises`),
  ]);
  PENDING_SUGGESTIONS_CACHE = suggestions;
  PENDING_ENTRIES_CACHE = entries;
  const suggestionsHtml = renderSuggestionsSection(suggestions);
  const entriesHtml = entries.map(e => {
    const isDist = e.activity_type === "walk" || e.activity_type === "running";
    const targetDesc = isDist ? `${e.target_amount} km`
      : (e.set_amounts ? e.set_amounts.join(", ") : `${e.target_sets} x ${e.target_amount}`);
    return `
    <div class="flex items-center justify-between py-sm border-t border-black/5">
      <div class="min-w-0">
        <p class="font-body-lg text-body-lg text-on-background">${escapeHtml(e.activity_type)}</p>
        <p class="font-label-sm text-label-sm text-secondary">${targetDesc} · ${e.auto_progress ? "auto-adjusting" : "fixed"}</p>
      </div>
      <div class="shrink-0 flex items-center gap-xs">
        <button onclick="editPendingEntry('${e.activity_type}')" aria-label="Edit"
          class="bg-black/5 hover:bg-black/10 px-md py-xs rounded-full font-label-bold text-label-bold text-on-background">Edit</button>
        <button onclick="removePendingEntry('${e.activity_type}')" aria-label="Remove"
          class="text-secondary hover:text-error"><span class="material-symbols-outlined text-[20px]">close</span></button>
      </div>
    </div>`;
  }).join("");
  const customHtml = customEntries.map(e => `
    <div class="flex items-center justify-between py-sm border-t border-black/5">
      <div class="min-w-0">
        <p class="font-body-lg text-body-lg text-on-background">${escapeHtml(e.name)}</p>
        <p class="font-label-sm text-label-sm text-secondary">Custom · ${e.target_sets ? `${e.target_sets} x ${e.target_amount}` : e.target_amount} ${e.measurement_type}</p>
      </div>
      <button onclick="removePendingCustomExercise('${e.id}')" aria-label="Remove"
        class="shrink-0 text-secondary hover:text-error"><span class="material-symbols-outlined text-[20px]">close</span></button>
    </div>`).join("");
  const combinedHtml = entriesHtml + customHtml || `<p class="font-body-md text-on-surface-variant py-sm">Add at least one activity, then share the link — they'll see this plan the moment they open it.</p>`;

  $("rd-body").innerHTML = `
    <button onclick="editPendingProfile()" class="self-start flex items-center gap-1 text-secondary font-label-bold text-label-bold mb-sm">
      <span class="material-symbols-outlined text-[18px]">edit</span> Edit their info
    </button>
    ${suggestionsHtml}
    <section class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg flex flex-col gap-1">
      <h3 class="font-headline-sm text-headline-sm text-on-background mb-xs">Activities</h3>
      ${combinedHtml}
    </section>
    <button onclick="copyInviteLink('${CURRENT_INVITE_TOKEN}')"
      class="bg-primary text-on-primary rounded-full py-md font-label-bold text-label-bold flex items-center justify-center gap-xs">
      <span class="material-symbols-outlined text-[18px]">link</span> Get the Link to Share
    </button>`;
}

const RANK_LABEL = ["Best Pick", "2nd Best", "3rd Best", "4th Best", "5th Best"];
let PENDING_SUGGESTIONS_CACHE = [];
async function acceptSuggestedExercise(activityType) {
  const s = PENDING_SUGGESTIONS_CACHE.find(x => x.activity_type === activityType);
  if (!s) return;
  const single = s.prescription_type === "distance" || s.prescription_type === "duration";
  const sets = single ? 1 : (parseInt($(`sg-sets-${activityType}`).value, 10) || 1);
  const amount = parseFloat($(`sg-amount-${activityType}`).value);
  if (!amount || amount <= 0) { toast("Enter an amount greater than zero.", true); return; }
  try {
    if (CURRENT_IS_PENDING) {
      const d = await api("POST", `/v1/relationships/${CURRENT_RELATIONSHIP}/pending-entries`, {
        activity_type: activityType, target_sets: sets, target_amount: amount, auto_progress: true,
      });
      if (d.needs_confirmation) {
        toast("That's blocked for the condition(s) you set. Use Custom Exercise instead.", true);
        return;
      }
      toast("Added to their plan");
      await renderPendingBuild();
      return;
    }
    // Active relationship: same two-step add_program_entry + set target
    // pattern submitManagedAdd already uses for a manual add.
    const d = await api("POST", "/v1/program/entries", {
      activity_type: activityType, managed_user_id: CURRENT_MANAGED_USER_ID, auto_progress: true,
    });
    if (d.needs_confirmation) {
      toast("That's blocked for the condition(s) you set. Use Custom Exercise instead.", true);
      return;
    }
    await api("PUT", `/v1/program/entries/${activityType}/target`, {
      managed_user_id: CURRENT_MANAGED_USER_ID, target_sets: sets, target_amount: amount,
    });
    toast("Added to their plan");
    await openRelationshipDetail(CURRENT_RELATIONSHIP);
  } catch (e) { toast(e.message, true); }
}

async function removePendingCustomExercise(id) {
  try {
    await api("DELETE", `/v1/relationships/${CURRENT_RELATIONSHIP}/pending-custom-exercises/${id}`);
    await renderPendingBuild();
  } catch (e) { toast(e.message, true); }
}

function editPendingProfile() {
  PENDING_STEP = "profile";
  renderPendingBuild();
}

function togglePendingCondition(code) {
  if (PENDING_PROFILE_CONDITIONS.has(code)) PENDING_PROFILE_CONDITIONS.delete(code);
  else PENDING_PROFILE_CONDITIONS.add(code);
  document.querySelectorAll(".pp-cond-chip").forEach(btn => {
    const on = PENDING_PROFILE_CONDITIONS.has(btn.dataset.code);
    btn.className = `pp-cond-chip px-md py-sm rounded-full font-label-sm text-label-sm border transition-colors ${on ? 'bg-primary-fixed/20 border-primary-fixed text-on-primary-fixed-variant' : 'bg-surface-container border-outline-variant text-on-surface-variant'}`;
  });
}

async function savePendingProfile() {
  const age = parseInt($("pp-age").value, 10);
  if (!age || age < 1) { toast("Enter a real age.", true); return; }
  const band = $("pp-band").value;
  try {
    await api("PUT", `/v1/relationships/${CURRENT_RELATIONSHIP}/pending-profile`, {
      age, condition_codes: [...PENDING_PROFILE_CONDITIONS], experience_band: band,
    });
    PENDING_PROFILE_AGE = age; // so it stays filled in if reopened later
    PENDING_PROFILE_BAND = band;
    PENDING_STEP = "activities";
    toast("Saved");
    await renderPendingBuild();
  } catch (e) { toast(e.message, true); }
}

async function removePendingEntry(activityType) {
  try {
    await api("DELETE", `/v1/relationships/${CURRENT_RELATIONSHIP}/pending-entries/${activityType}`);
    await renderPendingBuild();
  } catch (e) { toast(e.message, true); }
}

let PENDING_REVOKE_ID = null;
let PENDING_REVOKE_HARD = false;

function promptRevokeRelationship(id, name, status) {
  PENDING_REVOKE_ID = id;
  PENDING_REVOKE_HARD = false;
  const m = $("revoke-rel-modal");
  if (!m) {
    revokeRelationship(id);
    return;
  }
  const cleanName = name || "this person";
  if (status === "pending") {
    $("revoke-modal-title").textContent = `Cancel Invite for ${cleanName}?`;
    $("revoke-modal-desc").textContent = `This invite link will be cancelled and will no longer work.`;
    $("revoke-modal-confirm-btn").textContent = "Cancel Invite";
    $("revoke-modal-icon").textContent = "cancel";
  } else {
    $("revoke-modal-title").textContent = `Stop Managing ${cleanName}'s Plan?`;
    $("revoke-modal-desc").textContent = `They keep all their current activities and history — you just won't be able to see or edit their plan anymore.`;
    $("revoke-modal-confirm-btn").textContent = "Revoke Plan";
    $("revoke-modal-icon").textContent = "person_remove";
  }
  m.classList.remove("hidden");
  m.classList.add("flex");
}

function promptDeleteRelationship(id, name) {
  PENDING_REVOKE_ID = id;
  PENDING_REVOKE_HARD = true;
  const m = $("revoke-rel-modal");
  if (!m) return;
  const cleanName = name || "this entry";
  $("revoke-modal-title").textContent = `Remove ${cleanName} from list?`;
  $("revoke-modal-desc").textContent = `This plan has already been revoked. Removing it clears this card from your list.`;
  $("revoke-modal-confirm-btn").textContent = "Remove";
  $("revoke-modal-icon").textContent = "delete";
  m.classList.remove("hidden");
  m.classList.add("flex");
}

function closeRevokeModal() {
  const m = $("revoke-rel-modal");
  if (m) {
    m.classList.add("hidden");
    m.classList.remove("flex");
  }
  PENDING_REVOKE_ID = null;
  PENDING_REVOKE_HARD = false;
}

async function executeRevokeRelationship() {
  const id = PENDING_REVOKE_ID;
  const hard = PENDING_REVOKE_HARD;
  if (!id) return;
  closeRevokeModal();
  try {
    const url = hard ? `/v1/relationships/${id}?delete=true` : `/v1/relationships/${id}`;
    await api("DELETE", url);
    toast(hard ? "Removed from list" : "Plan revoked");
    await loadRelationships();
  } catch (e) {
    toast(e.message || "Failed to update plan", true);
  }
}

async function clearAllRevokedPlans() {
  try {
    const res = await api("DELETE", "/v1/relationships");
    toast(`Cleared ${res.count || 0} revoked plans`);
    await loadRelationships();
  } catch (e) {
    toast(e.message || "Failed to clear revoked plans", true);
  }
}

async function revokeRelationship(id) {
  try {
    await api("DELETE", `/v1/relationships/${id}`);
    toast("Plan revoked");
    await loadRelationships();
  } catch (e) {
    toast(e.message, true);
  }
}

let CURRENT_RELATIONSHIP = null;
let CURRENT_MANAGED_USER_ID = null;
let CURRENT_IS_PENDING = false;

async function openRelationshipDetail(id) {
  try {
    const [d, { suggestions }] = await Promise.all([
      api("GET", `/v1/relationships/${id}/activity`),
      api("GET", `/v1/relationships/${id}/suggested-exercises`),
    ]);
    CURRENT_RELATIONSHIP = id;
    CURRENT_MANAGED_USER_ID = d.managed_user_id;
    CURRENT_IS_PENDING = false;
    PENDING_SUGGESTIONS_CACHE = suggestions;
    renderRelationshipDetail(d, suggestions);
    go("relationship-detail");
  } catch (e) { toast(e.message, true); }
}

function renderSuggestionsSection(suggestions) {
  if (!suggestions.length) return "";
  return `
    <section class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg flex flex-col gap-1">
      <h3 class="font-headline-sm text-headline-sm text-on-background mb-xs">Suggested for Them</h3>
      <p class="font-label-sm text-label-sm text-secondary -mt-1 mb-1">Chosen for the conditions you set — genuinely helps, not just safe. Ranked best first.</p>
      ${suggestions.map((s, i) => `
        <div class="flex items-center justify-between py-sm border-t border-black/5 gap-sm">
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-xs">
              <span class="shrink-0 w-6 h-6 rounded-full bg-primary-fixed/20 text-on-primary-fixed-variant font-label-bold text-label-sm flex items-center justify-center">${i + 1}</span>
              <p class="font-body-lg text-body-lg text-on-background">${escapeHtml(s.display_name)}</p>
            </div>
            <p class="font-label-sm text-label-sm text-secondary ml-8">${RANK_LABEL[i] || `${i + 1}th Best`}</p>
            <div class="flex items-center gap-xs mt-1">
              ${s.prescription_type === "reps" || s.prescription_type === "hold" ? `
                <input id="sg-sets-${s.activity_type}" type="number" min="1" value="${s.suggested_sets}"
                  class="w-12 bg-surface-container-low border-none rounded px-1 py-1 text-center font-label-sm text-label-sm">
                <span class="font-label-sm text-label-sm text-secondary">x</span>` : ""}
              <input id="sg-amount-${s.activity_type}" type="number" step="any" value="${s.suggested_amount}"
                class="w-16 bg-surface-container-low border-none rounded px-1 py-1 text-center font-label-sm text-label-sm">
              <span class="font-label-sm text-label-sm text-secondary">${s.amount_unit}</span>
            </div>
          </div>
          <button onclick="acceptSuggestedExercise('${s.activity_type}')"
            class="shrink-0 bg-primary text-on-primary px-md py-xs rounded-full font-label-bold text-label-bold">Add</button>
        </div>`).join("")}
    </section>`;
}

function renderRelationshipDetail(d, suggestions = []) {
  $("rd-title").textContent = d.name
    ? `${d.name}'s Plan (${RELATION_LABELS[d.relation_type] || d.relation_type})`
    : `Their Plan (${RELATION_LABELS[d.relation_type] || d.relation_type})`;
  const streak = d.streak.current || 0;
  const entries = d.managed_entries || [];
  MANAGED_ENTRIES_CACHE = entries;
  const overrides = d.safety_overrides || [];

  const entriesHtml = entries.length ? entries.map(e => {
    const isDist = e.activity_type === "walk" || e.activity_type === "running";
    const targetDesc = isDist ? `${e.target_amount} km`
      : (e.set_targets ? e.set_targets.join(", ") : `${e.target_sets} x ${e.target_amount}`);
    return `
    <div class="flex items-center justify-between py-sm border-t border-black/5">
      <div class="min-w-0">
        <p class="font-body-lg text-body-lg text-on-background">${escapeHtml(e.activity_type)}</p>
        <p class="font-label-sm text-label-sm text-secondary">${targetDesc} · ${e.auto_progress ? "auto-adjusting" : "fixed"}</p>
      </div>
      <button onclick="editManagedTarget('${e.activity_type}')"
        class="shrink-0 bg-black/5 hover:bg-black/10 px-md py-xs rounded-full font-label-bold text-label-bold text-on-background">Edit</button>
    </div>`;
  }).join("")
    : `<p class="font-body-md text-on-surface-variant py-sm">You haven't added anything to their plan yet.</p>`;

  $("rd-body").innerHTML = `
    <section class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg flex flex-col gap-sm">
      <div class="flex items-center justify-between">
        <p class="font-body-md text-on-surface-variant">Current streak</p>
        <p class="font-headline-sm text-headline-sm text-on-background">${streak} day${streak === 1 ? "" : "s"}</p>
      </div>
    </section>

    ${renderSuggestionsSection(suggestions)}

    <section class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg flex flex-col gap-1">
      <h3 class="font-headline-sm text-headline-sm text-on-background mb-xs">Activities you manage</h3>
      ${entriesHtml}
    </section>

    ${overrides.length ? `
    <section class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg flex flex-col gap-1">
      <h3 class="font-headline-sm text-headline-sm text-on-background mb-xs">Safety overrides on file</h3>
      ${overrides.map(o => `
        <div class="py-sm border-t border-black/5">
          <p class="font-body-md text-on-background">${escapeHtml(o.activity_type)} — ${escapeHtml(o.grid_verdict)}</p>
          <p class="font-label-sm text-label-sm text-secondary">${escapeHtml(o.warning_text)}</p>
        </div>`).join("")}
    </section>` : ""}

    <section class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg flex flex-col gap-1">
      <h3 class="font-headline-sm text-headline-sm text-on-background mb-xs">Recent activity (30 days)</h3>
      ${d.recent_activity.length ? d.recent_activity.slice(0, 15).map(a => `
        <div class="flex items-center justify-between py-xs border-t border-black/5">
          <span class="font-body-md text-on-background">${escapeHtml(a.name)}</span>
          <span class="font-label-sm text-label-sm text-secondary">${a.sets} x ${a.amount} ${escapeHtml(a.unit)} · ${a.date}</span>
        </div>`).join("")
        : `<p class="font-body-md text-on-surface-variant py-sm">Nothing logged yet.</p>`}
    </section>`;
}

let MANAGED_ENTRIES_CACHE = [];
// Set while the Add screen is being used to EDIT an item already on someone's plan.
let MA_EDIT_TYPE = null;

// Switches the shared "Add to their plan" screen between adding and editing.
function setManagedModalMode(editType, pending = false) {
  MA_EDIT_TYPE = editType || null;
  const editing = !!editType;
  $("ma-title").textContent = editing ? "Edit their plan item" : "Add to their plan";
  $("ma-submit-btn").textContent = editing ? "Save" : "Add";
  $("ma-activity").disabled = editing;
  // Saving a target doesn't change the auto-adjust choice, and a custom exercise
  // is a different action, so neither is offered while editing.
  // For a STAGED item the auto-adjust choice is part of what gets saved, so it stays.
  $("ma-auto-wrap").classList.toggle("hidden", editing && !pending);
  $("ma-custom-section").classList.toggle("hidden", editing);
}

// Edit uses the same screen as Add (so it has the same options, including a
// different amount for each set), prefilled with what the item is set to now.
// It used to be two plain prompt() boxes that could only set one number for
// every set.
let PENDING_ENTRIES_CACHE = [];

// Opens the Add screen as an editor for one plan item, prefilled. Shared by the
// live plan (saved with a PUT) and a staged item (re-staged, which replaces it).
async function openManagedEditScreen(activityType, entry, { perSet, auto, pending }) {
  try {
    if (!MANAGED_ACTIVITIES_CACHE.length) {
      MANAGED_ACTIVITIES_CACHE = (await api("GET", "/v1/taxonomy/activities")).activities;
    }
    const act = MANAGED_ACTIVITIES_CACHE.find(a => a.activity_type === activityType);
    setManagedModalMode(activityType, pending);
    $("ma-activity").innerHTML =
      `<option value="${escapeHtml(activityType)}">${escapeHtml(act ? act.display_name : activityType)}</option>`;
    $("ma-activity").value = activityType;
    updateManagedActivityFields();   // shows the right fields (and resets the per-set switch) for this activity
    $("ma-sets").value = entry.target_sets;
    $("ma-amount").value = entry.target_amount;
    if (pending) $("ma-auto").checked = auto !== false;
    if (perSet) {
      $("ma-per-set-toggle").checked = true;
      toggleManagedPerSet();
      MA_EDITOR.set(perSet);
    }
    $("managed-add-modal").classList.remove("hidden");
    $("managed-add-modal").classList.add("flex");
  } catch (e) { toast(e.message, true); }
}

// Edit uses the same screen as Add (so it has the same options, including a
// different amount for each set), prefilled with what the item is set to now.
async function editManagedTarget(activityType) {
  const entry = MANAGED_ENTRIES_CACHE.find(e => e.activity_type === activityType);
  if (!entry) return;
  await openManagedEditScreen(activityType, entry, { perSet: entry.set_targets, pending: false });
}

// Edit for an item staged before the invite is opened.
async function editPendingEntry(activityType) {
  const entry = PENDING_ENTRIES_CACHE.find(e => e.activity_type === activityType);
  if (!entry) return;
  await openManagedEditScreen(activityType, entry,
    { perSet: entry.set_amounts, auto: entry.auto_progress, pending: true });
}

let MANAGED_ACTIVITIES_CACHE = [];

// Reusable "different amount for each set" editor: renders one input per set
// (add / remove rows) into `container` and uses event delegation, so it needs
// no per-modal inline handlers or globals. The self-service Edit and Add
// screens (pe-/ca- ids) predate this and keep their own copies; any NEW screen
// that needs per-set amounts should use this instead of copying them again.
function makePerSetEditor(container) {
  let values = [];
  // Keep what the user typed across add/remove re-renders; a blank/invalid
  // input keeps its previous value here (get() below still reports it as NaN).
  const flush = () => {
    container.querySelectorAll("input").forEach((el, idx) => {
      const v = parseFloat(el.value);
      if (Number.isFinite(v)) values[idx] = v;
    });
  };
  const render = () => {
    container.innerHTML = values.map((v, idx) => `
      <div class="flex items-center justify-between gap-sm">
        <span class="font-label-bold text-label-bold text-on-surface w-14 shrink-0">Set ${idx + 1}</span>
        <input type="number" step="any" min="0" value="${v}"
          class="flex-1 bg-surface-container-low border-none rounded-lg px-md py-sm font-body-md text-body-md text-center">
        ${values.length > 1 ? `
        <button type="button" data-remove="${idx}" aria-label="Remove set" class="shrink-0 text-secondary hover:text-error">
          <span class="material-symbols-outlined text-[20px]">close</span>
        </button>` : `<span class="w-5 shrink-0"></span>`}
      </div>`).join("")
      + `<button type="button" data-add class="flex items-center justify-center gap-1 text-primary font-label-bold text-label-bold py-xs">
           <span class="material-symbols-outlined text-[18px]">add</span> Add Set
         </button>`;
  };
  container.addEventListener("click", (ev) => {
    const rm = ev.target.closest("[data-remove]");
    if (rm) { flush(); values.splice(Number(rm.dataset.remove), 1); render(); return; }
    if (ev.target.closest("[data-add]")) { flush(); values.push(values[values.length - 1] ?? 1); render(); }
  });
  return {
    set(vals) { values = vals.slice(); render(); },
    // Reads the inputs as they are right now; an empty box comes back as NaN
    // so the caller's "every set must be > 0" check catches it.
    get() { return Array.from(container.querySelectorAll("input"), (el) => parseFloat(el.value)); },
  };
}

let MA_EDITOR = null;

function toggleManagedPerSet() {
  const on = $("ma-per-set-toggle").checked;
  $("ma-target-fields").classList.toggle("hidden", on);
  $("ma-per-set-inputs").classList.toggle("hidden", !on);
  if (on) {
    if (!MA_EDITOR) MA_EDITOR = makePerSetEditor($("ma-per-set-inputs"));
    const n = Math.max(1, parseInt($("ma-sets").value, 10) || 1);
    const amount = parseFloat($("ma-amount").value);
    MA_EDITOR.set(Array.from({length: n}, () => (Number.isFinite(amount) && amount > 0 ? amount : 1)));
  }
}

async function openManagedAddModal() {
  if (!CURRENT_RELATIONSHIP) return;
  setManagedModalMode(null);
  try {
    const [{ activities }, { blocked }] = await Promise.all([
      api("GET", "/v1/taxonomy/activities"),
      api("GET", `/v1/relationships/${CURRENT_RELATIONSHIP}/blocked-activities`),
    ]);
    MANAGED_ACTIVITIES_CACHE = activities;
    // Blocked ones are disabled outright here, no override offered — if the
    // manager still wants that exact movement for them, Custom Exercise
    // (below, no grid check by design) is the way, not this picker.
    $("ma-activity").innerHTML = activities.map(a => {
      const note = blocked[a.activity_type];
      return `<option value="${a.activity_type}" ${note ? "disabled" : ""}>` +
        `${note ? "🚫 " : ""}${escapeHtml(a.display_name)}${note ? " — Blocked" : ""}</option>`;
    }).join("");
    const firstAvailable = activities.find(a => !blocked[a.activity_type]);
    if (firstAvailable) {
      $("ma-activity").value = firstAvailable.activity_type;
    }
    $("ma-amount").value = "";
    updateManagedActivityFields();
    $("ma-custom-name").value = "";
    $("ma-custom-type").value = "reps";
    $("ma-custom-sets").value = "2";
    $("ma-custom-amount").value = "";
    updateManagedCustomTargetFields();
    $("managed-add-modal").classList.remove("hidden");
    $("managed-add-modal").classList.add("flex");
  } catch (e) { toast(e.message, true); }
}
function closeManagedAddModal() {
  const m = $("managed-add-modal"); m.classList.add("hidden"); m.classList.remove("flex");
  setManagedModalMode(null);
}

function updateManagedActivityFields() {
  const activityType = $("ma-activity").value;
  const isDistance = activityType === "walk" || activityType === "running";
  const setsWrap = $("ma-sets-wrap");
  const amountWrap = $("ma-amount-wrap");
  const amountLabel = $("ma-amount-label");
  const targetGrid = $("ma-target-fields");
  const act = (MANAGED_ACTIVITIES_CACHE || []).find(a => a.activity_type === activityType);

  if (isDistance) {
    if (setsWrap) setsWrap.classList.add("hidden");
    if (targetGrid) {
      targetGrid.classList.remove("grid", "grid-cols-2", "gap-sm");
      targetGrid.classList.add("block");
    }
    if (amountLabel) amountLabel.textContent = "km";
    $("ma-sets").value = "1";
    $("ma-amount").placeholder = "e.g. 1.0";
    if (act) $("ma-amount").value = act.default_amount || "1";
  } else {
    if (setsWrap) setsWrap.classList.remove("hidden");
    if (targetGrid) {
      targetGrid.classList.remove("block");
      targetGrid.classList.add("grid", "grid-cols-2", "gap-sm");
    }
    if (amountLabel) {
      amountLabel.textContent = act?.prescription_type === "hold" ? "Seconds" : (act?.prescription_type === "reps" ? "Reps" : "Amount");
    }
    $("ma-amount").placeholder = "";
    if (act) {
      $("ma-sets").value = act.default_sets || "2";
      $("ma-amount").value = act.default_amount || "";
    }
  }

  // Per-set amounts only make sense for reps/hold activities (distance and
  // duration are a single effort). Changing the activity always resets the
  // toggle, so a stale breakdown for the previous activity can never be sent.
  const supportsPerSet = !isDistance && (act?.prescription_type === "reps" || act?.prescription_type === "hold");
  $("ma-per-set-toggle-wrap").classList.toggle("hidden", !supportsPerSet);
  $("ma-per-set-toggle-label").textContent =
    act?.prescription_type === "hold" ? "Different hold time for each set" : "Different reps for each set";
  $("ma-per-set-toggle").checked = false;
  $("ma-per-set-inputs").classList.add("hidden");
  if (targetGrid) targetGrid.classList.remove("hidden");
}

function updateManagedCustomTargetFields() {
  updateCustomTargetFields($("ma-custom-type").value, "ma-custom-sets-wrap", "ma-custom-amount-label");
}

async function submitManagedAdd() {
  const activityType = $("ma-activity").value;
  const isDistance = activityType === "walk" || activityType === "running";
  const perSet = !isDistance && $("ma-per-set-toggle").checked && MA_EDITOR;
  let setAmounts = null;
  if (perSet) {
    setAmounts = MA_EDITOR.get();
    if (!setAmounts.length || setAmounts.some(v => !Number.isFinite(v) || v <= 0)) {
      toast("Enter an amount greater than zero for every set.", true);
      return;
    }
  }
  const sets = isDistance ? 1 : (parseInt($("ma-sets").value, 10) || 1);
  const amount = parseFloat($("ma-amount").value);
  if (!perSet && (!amount || amount <= 0)) {
    toast(isDistance ? "Enter distance in km." : "Enter an amount greater than zero.", true);
    return;
  }
  try {
    if (MA_EDIT_TYPE && !CURRENT_IS_PENDING) {
      await api("PUT", `/v1/program/entries/${MA_EDIT_TYPE}/target`,
        perSet
          ? { managed_user_id: CURRENT_MANAGED_USER_ID, set_amounts: setAmounts }
          : { managed_user_id: CURRENT_MANAGED_USER_ID, target_sets: sets, target_amount: amount });
      closeManagedAddModal();
      toast("Updated");
      await openRelationshipDetail(CURRENT_RELATIONSHIP);
      return;
    }
    if (CURRENT_IS_PENDING) {
      // Staged against the invite itself — materializes automatically the
      // moment the link is opened (app/relationships/service.py's
      // open_invite). The dropdown already disables anything the safety grid
      // blocks for the staged conditions, so no override path is offered
      // here — if this still comes back blocked (stale dropdown data), the
      // manager is pointed at Custom Exercise instead of being asked to
      // confirm past it.
      const d = await api("POST", `/v1/relationships/${CURRENT_RELATIONSHIP}/pending-entries`,
        perSet
          ? { activity_type: activityType, set_amounts: setAmounts, auto_progress: $("ma-auto").checked }
          : { activity_type: activityType, target_sets: sets, target_amount: amount,
              auto_progress: $("ma-auto").checked });
      if (d.needs_confirmation) {
        toast("That's blocked for the condition(s) you set. Use Custom Exercise instead.", true);
        return;
      }
      const wasEdit = !!MA_EDIT_TYPE;
      closeManagedAddModal();
      toast(wasEdit ? "Updated" : "Added to their plan");
      await renderPendingBuild();
      return;
    }
    // add_program_entry always seeds the taxonomy's own default sets/amount
    // for a brand-new entry — it has no notion of "start it at this specific
    // number" itself. The follow-up PUT right below is what actually makes
    // this form's sets/amount values stick.
    const d = await api("POST", "/v1/program/entries", {
      activity_type: activityType, managed_user_id: CURRENT_MANAGED_USER_ID,
      auto_progress: $("ma-auto").checked,
    });
    if (d.needs_confirmation) {
      toast("That's blocked for the condition(s) you set. Use Custom Exercise instead.", true);
      return;
    }
    await api("PUT", `/v1/program/entries/${activityType}/target`,
      perSet
        ? { managed_user_id: CURRENT_MANAGED_USER_ID, set_amounts: setAmounts }
        : { managed_user_id: CURRENT_MANAGED_USER_ID, target_sets: sets, target_amount: amount });
    closeManagedAddModal();
    toast("Added to their plan");
    await openRelationshipDetail(CURRENT_RELATIONSHIP);
  } catch (e) { toast(e.message, true); }
}

async function submitManagedCustomAdd() {
  const name = $("ma-custom-name").value.trim();
  if (!name) { toast("Type a name first", true); return; }
  const measurementType = $("ma-custom-type").value;
  const single = measurementType === "distance" || measurementType === "duration";
  const targetAmount = parseFloat($("ma-custom-amount").value);
  if (!targetAmount || targetAmount <= 0) { toast("Enter a target greater than zero.", true); return; }
  const targetSets = single ? 1 : (parseInt($("ma-custom-sets").value, 10) || 1);
  try {
    if (CURRENT_IS_PENDING) {
      await api("POST", `/v1/relationships/${CURRENT_RELATIONSHIP}/pending-custom-exercises`, {
        name, measurement_type: measurementType,
        target_amount: targetAmount, target_sets: targetSets,
      });
      $("ma-custom-name").value = "";
      closeManagedAddModal();
      toast("Custom exercise staged for their plan");
      await renderPendingBuild();
      return;
    }
    await api("POST", "/v1/custom-exercises", {
      name, measurement_type: measurementType,
      target_amount: targetAmount, target_sets: targetSets,
      managed_user_id: CURRENT_MANAGED_USER_ID,
    });
    $("ma-custom-name").value = "";
    closeManagedAddModal();
    toast("Custom exercise added for them");
    await openRelationshipDetail(CURRENT_RELATIONSHIP);
  } catch (e) { toast(e.message, true); }
}

function showSorenessPrompt(activityType) {
  const p = $("pl-soreness-prompt");
  if (p) {
    const tap = $("pl-soreness-tap");
    if (tap) tap.setAttribute("onclick", `reportExerciseSoreness('${activityType}')`);
    p.classList.remove("hidden");
  }
  const pd = $("pl-done-soreness-prompt");
  if (pd) {
    const tapDone = $("pl-done-soreness-tap");
    if (tapDone) tapDone.setAttribute("onclick", `reportExerciseSoreness('${activityType}')`);
    pd.classList.remove("hidden");
  }
}

async function reportExerciseSoreness(activityType) {
  if ($("pl-soreness-prompt")) $("pl-soreness-prompt").classList.add("hidden");
  if ($("pl-done-soreness-prompt")) $("pl-done-soreness-prompt").classList.add("hidden");
  try {
    const r = await api("POST", "/v1/soreness/report", {activity_type: activityType});
    toast(r.reduced
      ? "Noted — we've reduced this exercise's target for now."
      : "Thanks, noted.");
  } catch (e) { toast(e.message, true); }
  // On the done-view the soreness prompt is purely informational — no navigation.
  if (!$("pl-done-view").classList.contains("hidden")) return;
  // Call the pending action (playerGo(1) or finishPlayerSession).
  if (playerPendingSorenessAction) {
    const action = playerPendingSorenessAction;
    playerPendingSorenessAction = null;
    await action();
  } else {
    playerGo(1);
  }
}

async function addWaterGlass() {
  try {
    const r = await api("POST", "/v1/water-intake/add", {});
    $("h-water-count").textContent = r.glasses_count;
  } catch (e) { toast(e.message, true); }
}

async function clearSoreness(activityType) {
  try {
    await api("POST", "/v1/soreness/clear", {activity_type: activityType});
    toast("Restored to its normal target.");
    loadHome();
  } catch (e) { toast(e.message, true); }
}

function dismissSorenessPrompt() {
  if ($("pl-soreness-prompt")) $("pl-soreness-prompt").classList.add("hidden");
  if ($("pl-done-soreness-prompt")) $("pl-done-soreness-prompt").classList.add("hidden");
  // On the done-view the soreness prompt is purely informational — no navigation.
  if (!$("pl-done-view").classList.contains("hidden")) return;
  // Call the pending action (playerGo(1) or finishPlayerSession).
  if (playerPendingSorenessAction) {
    const action = playerPendingSorenessAction;
    playerPendingSorenessAction = null;
    action();
  } else {
    playerGo(1);
  }
}

// Every function referenced from an inline onclick="..." in index.html must be
// reachable on window — a Vite module's top-level functions are scoped to the
// module, not attached to the global object the way a plain <script> tag's
// were in the original single-file version.
Object.assign(window, {
  $,
  doAuth, setAuthMode, toggleAuthMode, submitAuthForm,
  togglePasswordVisibility, checkPasswordRules,
  openForgotPasswordModal, closeForgotPasswordModal, submitForgotPassword,
  submitNewPassword,
  submitOnboarding, selectActivityPeriod,
  toggleWhisperRecording, voiceSubmitTranscript, confirmVoiceLog, editVoiceLog, cancelVoiceLog, editVoiceValue,
  setElevenLabsEnabled,
  openCatalogue, closeCatalogue, addFromCatalogue,
  renameCustomPrompt, deleteCustomExercise, customContinue,
  logCustomCard, openProfileEditor, closeProfileEditor, saveProfileEditor,
  openPasswordEditor, closePasswordEditor, toggleForgotPassword, savePassword,
  openDemo, closeDemo, sendChat, doLogout, delAccount, go,
  startWorkout, playerGo, playerMarkDone, playerAddRestTime, playerSkipRest, closePlayer,
  playerToggleSet, playerTogglePause,
  openCreateInviteModal, closeCreateInviteModal, createRelationshipInvite, copyInviteLink,
  promptRevokeRelationship, promptDeleteRelationship, closeRevokeModal, executeRevokeRelationship, clearAllRevokedPlans,
  revokeRelationship, openRelationshipDetail, editManagedTarget, editPendingEntry,
  openManagedAddModal, closeManagedAddModal, submitManagedAdd, submitManagedCustomAdd, toggleManagedPerSet,
  closeAppConfirm,
  openPendingBuild, removePendingEntry, togglePendingCondition, savePendingProfile,
  renderExerciseBrowse, editPendingProfile, removePendingCustomExercise,
  updateManagedActivityFields, updateManagedCustomTargetFields, acceptSuggestedExercise,
  openGpsTracker, closeGpsTracker, selectGpsActivity, startGpsTracking, finishGpsTracking,
  openGpsTrackerForPlayer, playerRemoveCurrent, playerEditCurrent, closePlayerEditModal, submitPlayerEdit,
  discardGpsManualDistance, submitGpsManualDistance,
  togglePePerSet, renderPePerSetInputs, updatePeSetValue, addPeSetRow, removePeSetRow, openCatalogueAddEditor,
  closeCatalogueAddEditor, submitCatalogueAdd, toggleCaPerSet, renderCaPerSetInputs,
  addCaSetRow, removeCaSetRow, updateCaSetValue, updatePlayerSetActual,
  openActivityLogHistory,
  openTargetChangesHistory,
  filterTargetChangesHistory,
  reportExerciseSoreness,
  addWaterGlass,
  clearSoreness,
  dismissSorenessPrompt,
  playerSetDistance,
  playerAdjustDistance,
  playerUseTarget,
});

(async () => {
  // Check if opened via password reset link: ?reset_token=... or ?token=...
  const urlParams = new URLSearchParams(window.location.search);
  const resetTokenFromUrl = urlParams.get("reset_token") || urlParams.get("token");
  if (resetTokenFromUrl) {
    if ($("r-token")) $("r-token").value = resetTokenFromUrl;
    go("reset-password");
    // Clean up query param from URL bar
    const cleanUrl = window.location.pathname + window.location.hash;
    window.history.replaceState({}, document.title, cleanUrl);
    return;
  }

  // Confirmed in conversation: opening an invite link grants access
  // directly — the managed person never signs up or logs in. This silently
  // creates (or re-opens) their real account and switches this tab into it,
  // regardless of whoever else might already be logged in on this device —
  // that's the whole mechanism, not a bug. Only revoking the link (done by
  // whoever created it) ever cuts this off.
  if (PENDING_INVITE_TOKEN) {
    const opened = await openPendingInviteIfAny();
    if (!opened) { go("auth"); return; }
  }
  if (!AT) { go("auth"); return; }
  try {
    await route();
  } catch (e) {
    // Only a genuine auth failure should end the session. This used to clear
    // storage on ANY error, so a network blip — or simply opening the app
    // while the backend is cold-starting — silently signed the user out and
    // dropped them on the login screen, with a perfectly valid token thrown
    // away. api() already handles real 401s (refresh, then sign out if that
    // fails), so anything else reaching here is an infrastructure problem,
    // not an expired session.
    if (e && (e.status === 401 || e.status === 403)) {
      localStorage.clear(); AT = null; RT = null;
      go("auth");
    } else {
      go("home");
      toast("Couldn't reach the server. Pull down to retry in a moment.", true);
    }
  }
})();
