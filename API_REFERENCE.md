# ViTAinspire API Reference

The single source of truth for building the Flutter client against this backend.
Every endpoint below was verified directly against the current route and service
code (not written from memory) — if behavior ever seems to disagree with this
doc, trust the code and treat this file as needing an update.

**Base URL (local dev):** `http://127.0.0.1:5057`
**Content type:** `application/json` for every request and response body.
**There is no web client.** This is a pure JSON API for a mobile app.

---

## 1. Conventions that apply to every endpoint

### Auth header
Any endpoint marked **🔒 Auth required** needs:
```
Authorization: Bearer <access_token>
```
Missing or invalid → `401 unauthorized`.

### Device header
Any endpoint marked **📱 Device required** needs:
```
X-Device-Id: <a stable per-install identifier>
```
Generate this once per app install and persist it (e.g. secure storage) — it must
stay the same across sessions. Missing → `422 validation_failed`. This is never
allowed to be an IP address or anything server-derived; it identifies the
*install*, which is what refresh-token rotation and rate limiting are keyed on.

### Standard error shape
Every error, from every endpoint, looks like this:
```json
{ "error": { "code": "validation_failed", "message": "Human-readable reason" } }
```

| HTTP status | `code` | Meaning | Client should |
|---|---|---|---|
| 400 | `bad_request` | Malformed request | Fix the request |
| 401 | `unauthorized` | Missing/invalid/expired access token | Try a silent refresh, then re-login if that also fails |
| 403 | `revoked` | Token was explicitly revoked / session killed | Force re-login |
| 403 | `forbidden` | Not allowed (e.g. referred user hitting a gated endpoint) | Show the reason message, do not retry |
| 404 | `not_found` | Resource or route doesn't exist | — |
| 409 | `conflict` | State conflict | — |
| 422 | `validation_failed` | Bad input | Show `message` to the user, it's already human-readable |
| 429 | `rate_limited` | Too many requests | Respect the `Retry-After` header (seconds) |
| 429 | `quota_exhausted` | Daily rehab-chat quota used up | Show `message`, offer to try again after `resets_at` (see §6 quota) |
| 500 | `internal_error` | Server bug | Generic "something went wrong" — never shows internals |

### Pagination / lists
Nothing in this API paginates yet — list endpoints return everything relevant
(they're all naturally small: a user's own program, conditions, history).

---

## 2. Auth (Module 1) — `/v1/auth/*`

None of these need `Authorization`. Signup/login/refresh need `X-Device-Id` 📱.

### `POST /v1/auth/signup`
📱 Device required
```json
// Request
{ "email": "user@example.com", "password": "at least 10 characters" }
```
```json
// 201 Response
{
  "access_token": "...", "refresh_token": "...",
  "access_expires_at": "2026-08-24T13:31:52.265215+00:00",
  "refresh_expires_at": "2026-09-20T13:01:52.265215+00:00",
  "user_id": "uuid"
}
```
A verification email is conceptually sent (Module 8 mailer isn't wired up yet —
in dev, the token is only logged server-side, never returned in the response).

### `POST /v1/auth/login`
📱 Device required
```json
// Request
{ "email": "...", "password": "..." }
```
Response: same token-pair shape as signup, minus `user_id`.

### `POST /v1/auth/refresh`
📱 Device required. Call this proactively before the access token expires
(check `access_expires_at`) — a real device with a shaky connection is exactly
what the sliding refresh window (30 days, 180-day absolute cap) is for.
```json
// Request
{ "refresh_token": "..." }
```
Response: a fresh token pair, same shape as login. **The old refresh token is
rotated out** — always store the new one, the old one stops working (with a
short ~30s grace window for a request that was already in flight).

### `POST /v1/auth/logout`
🔒 Auth required
```json
// Request (refresh_token optional — omit to only kill the access token's session)
{ "refresh_token": "..." }
```
`200 { "status": "signed_out" }`

### `POST /v1/auth/verify-email`
```json
{ "token": "..." }
```
`200 { "status": "verified" }`

### `POST /v1/auth/password-reset/request`
📱 Device required
```json
{ "email": "..." }
```
`200 { "status": "ok", "message": "..." }` — **always** this response, whether
or not the email exists (prevents account enumeration). The reset token itself
is only logged server-side in dev; a real mailer isn't wired up yet.

### `POST /v1/auth/password-reset/confirm`
📱 Device required
```json
{ "token": "...", "password": "new password, 10+ chars" }
```
`200 { "status": "password_updated" }`

### `POST /v1/auth/change-password`
🔒 Auth required
```json
{ "current_password": "...", "new_password": "..." }
```
`200 { "status": "password_updated" }`

### `DELETE /v1/account`
🔒 Auth required. Schedules account deletion (not immediate). Requires the
current password to be re-confirmed — the same re-authentication step
`change-password` already requires, since this is the single most
destructive action in the app.
```json
{ "password": "..." }
```
`200 { "status": "deletion_scheduled" }` · `422` if the password is wrong.

---

## 3. Profile & Onboarding (Module 3) — `/v1/*`

### `GET /v1/conditions`
No auth — the client needs this to render onboarding before an account exists.
```json
{ "conditions": [
  { "code": "chronic_back_pain", "name": "Chronic back pain", "bucket": "supported", "notes": null },
  { "code": "heart_condition", "name": "Heart condition", "bucket": "refer", "notes": "..." }
]}
```
`bucket` is either `"supported"` (app gives guidance) or `"refer"` (app refuses
all exercise content and points the user to a doctor — see §4's referral gate).
**Never let a user type a free-text condition** — always pick from this list.

### `GET /v1/taxonomy/activities`
No auth. The full curated exercise catalogue (used to build UI pickers, etc.).
```json
{ "activities": [{
  "activity_type": "glute_bridge", "display_name": "Glute Bridge",
  "prescription_type": "reps",           // "reps" | "hold" | "distance" | "duration"
  "default_sets": 2, "default_amount": 5.0, "amount_unit": "count",
  "per_side": false, "rest_seconds": 30, "synonyms": ["glute bridge", "hip raise"]
}]}
```

### `POST /v1/profile/onboarding`
🔒 Auth required. Call once right after signup. **This also auto-builds a full
default exercise program** for the user (unless they picked a `refer`-bucket
condition, in which case no program is created at all).
```json
// Request
{
  "age": 28, "goal": "health",              // "health"|"fitness"|"flexibility"|"habit"
  "experience_band": "beginner",             // "beginner"|"1m"|"2m"|"3m"|"experienced"
  "condition_codes": ["chronic_back_pain"],  // [] means "none" — never omit the field
  "unit_preference": "km"                    // "km"|"mi"
}
```
```json
// 200 Response
{ "referred": false, "message": null, "program": [ /* same shape as GET /v1/program */ ] }
```
If a `refer`-bucket condition was picked: `{"referred": true, "message": "<referral text>", "program": []}`.
**Age must be a whole number from 1 to 120** (`422` otherwise). There is deliberately no adult-only age floor.
**Omitting `condition_codes` is a `422`** (send `[]` for none). If `experience_band` differs from the one on file, the
plan is rescaled (see "Changes since this document was verified" at the end).

### `GET /v1/profile`
🔒 Auth required.
```json
{
  "onboarded": true, "age": 28, "goal": "health", "experience_band": "beginner",
  "unit_preference": "km",
  "conditions": [{"code": "chronic_back_pain", "name": "Chronic back pain", "bucket": "supported"}],
  "referred": false
}
```

### `PUT /v1/profile`
🔒 Auth required. Send only the fields you want to change — conditions are
**not** editable here (see the rehab chat's condition-confirmation flow, §6,
which is the only way conditions change after onboarding).
```json
// Request (any subset)
{ "age": 29, "goal": "fitness", "experience_band": "1m", "unit_preference": "mi" }
```
Response: same shape as `GET /v1/profile`.

---

## 4. Program & Activity Logging (Modules 4, 14) — `/v1/*`

**Referral gate applies throughout this section**: if the user has a
`refer`-bucket condition, `GET /v1/program` silently returns `[]`, and anything
that tries to *add* to the program raises `403 forbidden` with a message to see
a doctor. Logging activity and viewing history still work — the referral gate
blocks new exercise *content*, not a user's own record-keeping.

### `GET /v1/program`
🔒 Auth required. The user's current active program.
```json
{ "program": [{
  "activity_type": "glute_bridge", "display_name": "Glute Bridge",
  "prescription_type": "reps", "sets": 2, "amount": 5.0, "unit": "count",
  "per_side": false, "rest_seconds": 30,
  "prescription": "2 x 5",                 // human-readable, ready to show directly
  "total_volume": 10.0,
  "est_minutes": null,                     // only set for distance activities
  "status": "active",                      // "active" | "suspended"
  "caution": null,                         // set if the safety grid says "modify" for this user
  "demo_gif": "/static/demos/glute_bridge.gif",   // relative path, or null
  "demo_images": ["https://..."],
  "instructions": ["Step 1...", "Step 2..."],
  "priority_order": 2
}]}
```

### `GET /v1/program/catalogue`
🔒 Auth required. Everything NOT already on the dashboard, for an "add more
exercises" screen.
```json
{ "catalogue": [{
  "activity_type": "running", "display_name": "Running", "prescription_type": "distance",
  "default_sets": 1, "default_amount": 1.0, "amount_unit": "km", "per_side": false,
  "verdict": "safe",          // "safe" | "modify" | "avoid" — for THIS user's conditions
  "caution": null             // the safety-grid note, if verdict isn't "safe"
}]}
```

### `POST /v1/program/entries`
🔒 Auth required. Add an activity from the catalogue.
```json
// Request
{ "activity_type": "running", "override_confirmed": false }
```
**Two possible 200 shapes:**
- Normal add: `{ "added": true, "needs_confirmation": false, "program": [...] }`
- The safety grid says `avoid` for this user, and `override_confirmed` was
  `false` — **the app must show a blocking confirmation dialog with the exact
  returned text**, then resend the same request with `override_confirmed: true`
  only if the user explicitly accepts:
  ```json
  {
    "added": false, "needs_confirmation": true,
    "condition_code": "herniated_disk", "condition_name": "Herniated disk",
    "verdict": "avoid",
    "warning_text": "Your herniated disk condition means this exercise is normally avoided. ..."
  }
  ```

### `DELETE /v1/program/entries/<activity_type>`
🔒 Auth required. Always allowed (even for a referred user's existing entries).
`200 { "removed": true, "program": [...] }`

### `GET /v1/suggestions`
🔒 Auth required. What to show on the home/today screen.
```json
{
  "referred": false, "message": null,
  "items": [ /* top 3, same shape as GET /v1/program entries, plus: */ ],
  "all_items": [ /* full list, same shape */ ],
  "tier": "beginner",
  "tier_note": "2 exercise(s) left out based on your health conditions"   // or null
}
```
Each item additionally has: `"completed": true|false` (logged today) and
`"video_url"` (string or `null`) and `"tier"`: `"default"|"optional"|"added"`.

### `POST /v1/activities`
🔒 Auth required. Log one or more completed activities in a single call.
```json
// Request — either a single entry directly, or {"entries": [...]}
{
  "activity_type": "glute_bridge",     // XOR custom_exercise_id — exactly one
  "custom_exercise_id": null,
  "amount": 5, "sets": 2,
  "unit": null,                         // omit to use the activity's own default unit
  "source": "manual",                   // "manual" | "voice"
  "client_entry_id": "some-uuid-you-generate"  // ⚠️ see idempotency note below
}
```
```json
// 201 Response
{ "logged": [{
    "id": "uuid", "duplicate": false, "implausible": false,
    "total_volume": 10.0,
    "prescription": "2 x 5"            // human-readable, ready to show back to the user
  }],
  "streak": { "current": 3, "last_active": "2026-08-24", "active_days": 12, "longest": 7 } }
```
**`client_entry_id` is how offline logging works.** Generate a real UUID
client-side *before* attempting the network call, store it with the queued
log entry, and always send the same one on retry. If the server already saw
that id for this user, it returns `"duplicate": true` instead of double-logging
— this is what makes it safe to retry a log that might have actually
succeeded before the connection dropped.
**`implausible: true`** means the amount was absurdly high for this activity —
the entry is still kept (never silently rejected), just flagged and excluded
from the weekly growth calculation.

### `GET /v1/dashboard/trend?days=15` (or `?start=YYYY-MM-DD&end=YYYY-MM-DD`)
🔒 Auth required. Daily log **counts** (not which exercises) for the chart.
```json
{ "trend": [{ "date": "2026-08-10", "label": "M", "count": 3 }, ...] }
```

### `GET /v1/dashboard/activity-detail?days=7` (or `?start=&end=`)
🔒 Auth required. What `trend` doesn't give you: the actual itemized entries.
```json
{ "entries": [{
  "date": "2026-08-24", "name": "Glute Bridge",
  "sets": 2, "amount": 5.0, "unit": "count", "total_volume": 10.0
}]}
```
Default range is the last 7 days if no query params are given.

### `GET /v1/dashboard/summary`
🔒 Auth required. Everything the home screen needs in one call.
```json
{
  "streak": { "current": 3, "last_active": "2026-08-24", "active_days": 12, "longest": 7 },
  "week": [ /* same shape as dashboard/trend, fixed 7 days */ ],
  "suggestions": { /* same shape as GET /v1/suggestions */ },
  "progression": [{
    "activity_type": "walk", "display_name": "Walking",
    "points": [{
      "applied_at": "2026-08-24T12:00:00+00:00",
      "curve_pct": 10.4,        // % the growth math wanted, before rounding
      "adherence_pct": 100.0,
      "old_target": 1.0, "new_target": 1.2,
      "reason": "curve"          // "curve"|"hold"|"reduce"|"floor"|"escalation"
    }]
  }]
}
```
`progression` is `[]` until the weekly backend job has run at least once for
this user — don't render an empty chart as an error, just hide that section.

---

## 5. Custom Exercises (Module 4.4) — `/v1/custom-exercises`

Personal, tracking-only exercises. **Never suggested, never safety-graded,
never appear in the program** — purely a way to log something outside the
curated catalogue.

### `GET /v1/custom-exercises`
🔒 Auth required.
```json
{ "custom_exercises": [{
  "id": "uuid", "name": "Swimming",
  "measurement_types": ["duration"],      // 1-3 of "reps"|"distance"|"duration"
  "units": ["min"],                       // matching unit per measurement_type
  "completed_today": false
}]}
```

### `POST /v1/custom-exercises`
🔒 Auth required.
```json
{ "name": "Swimming", "measurement_types": ["duration"] }
```
`201`, same shape as one item above. Max 20 per user; name must not collide
with a real catalogue activity or an existing custom one.

### `PUT /v1/custom-exercises/<id>`
🔒 Auth required. `{ "name": "New name" }` → same shape, renamed.

### `DELETE /v1/custom-exercises/<id>`
🔒 Auth required. Soft delete — past logs against it keep displaying normally.
`200 { "deleted": true }`

To **log** against a custom exercise, use `POST /v1/activities` with
`custom_exercise_id` instead of `activity_type` (see §4).

---

## 6. Rehab & Pain Guidance Chat + RLM — `/v1/rehab/*`

This is the single most stateful part of the API — read this section fully
before building the chat screen.

### `GET /v1/rehab/quota`
🔒 Auth required. Check before showing the composer, and after every message.
```json
{
  "limit": 15, "used": 4, "remaining": 11,
  "resets_at": "2026-08-25T00:00:00+05:30",
  "referred": false, "referral_message": null
}
```

### `GET /v1/rehab/history`
🔒 Auth required. The full past transcript — call once on chat screen load to
restore it; there is no separate "conversation id," there is exactly one
conversation per user, forever.
```json
{ "history": [
  { "question": "what causes gout", "answer": "...", "created_at": "2026-08-20T12:00:00+00:00" }
]}
```

### `POST /v1/rehab/query`
🔒 Auth required. **One endpoint, three possible request shapes** depending on
what the user is doing:

**A. A fresh question:**
```json
{ "question": "what causes gout" }
```

**B. Confirming (or declining) a condition the bot suspected mid-conversation**
— only send this in direct response to a `needs_condition_selection: true`
reply (see below); never speculatively:
```json
{ "question": "<the SAME original_question the server gave you>",
  "condition_confirmation": { "code": "carpal_tunnel" } }
```
Omit `code` (or send `condition_confirmation: {}`) to mean "no, that's not it."

**C. Confirming (or declining) a growth-increase proposal** — a question-less
action, only send in response to `needs_escalation_confirmation: true`:
```json
{ "escalation_confirmation": { "activity_type": "walk", "confirmed": true } }
```
This path does **not** consume quota — it's a UI action, not a new question.

**The response is always this same shape**, with different fields populated
depending on which of the above happened:
```json
{
  "referred": false,
  "message": null,
  "answer": "Foods that can trigger a gout flare include...",
  "needs_condition_selection": false,
  "condition_options": null,
  "needs_escalation_confirmation": false,
  "escalation_proposal": null,
  "quota": { /* same shape as GET /v1/rehab/quota */ }
}
```

**Field-by-field, what to actually render:**

| Field | When set | What to do |
|---|---|---|
| `referred: true` | User has a refer-bucket condition | Show `message`, disable the composer entirely — never even offer to type |
| `needs_condition_selection: true` | The bot thinks this message describes a new, undiagnosed condition | Show a tappable confirm/deny UI using `condition_options` (below), never let it silently auto-confirm |
| `condition_options` | (with the above) | `{"prompt_text": "...", "matched_code": "carpal_tunnel", "matched_label": "Carpal Tunnel Syndrome", "original_question": "..."}` — on tap, resend as shape B above, echoing `original_question` back exactly |
| `needs_escalation_confirmation: true` | The RLM found the user is consistently exceeding their target | Show `escalation_proposal.prompt_text` as a yes/no prompt |
| `escalation_proposal` | (with the above) | `{"activity_type": "walk", "display_name": "Walking", "proposed_pct_increase": 0.05, "adherence_pct_snapshot": 100.0, "reason": "...", "prompt_text": "..."}` — on tap "yes", resend as shape C above |
| `answer` | Normal reply | Render as the bot's chat bubble. Can be `null` when a confirmation is still pending (nothing to say yet) |
| `quota` | Always | Update your quota display every time |

**Escalation confirmation's own extra fields** (only present on a shape-C
response):
```json
{ ..., "escalation_applied": true, "new_target": 1.26 }
```
`escalation_applied: false` with a `message` means it either was declined, or
the offer expired because the user's numbers changed since it was shown —
either way, don't retry automatically, just tell the user.

**Errors specific to this endpoint:**
- `429 quota_exhausted` — out of questions for today. `escalation_confirmation`
  requests are exempt from this.
- `429 rate_limited` with `retry_after` — the AI provider was briefly
  overloaded; **this attempt was not counted against quota**, safe to retry
  after the given delay.

**What this endpoint can talk about**, so you can set UI expectations:
questions about a condition/pain/exercise guidance (grounded in a reviewed
medical knowledge base — 84 topics), the user's own performance/growth trend
("how am I doing", "analyze my growth"), and literal recall of their own
logged history ("what did I do yesterday", "what about last Tuesday"). It will
never name a specific medication, dose, or surgical procedure — by design, not
a gap to work around client-side.

---

## 7. Videos (Module 6) — `/v1/videos`

### `GET /v1/videos`
🔒 Auth required.
```json
{
  "referred": false, "message": null,
  "videos": [{
    "activity_type": "walk", "display_name": "Walking", "title": "How to walk with good form",
    "video_id": "abc123XYZ90", "curated": true,
    "watch_url": "https://www.youtube.com/watch?v=abc123XYZ90",
    "prescription_type": "distance", "per_side": false,
    "demo_images": ["https://..."], "demo_gif": null, "demo_gif_source": null,
    "instructions": ["..."]
  }]
}
```
`curated: false` means no one's picked a real video yet — `video_id`/`watch_url`
will be `null`; fall back to `demo_images`/`demo_gif`.

### `PUT /v1/admin/videos/<activity_type>` — staff only, not for the app
🔒 Auth required **and** the account must have the staff flag; anyone else gets
`403 forbidden`. Listed here only for completeness — an internal curation tool
for pasting an approved YouTube link, never something the mobile client calls.
Request `{ "url": "<youtube url or bare 11-char id>", "title": "optional" }` →
`200 { "activity_type": "...", "video_id": "..." }`.

---

## 8. Unauthenticated / infra endpoints

### `GET /healthz`
`200 { "status": "ok", "env": "development" }` — for your own smoke-testing, not app UI.

### `GET /v1/app/config`
Call on app launch, before login. Used for forced-update gating.
```json
{ "minimum_supported_version": "1.0.0", "soft_update_version": "1.0.0" }
```
If the installed app version is below `minimum_supported_version`, block usage
and force an update. Below `soft_update_version`, show a dismissible nudge.

---

## 9. Things that don't have endpoints yet (don't build UI assuming they exist)

- **Push notifications** — no send mechanism is wired up; a `device_push_tokens`
  table exists but nothing writes to it yet. Don't build a token-registration
  call against this API until told it's ready.
- **Per-exercise discomfort reporting** ("this one hurt") — not built.
- **Calibration** (finding the right starting level for a newly added exercise) — not built.
- **Email delivery** — verification/reset tokens are real and work, but are
  currently only logged server-side in dev, never emailed.

---

## 10. A note on staleness

**Verified 2026-08-24** against the live route map (`app.url_map`) and the
service functions behind each endpoint. That pass checked: every path and HTTP
method, every documented request/response field name, all error codes and
statuses, and the "doesn't exist yet" list in §9. Four corrections came out of
it — three response fields that had been missed (`streak.longest`,
`logged[].prescription`, `escalation_proposal.adherence_pct_snapshot`) and the
staff-only admin videos endpoint, all now fixed above.

Worth knowing *why* one of those was wrong: `streak.longest` was added to the
backend **during the same session this doc was written**, hours after the
section describing it. That's the failure mode to expect here — not sloppiness,
just a fast-moving backend. Before leaning hard on any single field, grepping
the actual route file (`app/*/routes.py`) or its service function costs seconds
and settles it.

To re-verify the route list wholesale at any point:
```bash
python -c "
from app import create_app
app = create_app()
for r in sorted(app.url_map.iter_rules(), key=str):
    if r.endpoint != 'static':
        print(sorted(r.methods - {'HEAD','OPTIONS'}), r.rule)
"
```

---

## 11. Changes since this document was verified (2026-10)

- **Personal bests:** `GET /v1/personal-bests` returns `{"activities": {key: [one record]}}` — exactly one record per exercise the user has logged. Reps use the most reps in a single set; holds the longest hold; walk/running/time the longest amount.
- **Player overwrite:** `POST /v1/activities` accepts `replace_today: true` (catalogue exercise + `workout_session_id` + source `manual` only). It replaces the Workout Player's earlier entry for that exercise today instead of adding to it. `GET /v1/suggestions` items carry `player_log` (the player's saved values for today, or null).
- **Experience level:** `PUT /v1/profile` and `POST /v1/profile/onboarding` rescale the plan when `experience_band` changes and return `plan_rescaled`. Moving to a LOWER level returns `{"needs_confirmation": true, "reason": "level_down", ...}` and saves nothing until the request is repeated with `"confirm_rescale": true`. `GET /v1/profile` also returns `display_name` (the name a manager gave a managed person, otherwise null).
- **Voice:** `POST /v1/voice/extract` converts a time-only walk/run into a distance using the exercise's pace (`estimated_from_minutes` marks it); a distance the user said is never replaced. `POST /v1/voice/tts` (JSON `{"text"}` -> `audio/mpeg`) and `POST /v1/voice/stt` (raw audio body -> `{"text"}`) proxy ElevenLabs so its key stays on the server (503 `voice_unavailable` if not configured, 502 `voice_upstream_error` if ElevenLabs fails).
- **Revoking a managed plan:** `DELETE /v1/relationships/<id>` revokes the link and the manager's access; the person's own session keeps working. Add `?sign_out=true` to also end every session of that person's account (for a leaked link) — the response is `{"status": "revoked", "signed_out": true|false}`. `?delete=true` removes an already-revoked card from the list.
- **Managed plans:** `PUT /v1/program/entries/<type>/target` with `managed_user_id` and `set_amounts`, and `POST /v1/relationships/<id>/pending-entries` with `set_amounts`, support a different amount for each set.
- **Ids in URLs** must be UUIDs: anything else is a `404` ("No such endpoint").
- **Headers:** every response has `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`; responses to requests carrying an `Authorization` header also have `Cache-Control: no-store`.
- **Revoked sessions:** a revoked access token returns `403` with code `revoked` (not 401); clients should treat it as "sign in again".
