import "./style.css";
import { elevenlabsSTT, elevenlabsTTS } from "./elevenlabs_tester.js";

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

const NAV_ON = "fixed bottom-md left-1/2 -translate-x-1/2 w-[90%] max-w-md z-50 flex justify-between items-center px-md py-sm bg-surface-container-lowest/80 backdrop-blur-md shadow-lg rounded-full";
const DEV = "web-" + (localStorage.getItem("did") || (()=>{const d=Math.random().toString(36).slice(2,10);localStorage.setItem("did",d);return d})());
let AT = localStorage.getItem("at"), RT = localStorage.getItem("rt"), EMAIL = localStorage.getItem("em") || "";

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

function go(name) {
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("on"));
  $("s-" + name).classList.add("on");
  $("nav").className = ["auth","onboard","player"].includes(name) ? "hidden" : NAV_ON;
  document.querySelectorAll(".nav-btn").forEach(b => {
    const on = b.dataset.t === name;
    b.className = (on
      ? "nav-btn flex flex-row items-center gap-xs bg-primary-container text-on-primary-container rounded-full px-md py-xs"
      : "nav-btn flex items-center justify-center text-secondary p-xs rounded-full") + " active:scale-90 duration-200";
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

// ── auth ──
async function doAuth(mode) {
  const email = $("a-email").value.trim();
  if (!email) {
    $("a-err").textContent = "Enter your email address.";
    $("a-err").className = "rounded-lg px-md py-sm font-body-md bg-error-container text-on-error-container";
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
    $("a-err").className = "rounded-lg px-md py-sm font-body-md bg-error-container text-on-error-container";
  }
}

async function route() {
  const p = await api("GET", "/v1/profile");
  await claimPendingInviteIfAny();
  renderNotifBadge();
  if (!p.onboarded) { await loadOnboarding(); go("onboard"); return; }
  if (p.referred) { $("refer-msg").textContent = "We can't suggest exercises based on what you selected."; go("refer"); $("nav").className = NAV_ON; return; }
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
    if (d.referred) { $("refer-msg").textContent = d.message; go("refer"); $("nav").className = NAV_ON; return; }
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

// ── custom exercises (4.4) — personal, tracking-only, never suggested ──
let customExercises = [];
let customMeasurementTypes = []; // 1-3 of CUSTOM_TYPES, in pick order
let customPhase = "name"; // name -> type
const CUSTOM_TYPES = [["reps","Reps"],["distance","Distance"],["duration","Time"]];

async function loadCustomExerciseSection() {
  $("custom-name").value = "";
  $("custom-err").className = "hidden";
  $("custom-type-wrap").classList.add("hidden");
  $("custom-continue-btn").textContent = "Continue";
  customMeasurementTypes = [];
  customPhase = "name";
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
  CUSTOM_TYPES.forEach(([v, l]) => box.appendChild(chip(l, customMeasurementTypes.includes(v), () => {
    if (customMeasurementTypes.includes(v)) {
      customMeasurementTypes = customMeasurementTypes.filter(x => x !== v);
    } else {
      customMeasurementTypes.push(v);
    }
    renderCustomTypeChips();
  })));
}

// Add a New One only ever CREATES the exercise (mirrors exactly how adding a
// built-in exercise via addFromCatalogue just adds it, never logs it).
// Logging happens separately, whenever the user actually does it, via its
// card in the "Your Own Exercises" section on Home. Pick 1, 2, or all 3 of
// Reps/Distance/Time — the card on Home then asks for all of the ones picked.
function customContinue() {
  if (customPhase === "type") {
    if (!customMeasurementTypes.length) { toast("Pick at least one way to count it", true); return; }
    submitCustomCreate();
    return;
  }
  const name = $("custom-name").value.trim();
  if (!name) { toast("Type a name first", true); return; }
  const match = customExercises.find(c => c.name.toLowerCase() === name.toLowerCase());
  if (match) { toast(`You already have "${match.name}" — log it from the Home screen`, true); return; }
  customMeasurementTypes = [];
  customPhase = "type";
  $("custom-type-wrap").classList.remove("hidden");
  renderCustomTypeChips();
  $("custom-continue-btn").textContent = "Save";
}

async function submitCustomCreate() {
  try {
    await api("POST", "/v1/custom-exercises", {
      name: $("custom-name").value.trim(), measurement_types: customMeasurementTypes,
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
  if (!confirm("Delete this exercise?")) return;
  try {
    await api("DELETE", `/v1/custom-exercises/${id}`);
    const d = await api("GET", "/v1/custom-exercises");
    customExercises = d.custom_exercises;
    renderCustomList();
    loadHome(); // keep the Dashboard's cards (renderCustomCard) in sync too
    toast("Deleted");
  } catch (e) { toast(e.message, true); }
}

// ── dashboard screen (stats + Your Own Exercises; the program's exercise
// list itself — formerly "Recommended Today" — now lives entirely inside
// the guided Workout Player, since it showed exactly the same exercises) ──
async function loadHome() {
  $("h-avatar").textContent = (EMAIL[0] || "?").toUpperCase();
  $("h-hello").textContent = "Welcome back, " + EMAIL.split("@")[0] + "!";

  // Fired together, not sequentially: the summary's streak/suggestions and
  // the 365-day trend (for the stat cards + heatmap) don't depend on each
  // other. suggestions.all_items is kept in HOME_ITEMS purely so the
  // dumbbell nav icon's startWorkout() has something to start from without
  // a separate fetch — this screen no longer renders them itself.
  const [d, yearTrend] = await Promise.all([
    api("GET", "/v1/dashboard/summary"),
    api("GET", "/v1/dashboard/trend?days=365"),
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

  // your own exercises — tracking-only, never suggested/programmed (4.4):
  // creating/logging here only ever calls custom-exercise endpoints, never
  // /v1/program/entries.
  const { custom_exercises } = await api("GET", "/v1/custom-exercises");
  HOME_CUSTOM_ITEMS = custom_exercises;
  $("h-custom-section").classList.toggle("hidden", !custom_exercises.length);
  $("h-custom-cards").innerHTML = custom_exercises.map(renderCustomCard).join("");

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
      PLAYER_ITEMS = PLAYER_ITEMS.concat(added);
      renderPlayerExercise();
    }
  } else {
    startWorkout();
  }
}

const CUSTOM_TYPE_LABEL = { reps: "Reps", distance: "Distance", duration: "Time" };

function renderCustomCard(c) {
  // 1-3 measurement types, each picked at creation — every one of them gets
  // its own Sets/Amount pair here, and Save logs all of them together.
  const blocks = c.measurement_types.map((mt, i) => {
    const unit = c.units[i];
    const single = mt === "distance" || mt === "duration";
    const amountLabel = mt === "reps" ? "Reps" : `${CUSTOM_TYPE_LABEL[mt]} (${unit})`;
    return `
    <div class="grid grid-cols-2 gap-sm items-stretch">
      <div class="space-y-xs flex flex-col min-w-0">
        <label class="text-label-sm text-secondary truncate block h-4 mb-1">Sets</label>
        ${single
          ? `<div class="bg-surface-container-low rounded-DEFAULT h-10 flex items-center justify-center"><span class="font-body-md text-on-surface-variant text-xs">—</span></div>`
          : `<input id="cc-sets-${c.id}-${mt}" type="number" min="1" value="1"
               class="bg-surface-container-low border-none rounded-DEFAULT h-10 text-center font-body-md text-on-surface w-full px-xs">`}
      </div>
      <div class="space-y-xs flex flex-col min-w-0">
        <label class="text-label-sm text-secondary truncate block h-4 mb-1">${amountLabel}</label>
        <input id="cc-amount-${c.id}-${mt}" type="number" step="any"
          class="bg-surface-container-low border-none rounded-DEFAULT h-10 text-center font-body-md text-on-surface w-full px-xs">
      </div>
    </div>`;
  }).join("");

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
    ${blocks}
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
  const entries = [];
  for (let i = 0; i < c.measurement_types.length; i++) {
    const mt = c.measurement_types[i], unit = c.units[i];
    const single = mt === "distance" || mt === "duration";
    const amount = parseFloat($(`cc-amount-${id}-${mt}`).value);
    const sets = single ? 1 : (parseInt($(`cc-sets-${id}-${mt}`).value, 10) || 1);
    if (!amount || amount <= 0) {
      toast(`Enter a ${CUSTOM_TYPE_LABEL[mt].toLowerCase()} value`, true);
      return;
    }
    entries.push({custom_exercise_id: id, amount, sets, unit});
  }
  try {
    const d = await api("POST", "/v1/activities", {entries});
    const anyImplausible = d.logged.some(l => l.implausible);
    toast(anyImplausible
      ? "Saved — some of that looks unusual, so it won't count towards progress"
      : `Saved · ${d.streak.current} day streak`, anyImplausible);
    loadHome();
  } catch (e) { toast(e.message, true); }
}

async function send(type, amount, sets, workoutSessionId, setAmounts) {
  try {
    // set_amounts (per-set breakdown) replaces amount/sets entirely when
    // given — the backend derives sets_done/amount_per_set from the array
    // itself (see app.profile.service.log_activity), so sending both would
    // be redundant/conflicting.
    const body = setAmounts ? {activity_type: type, set_amounts: setAmounts}
                            : {activity_type: type, amount, sets};
    if (workoutSessionId) body.workout_session_id = workoutSessionId;
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
    try { await elevenlabsTTS(text); return; }
    catch (e) { console.error("ElevenLabs TTS failed, falling back to free voice:", e); }
  }
  await speakWithBrowserTTS(text);
}

async function transcribeAudio(float32Array, sampleRate) {
  if (isElevenLabsEnabled()) {
    try { return await elevenlabsSTT(float32Array, sampleRate); }
    catch (e) {
      console.error("ElevenLabs STT failed, falling back to free transcription:", e);
      toast("Premium voice unavailable — using free transcription instead", true);
    }
  }
  return await transcribeWithWhisper(float32Array);
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

function finalizeVoiceSubmit() {
    if (voiceTranscript.trim()) {
        toast("Processing final transcript...");
        voiceSubmitTranscript(voiceTranscript);
    } else {
        toast("No speech detected.");
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
    try {
        voiceTranscript = "";
        audioChunks = [];

        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        mediaRecorder = new MediaRecorder(stream);

        mediaRecorder.ondataavailable = (e) => {
            if (e.data.size > 0) audioChunks.push(e.data);
        };

        mediaRecorder.onstop = async () => {
            // User hit stop, decode WebM to PCM AudioBuffer
            const blob = new Blob(audioChunks, { type: 'audio/webm' });
            const audioCtx = new window.AudioContext({ sampleRate: 16000 });
            const arrayBuffer = await blob.arrayBuffer();
            const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);
            const float32Array = audioBuffer.getChannelData(0);

            // Stop the tracks to release mic
            stream.getTracks().forEach(t => t.stop());

            try {
                voiceTranscript = await transcribeAudio(float32Array, 16000);
                finalizeVoiceSubmit();
            } catch (e) {
                toast("Transcription error: " + e.message, true);
                $("main-mic-icon").textContent = "mic";
                $("main-mic-fab").classList.remove("bg-surface-variant");
                $("main-mic-fab").classList.add("bg-primary-fixed");
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

        mediaRecorder.start();
        isWhisperRecording = true;

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
        showVoiceFallback("Microphone Error: " + (e.message || "Unknown error"));
    }
}

async function voiceSubmitTranscript(transcript) {
  toast("Extracting...");
  try {
    const res = await api("POST", "/v1/voice/extract", { transcript });
    if (res.failed || !res.entries || res.entries.length === 0) {
      showVoiceFallback("Extraction failed. Please type instead or enter manually.");
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
    const unit = e.unit === "count" ? "" : e.unit;
    return e.set_amounts.map((target, idx) => `
      <div class="flex items-center gap-1.5 cursor-pointer select-none" onclick="editVoiceValue(${i}, ${idx})" title="Tap to edit this value">
        <div class="w-14 h-11 rounded-xl flex items-center justify-center shrink-0 shadow-sm transition-all bg-white/70 border border-black/10">
          <span class="material-symbols-outlined text-[20px] text-secondary">edit</span>
        </div>
        <div class="flex-1 h-11 rounded-xl px-sm flex items-center justify-between shadow-sm border transition-all bg-white/75 border-black/5">
          <span class="text-xs font-bold text-on-background">${name} - Set ${idx + 1}</span>
          <span class="text-xs font-bold text-secondary">${target}${unit}</span>
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
      let customId = existingByName.get(e.activity_name.toLowerCase());
      if (!customId) {
        const measurementType = e.unit === "km" ? "distance" : e.unit === "min" ? "duration" : "reps";
        const created = await api("POST", "/v1/custom-exercises", {
          name: e.activity_name, measurement_types: [measurementType],
        });
        customId = created.id;
      }
      const sets = e.set_amounts.length;
      const amount = e.set_amounts.reduce((a, b) => a + b, 0) / sets;
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

function editVoiceLog() {
  cancelVoiceLog();
  startVoiceLogging();
}

function cancelVoiceLog() {
  $("voice-modal").classList.add("hidden");
  voiceEntries = [];
  voiceTranscript = "";
  if (isWhisperRecording) {
    toggleWhisperRecording(); // stop if running
  }
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
  // Found live: once every exercise was logged, this fell through to
  // "nothing to do" and left no way to open the player again for the rest
  // of the day — the one entry point for it (the dumbbell nav icon) is
  // never hidden now, so this needs a real fallback, not just a dead end. If
  // nothing's left to do, replay the full program instead of refusing.
  const todo = HOME_ITEMS.filter(i => !i.completed);
  const toPlay = todo.length ? todo : HOME_ITEMS;
  if (!toPlay.length) {
    // Program is completely empty — there is no separate exercise-list
    // screen to point at any more (it moved entirely into this Player), so
    // open the add-exercise catalogue directly instead of a dead-end toast.
    // addFromCatalogue's refreshPlayerItems() starts the workout automatically
    // once something's actually been added.
    openCatalogue();
    return;
  }
  try {
    const started = await api("POST", "/v1/workout-sessions",
      {activity_types: toPlay.map(i => i.activity_type)});
    PLAYER_ITEMS = toPlay;
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

function renderPlayerExercise() {
  const total = PLAYER_ITEMS.length;
  const i = PLAYER_ITEMS[playerIndex];
  $("pl-progress").innerHTML = PLAYER_ITEMS.map((_, idx) =>
    `<div class="flex-1 h-full bg-black/10 rounded-full overflow-hidden">
       <div class="h-full rounded-full bg-primary-fixed transition-all duration-300" style="width:${idx <= playerIndex ? 100 : 0}%"></div>
     </div>`
  ).join("");
  $("pl-counter").textContent = `${playerIndex + 1} of ${total}`;
  $("pl-icon").textContent = icon(i.activity_type);
  $("pl-title").textContent = i.display_name;
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
    $("pl-video-label").textContent = i.display_name;
    $("pl-video-footer-label").textContent = i.display_name;
  }
  $("pl-prev").disabled = playerIndex === 0;
  $("pl-next").disabled = playerIndex === total - 1;
  $("pl-rest").classList.add("hidden");
  $("pl-exercise-view").classList.remove("hidden");
  $("pl-done-view").classList.add("hidden");

  // Single-effort activities (distance/duration — Walking, Running: already
  // locked to exactly 1 "set" server-side) keep the old one-tap button.
  // Everything else (reps/hold) gets the real per-set slide table instead.
  const singleEffort = i.prescription_type === "distance" || i.prescription_type === "duration";
  $("pl-done-btn").classList.toggle("hidden", !singleEffort);
  $("pl-sets-table").classList.toggle("hidden", singleEffort);
  if (singleEffort) {
    $("pl-done-btn").disabled = false;
    $("pl-done-btn").textContent = "Mark Done";
  } else {
    playerSetDone = (i.set_targets || Array(i.sets).fill(i.amount)).map(() => false);
    renderSetsTable(i);
  }
}

function renderSetsTable(i) {
  const targets = i.set_targets || Array(i.sets).fill(i.amount);
  const unit = i.prescription_type === "hold" ? "s" : "";
  // Row shape matches Exercise workout Player.html's own sets table: a
  // dedicated toggle tile on the left (its tap/slide target) next to a tile
  // showing that set's own number and target. KG/RPE columns from the
  // mockup are dropped — this app doesn't track weight or perceived
  // exertion, and fabricating those fields would show data that was never
  // actually logged.
  $("pl-sets-table").innerHTML = targets.map((target, idx) => {
    const done = playerSetDone[idx];
    return `
    <div class="flex items-center gap-1.5 cursor-pointer select-none" onclick="playerToggleSet(${idx})" title="Tap to mark Set ${idx + 1} done">
      <div class="w-14 h-11 rounded-xl flex items-center justify-center shrink-0 shadow-sm transition-all
                  ${done ? "bg-primary-fixed" : "bg-white/70 border border-black/10"}">
        <span class="material-symbols-outlined text-[20px] ${done ? "text-on-primary-fixed" : "text-secondary"}">${done ? "check" : "chevron_right"}</span>
      </div>
      <div class="flex-1 h-11 rounded-xl px-sm flex items-center justify-between shadow-sm border transition-all
                  ${done ? "bg-white/80 border-primary/50" : "bg-white/75 border-black/5"}">
        <span class="text-xs font-bold text-on-background">Set ${idx + 1}</span>
        <span class="text-xs font-bold ${done ? "text-primary" : "text-secondary"}">${target}${unit}</span>
      </div>
    </div>`;
  }).join("");
}

function playerToggleSet(idx) {
  playerSetDone[idx] = !playerSetDone[idx];
  const i = PLAYER_ITEMS[playerIndex];
  renderSetsTable(i);
  if (playerSetDone.every(Boolean)) playerSubmitSets();
}

async function playerSubmitSets() {
  const i = PLAYER_ITEMS[playerIndex];
  const targets = i.set_targets || Array(i.sets).fill(i.amount);
  const result = await send(i.activity_type, null, null, playerSessionId, targets);
  if (!result) {
    // Logging failed — let the user try again rather than stranding them on
    // an all-checked table with nothing actually saved.
    playerSetDone = targets.map(() => false);
    renderSetsTable(i);
    return;
  }
  if (playerIndex === PLAYER_ITEMS.length - 1) {
    await finishPlayerSession();
  } else if (i.rest_seconds) {
    playerStartRest(i.rest_seconds);
  } else {
    playerGo(1);
  }
}

function playerGo(delta) {
  const next = playerIndex + delta;
  if (next < 0 || next >= PLAYER_ITEMS.length) return;
  playerIndex = next;
  renderPlayerExercise();
}

async function playerMarkDone() {
  const i = PLAYER_ITEMS[playerIndex];
  $("pl-done-btn").disabled = true;
  $("pl-done-btn").textContent = "Saving…";
  const result = await send(i.activity_type, i.amount, i.sets, playerSessionId);
  if (!result) {
    $("pl-done-btn").disabled = false;
    $("pl-done-btn").textContent = "Mark Done";
    return;
  }
  if (playerIndex === PLAYER_ITEMS.length - 1) {
    await finishPlayerSession();
  } else if (i.rest_seconds) {
    playerStartRest(i.rest_seconds);
  } else {
    playerGo(1);
  }
}

function playerStartRest(seconds) {
  playerRestRemaining = seconds;
  $("pl-rest").classList.remove("hidden");
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
  playerGo(1);
}

async function finishPlayerSession() {
  clearInterval(playerClockHandle);
  clearInterval(playerRestHandle);
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
      <button onclick="addFromCatalogue('${c.activity_type}')"
        class="shrink-0 ${c.verdict === 'avoid' ? 'bg-error-container text-on-error-container' : 'bg-primary text-on-primary'} px-md py-xs rounded-full font-label-bold text-label-bold hover:opacity-90">
        ${c.verdict === 'avoid' ? 'Blocked — add anyway' : 'Add'}</button>
    </div>`).join("")
    : `<p class="font-body-md text-on-surface-variant text-center py-lg">Everything is already on your dashboard.</p>`;
  const m = $("catalogue-modal");
  m.classList.remove("hidden");
  m.classList.add("flex");
}

function closeCatalogue() {
  const m = $("catalogue-modal");
  m.classList.add("hidden");
  m.classList.remove("flex");
}

async function addFromCatalogue(activityType, overrideConfirmed) {
  try {
    const d = await api("POST", "/v1/program/entries",
      { activity_type: activityType, override_confirmed: !!overrideConfirmed });
    if (d.needs_confirmation) {
      const ok = confirm(
        d.warning_text + "\n\nClick OK only if a doctor has advised you to do this. " +
        "Click Cancel to keep it blocked."
      );
      if (ok) await addFromCatalogue(activityType, true);
      return;
    }
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

    let d;
    if (conditionsChanged) {
      // Condition changes need the full safety-grid re-check (14.4), so this
      // one case still goes through onboarding rather than the quick-edit path.
      d = await api("POST", "/v1/profile/onboarding", {
        age: newAge, goal: editGoal, experience_band: editBand,
        unit_preference: editProfile.unit_preference, condition_codes: [...editPicked],
      });
    } else {
      // No condition change — use the lightweight edit (3.4), only send what moved.
      const changed = {};
      if (newAge !== editProfile.age) changed.age = newAge;
      if (editGoal !== editProfile.goal) changed.goal = editGoal;
      if (editBand !== editProfile.experience_band) changed.experience_band = editBand;
      if (Object.keys(changed).length) await api("PUT", "/v1/profile", changed);
      d = {};
    }
    closeProfileEditor();
    if (d.referred) {
      $("refer-msg").textContent = d.message;
      go("refer");
      $("nav").className = NAV_ON;
      return;
    }
    await loadProfile();
    toast("Profile updated");
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
  $("pw-reset-code").value = "";
  $("pw-err").className = "hidden";
  pwForgotMode = false;
  $("pw-current-wrap").classList.remove("hidden");
  $("pw-reset-wrap").classList.add("hidden");
  $("pw-forgot-link").textContent = "Forgot your password?";
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
  // Email delivery isn't wired up yet (the backend mailer is a stub that
  // silently drops every email in production) — this used to call the real
  // /password-reset/request endpoint and tell the user "a reset code was
  // sent," which was worse than saying nothing: the code was real, but it
  // went nowhere, and the user would wait for an email that could never
  // arrive. Until real email exists, be honest and point at the manual
  // path instead of pretending this works.
  $("pw-err").textContent =
    "Password reset by email isn't available yet. Please contact us and we'll reset it for you.";
  $("pw-err").className = "font-body-md text-on-error-container bg-error-container rounded px-sm py-xs";
}

async function savePassword() {
  try {
    if (pwForgotMode) {
      await api("POST", "/v1/auth/password-reset/confirm", {
        token: $("pw-reset-code").value.trim(),
        password: $("pw-new").value,
      });
    } else {
      await api("POST", "/v1/auth/change-password", {
        current_password: $("pw-current").value,
        new_password: $("pw-new").value,
      });
    }
    closePasswordEditor();
    // Either path revokes every session server-side (1.9 / this device's own
    // change) - the current session is dead now too, so return to login.
    localStorage.clear();
    AT = null; RT = null;
    go("auth");
    toast("Password updated — please log in again");
  } catch (e) {
    $("pw-err").textContent = e.message;
    $("pw-err").className = "font-body-md text-on-error-container bg-error-container rounded px-sm py-xs";
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
  const d = await api("GET", "/v1/videos");
  if (d.referred) {
    $("v-list").innerHTML = `<div class="bg-surface-container-lowest ambient-shadow rounded-lg p-lg font-body-md text-on-surface-variant">${d.message}</div>`;
    return;
  }
  $("v-list").innerHTML = d.videos.map(v => {
    const hasGif = !!v.demo_gif;
    const hasDemo = v.demo_images && v.demo_images.length;
    return `
    <article class="bg-surface-container-lowest rounded-lg p-3 ambient-shadow flex flex-col gap-sm">
      <div class="relative w-full aspect-video rounded-[14px] overflow-hidden bg-surface-container-low">
        ${hasGif
          ? `<img src="${assetUrl(v.demo_gif)}" alt="${v.display_name}" class="w-full h-full object-contain">`
          : hasDemo
          ? `<img id="demo-${v.activity_type}" src="${assetUrl(v.demo_images[0])}" alt="${v.display_name}"
                  class="w-full h-full object-contain">
             <span class="absolute bottom-2 right-2 bg-inverse-surface/80 text-inverse-on-surface font-label-sm text-label-sm px-2 py-1 rounded backdrop-blur-sm">
               ${v.demo_images.length} frames</span>`
          : `<div class="w-full h-full flex flex-col items-center justify-center gap-xs">
               <span class="material-symbols-outlined text-[40px] text-on-surface-variant">directions_walk</span>
               <span class="font-label-sm text-label-sm text-on-surface-variant">No demonstration needed</span>
             </div>`}
      </div>
      <div class="px-1 flex flex-col gap-2">
        <div class="flex gap-2 flex-wrap">
          <span class="px-2 py-1 bg-primary-fixed/20 text-on-primary-fixed-variant font-label-sm text-label-sm rounded-full">${v.prescription_type}</span>
          ${v.per_side ? '<span class="px-2 py-1 bg-surface-variant text-on-surface-variant font-label-sm text-label-sm rounded-full">each side</span>' : ''}
        </div>
        <h3 class="font-headline-sm text-headline-sm">${v.display_name}</h3>
        ${v.instructions && v.instructions.length ? `
          <ol class="font-body-md text-body-md text-on-surface-variant list-decimal pl-5 space-y-1">
            ${v.instructions.map(i => `<li>${i}</li>`).join("")}
          </ol>` : ""}
        ${v.watch_url ? `<a href="${v.watch_url}" target="_blank" class="inline-flex items-center gap-1 text-primary font-label-bold text-label-bold mt-1">
            Watch on YouTube <span class="material-symbols-outlined text-[16px]">open_in_new</span></a>` : ""}
      </div>
    </article>`;}).join("");

  // Alternate the start/end frames — this IS the animation. Two static images
  // looping beats hosting a GIF: smaller, already public domain, and it stops
  // when the tab is hidden so it costs nothing in the background.
  startDemoLoop(d.videos);
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
      <button onclick="go('relationships')" class="flex items-center justify-between py-sm text-left border-t border-black/5">
        <span class="flex items-center gap-sm font-body-lg text-body-lg text-on-background"><span class="material-symbols-outlined text-[20px] text-secondary">supervisor_account</span> Manage Plans for Others</span>
        <span class="material-symbols-outlined text-secondary">chevron_right</span>
      </button>
      <button onclick="openPasswordEditor()" class="flex items-center justify-between py-sm text-left border-t border-black/5">
        <span class="flex items-center gap-sm font-body-lg text-body-lg text-on-background"><span class="material-symbols-outlined text-[20px] text-secondary">lock_reset</span> Change Password</span>
        <span class="material-symbols-outlined text-secondary">chevron_right</span>
      </button>
      <button onclick="doLogout()" class="flex items-center justify-between py-sm text-left border-t border-black/5">
        <span class="flex items-center gap-sm font-body-lg text-body-lg text-primary font-semibold"><span class="material-symbols-outlined text-[20px]">logout</span> Log out</span>
        <span class="material-symbols-outlined text-secondary">chevron_right</span>
      </button>
    </section>

    <!-- Danger Zone — centered red pill, matching the mockup exactly. -->
    <button onclick="delAccount()" class="flex items-center justify-center gap-xs mx-auto mt-sm px-lg py-sm rounded-full text-error font-label-bold text-label-bold hover:bg-error-container/40 transition-colors">
      <span class="material-symbols-outlined text-[18px]">delete_forever</span> Delete Account
    </button>`;
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
  if (!confirm("Delete your account?")) return;
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

async function claimPendingInviteIfAny() {
  if (!PENDING_INVITE_TOKEN) return;
  const token = PENDING_INVITE_TOKEN;
  sessionStorage.removeItem("pendingInvite");
  PENDING_INVITE_TOKEN = null;
  try {
    await api("POST", "/v1/relationships/claim", {invite_token: token});
    toast("You're linked — they can now see and help manage your plan.");
  } catch (e) {
    toast(e.message || "That invite link didn't work.", true);
  }
}

async function loadRelationships() {
  const { relationships } = await api("GET", "/v1/relationships");
  $("rel-list").innerHTML = relationships.length ? relationships.map(r => {
    const openFn = r.status === "active" ? `openRelationshipDetail('${r.id}')`
      : r.status === "pending" ? `openPendingBuild('${r.id}', '${r.invite_token}')` : null;
    const clickable = openFn ? `cursor-pointer" onclick="${openFn}` : "";
    return `
    <div class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-md flex items-center gap-sm">
      <div class="w-10 h-10 rounded-full bg-primary-container flex items-center justify-center text-on-primary-container shrink-0 ${clickable}">
        <span class="material-symbols-outlined">${RELATION_ICONS[r.relation_type] || 'person'}</span>
      </div>
      <div class="flex-1 min-w-0 ${clickable}">
        <h4 class="font-label-bold text-label-bold text-on-surface">${RELATION_LABELS[r.relation_type] || r.relation_type}</h4>
        <p class="font-label-sm text-label-sm text-secondary">${r.status === 'pending' ? 'Not accepted yet — tap to set up their plan' : r.status === 'revoked' ? 'Revoked' : 'Active'}</p>
      </div>
      ${r.status === 'pending' ? `<button onclick="event.stopPropagation(); copyInviteLink('${r.invite_token}')" class="shrink-0 bg-primary text-on-primary px-md py-xs rounded-full font-label-bold text-label-bold">Copy link</button>` : ''}
      ${r.status !== 'revoked' ? `<button onclick="event.stopPropagation(); revokeRelationship('${r.id}')" aria-label="Revoke" class="shrink-0 text-secondary hover:text-error"><span class="material-symbols-outlined text-[20px]">close</span></button>` : ''}
    </div>`;
  }).join("")
    : `<p class="font-body-md text-on-surface-variant text-center py-lg">You're not managing anyone's plan yet. Tap "New" to invite a child, student, or patient.</p>`;
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
  const m = $("invite-modal"); m.classList.remove("hidden"); m.classList.add("flex");
}
function closeCreateInviteModal() {
  const m = $("invite-modal"); m.classList.add("hidden"); m.classList.remove("flex");
}

async function createRelationshipInvite(relationType) {
  try {
    const r = await api("POST", "/v1/relationships", {relation_type: relationType});
    closeCreateInviteModal();
    // Build the plan FIRST, get the link after — so the moment it's
    // claimed, the person sees it already set up instead of an empty
    // account waiting on a second trip back from the manager.
    openPendingBuild(r.id, r.invite_token);
  } catch (e) { toast(e.message, true); }
}

let CURRENT_INVITE_TOKEN = null;

async function openPendingBuild(relationshipId, inviteToken) {
  CURRENT_RELATIONSHIP = relationshipId;
  CURRENT_IS_PENDING = true;
  CURRENT_INVITE_TOKEN = inviteToken;
  CURRENT_MANAGED_USER_ID = null;
  await renderPendingBuild();
  go("relationship-detail");
}

async function renderPendingBuild() {
  const { entries } = await api("GET", `/v1/relationships/${CURRENT_RELATIONSHIP}/pending-entries`);
  $("rd-title").textContent = "Set Up Their Plan";
  const entriesHtml = entries.length ? entries.map(e => `
    <div class="flex items-center justify-between py-sm border-t border-black/5">
      <div class="min-w-0">
        <p class="font-body-lg text-body-lg text-on-background">${escapeHtml(e.activity_type)}</p>
        <p class="font-label-sm text-label-sm text-secondary">${e.target_sets} x ${e.target_amount} · ${e.auto_progress ? "auto-adjusting" : "fixed"}</p>
      </div>
      <button onclick="removePendingEntry('${e.activity_type}')" aria-label="Remove"
        class="shrink-0 text-secondary hover:text-error"><span class="material-symbols-outlined text-[20px]">close</span></button>
    </div>`).join("")
    : `<p class="font-body-md text-on-surface-variant py-sm">Add at least one activity, then share the link — they'll see this plan the moment they sign up.</p>`;

  $("rd-body").innerHTML = `
    <section class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg flex flex-col gap-1">
      <h3 class="font-headline-sm text-headline-sm text-on-background mb-xs">Activities</h3>
      ${entriesHtml}
    </section>
    <button onclick="copyInviteLink('${CURRENT_INVITE_TOKEN}')"
      class="bg-primary text-on-primary rounded-full py-md font-label-bold text-label-bold flex items-center justify-center gap-xs">
      <span class="material-symbols-outlined text-[18px]">link</span> Get the Link to Share
    </button>`;
}

async function removePendingEntry(activityType) {
  try {
    await api("DELETE", `/v1/relationships/${CURRENT_RELATIONSHIP}/pending-entries/${activityType}`);
    await renderPendingBuild();
  } catch (e) { toast(e.message, true); }
}

async function revokeRelationship(id) {
  if (!confirm("Stop managing this plan? They keep their current activities and targets exactly as they are — you just won't be able to see or edit them anymore.")) return;
  try {
    await api("DELETE", `/v1/relationships/${id}`);
    await loadRelationships();
  } catch (e) { toast(e.message, true); }
}

let CURRENT_RELATIONSHIP = null;
let CURRENT_MANAGED_USER_ID = null;
let CURRENT_IS_PENDING = false;

async function openRelationshipDetail(id) {
  try {
    const d = await api("GET", `/v1/relationships/${id}/activity`);
    CURRENT_RELATIONSHIP = id;
    CURRENT_MANAGED_USER_ID = d.managed_user_id;
    CURRENT_IS_PENDING = false;
    renderRelationshipDetail(d);
    go("relationship-detail");
  } catch (e) { toast(e.message, true); }
}

function renderRelationshipDetail(d) {
  $("rd-title").textContent = `Their Plan (${RELATION_LABELS[d.relation_type] || d.relation_type})`;
  const streak = d.streak.current || 0;
  const entries = d.managed_entries || [];
  const overrides = d.safety_overrides || [];

  const entriesHtml = entries.length ? entries.map(e => `
    <div class="flex items-center justify-between py-sm border-t border-black/5">
      <div class="min-w-0">
        <p class="font-body-lg text-body-lg text-on-background">${escapeHtml(e.activity_type)}</p>
        <p class="font-label-sm text-label-sm text-secondary">${e.target_sets} x ${e.target_amount} · ${e.auto_progress ? "auto-adjusting" : "fixed"}</p>
      </div>
      <button onclick="editManagedTarget('${e.activity_type}', ${e.target_sets}, ${e.target_amount})"
        class="shrink-0 bg-black/5 hover:bg-black/10 px-md py-xs rounded-full font-label-bold text-label-bold text-on-background">Edit</button>
    </div>`).join("")
    : `<p class="font-body-md text-on-surface-variant py-sm">You haven't added anything to their plan yet.</p>`;

  $("rd-body").innerHTML = `
    <section class="bg-white/80 backdrop-blur-2xl border border-white/90 ambient-shadow rounded-2xl p-lg flex flex-col gap-sm">
      <div class="flex items-center justify-between">
        <p class="font-body-md text-on-surface-variant">Current streak</p>
        <p class="font-headline-sm text-headline-sm text-on-background">${streak} day${streak === 1 ? "" : "s"}</p>
      </div>
    </section>

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

async function editManagedTarget(activityType, currentSets, currentAmount) {
  const sets = window.prompt(`Sets for ${activityType}:`, currentSets);
  if (sets === null) return;
  const amount = window.prompt(`Amount per set for ${activityType}:`, currentAmount);
  if (amount === null) return;
  try {
    await api("PUT", `/v1/program/entries/${activityType}/target`, {
      managed_user_id: CURRENT_MANAGED_USER_ID,
      target_sets: parseInt(sets, 10), target_amount: parseFloat(amount),
    });
    toast("Updated");
    await openRelationshipDetail(CURRENT_RELATIONSHIP);
  } catch (e) { toast(e.message, true); }
}

async function openManagedAddModal() {
  if (!CURRENT_RELATIONSHIP) return;
  try {
    const { activities } = await api("GET", "/v1/taxonomy/activities");
    $("ma-activity").innerHTML = activities.map(a =>
      `<option value="${a.activity_type}">${escapeHtml(a.display_name)}</option>`).join("");
    // Custom exercises need a real, already-existing account to attach to —
    // not available yet while the invite is still pending (no managed_user_id).
    $("ma-custom-section").classList.toggle("hidden", CURRENT_IS_PENDING);
    const m = $("managed-add-modal"); m.classList.remove("hidden"); m.classList.add("flex");
  } catch (e) { toast(e.message, true); }
}
function closeManagedAddModal() {
  const m = $("managed-add-modal"); m.classList.add("hidden"); m.classList.remove("flex");
}

async function submitManagedAdd(overrideConfirmed) {
  const activityType = $("ma-activity").value;
  const sets = parseInt($("ma-sets").value, 10) || 1;
  const amount = parseFloat($("ma-amount").value);
  if (!amount || amount <= 0) { toast("Enter an amount greater than zero.", true); return; }
  try {
    if (CURRENT_IS_PENDING) {
      // Staged against the invite itself — materializes automatically the
      // moment it's claimed (app/relationships/service.py's claim_invite).
      await api("POST", `/v1/relationships/${CURRENT_RELATIONSHIP}/pending-entries`, {
        activity_type: activityType, target_sets: sets, target_amount: amount,
        auto_progress: $("ma-auto").checked,
      });
      closeManagedAddModal();
      toast("Added to their plan");
      await renderPendingBuild();
      return;
    }
    // add_program_entry always seeds the taxonomy's own default sets/amount
    // for a brand-new entry — it has no notion of "start it at this specific
    // number" itself. The follow-up PUT right below is what actually makes
    // this form's sets/amount values stick.
    const d = await api("POST", "/v1/program/entries", {
      activity_type: activityType, managed_user_id: CURRENT_MANAGED_USER_ID,
      auto_progress: $("ma-auto").checked, override_confirmed: !!overrideConfirmed,
    });
    if (d.needs_confirmation) {
      const ok = confirm(
        d.warning_text + "\n\nClick OK only if this has been clinically advised. " +
        "Click Cancel to keep it blocked."
      );
      if (ok) await submitManagedAdd(true);
      return;
    }
    await api("PUT", `/v1/program/entries/${activityType}/target`, {
      managed_user_id: CURRENT_MANAGED_USER_ID, target_sets: sets, target_amount: amount,
    });
    closeManagedAddModal();
    toast("Added to their plan");
    await openRelationshipDetail(CURRENT_RELATIONSHIP);
  } catch (e) { toast(e.message, true); }
}

async function submitManagedCustomAdd() {
  const name = $("ma-custom-name").value.trim();
  if (!name) { toast("Type a name first", true); return; }
  try {
    await api("POST", "/v1/custom-exercises", {
      name, measurement_types: [$("ma-custom-type").value],
      managed_user_id: CURRENT_MANAGED_USER_ID,
    });
    $("ma-custom-name").value = "";
    closeManagedAddModal();
    toast("Custom exercise added for them");
    await openRelationshipDetail(CURRENT_RELATIONSHIP);
  } catch (e) { toast(e.message, true); }
}

// Every function referenced from an inline onclick="..." in index.html must be
// reachable on window — a Vite module's top-level functions are scoped to the
// module, not attached to the global object the way a plain <script> tag's
// were in the original single-file version.
Object.assign(window, {
  doAuth, submitOnboarding, selectActivityPeriod,
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
  revokeRelationship, openRelationshipDetail, editManagedTarget,
  openManagedAddModal, closeManagedAddModal, submitManagedAdd, submitManagedCustomAdd,
  openPendingBuild, removePendingEntry,
});

(async () => {
  // Found live: opening an invite link on the SAME browser where the
  // manager is already logged in silently continued as the manager's own
  // account (AT was already set, so this skipped straight past the login
  // screen) — the exact "it's using my account instead of asking the
  // student/patient/child to sign in as themselves" bug. An invite link
  // must always show the login/signup screen, regardless of who (if anyone)
  // is already authenticated on this device, so the actual recipient signs
  // up or logs in as THEIR OWN account before the link gets claimed.
  if (PENDING_INVITE_TOKEN) {
    toast("Sign up or log in as the person this plan is for.");
    go("auth");
    return;
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
