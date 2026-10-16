# Fitness App — Build Requirements

Derived from the finalized system architecture. Target scale: **1,000 users**.

**Market: India only. Single timezone (IST). No age floor — any age may sign up.**
These constraints simplify several designs — see 8.1 (daily rather than hourly
scheduling). The age floor that was originally planned here (18+) was deliberately
removed in a later product decision so a managed account (a child, student, or
patient set up by a parent/teacher/doctor through Managed Plans) could be any age;
the decision was then extended to self-signup too, so no account anywhere in the
app enforces a minimum age. See *Out of Scope* item 17 for the compliance follow-up
this still leaves open (parental consent / youth data handling).

**Governing principle: the LLM never selects an exercise.** Every exercise a user is
shown or given comes from a reviewed table with a named reviewer and a date. The model
explains, converses, and reads back the user's own data — nothing more. See the
*Knowledge Base* section.

Each module below maps to components in the architecture diagram. Submodules are the
unit of work — most are independently buildable and testable.

---

## Module 1 — Authentication & Session Management

**Architecture component:** `Signup, Login & Stateless JWT Issuance`

### 1.1 Signup
- Email + password registration
- Password hashing (bcrypt/argon2 — never plaintext)
- **Password policy:** minimum 10 characters; reject known-breached passwords (k-anonymity
  check against a breach corpus). Length beats composition rules — do not mandate symbol
  classes.
- **UNIQUE constraint on email at the DB level** — this is what prevents duplicate
  accounts under concurrent signup requests, not application-level checks
- Return signed JWT on success

### 1.2 Login
- Credential validation against stored hash
- Issue short-lived access JWT (15–60 min) + long-lived refresh token (~30 days)
- **Per-account failed-login counter with exponential backoff**, independent of the
  per-IP and per-device limits in 2.3. Those keys are attacker-controlled — someone
  targeting one account simply rotates IP and device ID and never trips either. The
  counter must live on the account.
- Counter resets on success; lockout is temporary and self-clearing, never permanent
  (a permanent lock is a denial-of-service against the real user)

### 1.3 Access Token (JWT)
- **Stateless** — signature-verified at the Gateway, no DB/Redis write on issuance
- Must embed: `user_id`, **`jti` (unique token ID)**, `issued_at` (needed for the
  revocation check), `expiry`
- **`jti` is required for logout to function** — the denylist is keyed on it
  (`revoked:jwt:{jti}`). Without it there is nothing to write to the blocklist and
  logout silently fails to revoke anything.
- **Signing key rotation:** support two active keys (current + previous) via a `kid`
  header so a key can be retired without invalidating every live session. Document the
  rotation procedure; it is needed the day a key is suspected leaked, not before.

### 1.4 Refresh Token Lifecycle
- Store **hashed** in PostgreSQL (never plaintext)
- **Rotation on every use** — issue new token, invalidate old one
- **Sliding expiry: each rotation grants a fresh ~30 days.** An active user therefore
  stays signed in indefinitely and never sees a login screen again after signup.
  Inactivity is what ends a session — open the app within 30 days and it renews.
- **Absolute session cap: ~180 days from the original login**, after which a fresh
  login is required regardless of activity. Without a cap, sliding expiry means a stolen
  refresh token can be kept alive forever by an attacker who simply keeps using it.
  Track the original login time on the token chain, not just the current token's expiry.

> **This is what determines whether users stay logged in.** Sliding + cap gives: daily
> users never interrupted, lapsed users re-authenticate after a month, no session lives
> beyond six months. Absolute-only expiry would force every user back to the login
> screen monthly regardless of engagement — a materially different product.
- **~30s grace period** on the rotated-out token, so a dropped network response
  and client retry isn't mistaken for theft
- **Grace period bound to a stable device ID only — never the IP address.** The device
  ID is generated on first launch and held in secure storage.

> **Do not put IP in this gate.** Mobile IPs change constantly — wifi to cellular
> handoff, tower changes, carrier NAT rotation. A refresh sent over wifi and retried
> over cellular is a different IP, and treating that as theft would revoke sessions on
> every device for a user who did nothing wrong. IP may be logged for investigation; it
> must never trigger revocation.

### 1.5 Logout
- Add current access JWT to the revocation denylist (until natural expiry)
- Revoke the refresh token

### 1.6 Theft Detection & Full Session Revocation
Triggered when a refresh token is reused after its grace period, **or** reused from a
**different device ID** even within the grace period.

> **IP is never part of this decision** — see the warning in 1.4. Same-device reuse
> within the grace window is a benign retry regardless of which network it came from.

Response must do all four:
1. Revoke **ALL** refresh tokens for the user (not just the reused one)
2. Set the user's `tokens_valid_after = now` in Redis (kills every live access token)
3. Revoke/delete the user's device push tokens
4. Page on-call — this event must not happen silently

### 1.7 Staff Authentication *(decision required)*
- Admin console needs a separate identity + role from end users
- Likely answer: existing admin framework behind SSO/VPN, not a service you build
- **Do not ship the admin console without this**

### 1.8 Email Verification
- Send a verification link on signup; token single-use, short expiry (~24h), stored hashed
- Decide and document what an unverified account can do — recommended: full access
  except password reset, with a persistent prompt to verify
- Re-send must be rate limited (see 2.3, per-IP and per-device)

### 1.9 Password Reset
- Request by email → single-use token, hashed, short expiry (~1h)
- **Response must be identical whether or not the email exists** — otherwise the
  endpoint becomes an account-enumeration oracle
- On successful reset: revoke ALL refresh tokens and set `tokens_valid_after = now`
  (same mechanism as 1.6) so any session an attacker already holds dies immediately
- Rate limited per-IP and per-email

### 1.10 Account Deletion
- User-initiated, from the app
- Must delete or irreversibly anonymise: profile, **selected conditions**, activity logs,
  program entries, unmatched phrases (4.5), refresh tokens, device push tokens, quota
  counters
- Revoke all sessions as part of the flow
- Recommended: soft-delete with a short grace window (accidental deletion is common),
  then hard purge — document the window and honour it
- **Backups will still contain the data.** A `deletion_requests` record must **outlive
  the data it deleted**. Policy: deletion is immediate in live systems; backups age out
  naturally within the PITR window; and **if a restore ever occurs, a reconciliation job
  re-applies every pending deletion** before the system returns to service. Without
  that record, a restore silently resurrects deleted accounts.
- **Not optional.** Health-adjacent personal data makes deletion a likely legal
  requirement, not a nice-to-have — see *Out of Scope* item 1

---

## Module 2 — API Gateway & Edge

**Architecture component:** `API Gateway (Stateless JWT Verification & Shared Rate Limit)`

### 2.1 JWT Verification
- Stateless signature verification — no DB lookup

### 2.2 Revocation Check (per request)
Single Redis lookup covering both conditions:
- Token is not on the logout/revocation denylist
- Token's `issued_at` is **after** the user's `tokens_valid_after` timestamp

### 2.3 Rate Limiting — three independent limits
| Limit | Key | Applies to | Notes |
|---|---|---|---|
| Per-user | `user_id` | Authenticated endpoints | Shared across Gateway instances |
| Per-device | `device_id` | Signup / login | Generous threshold |
| Per-IP | `ip_address` | Signup / login | Stricter threshold |

> Per-device and per-IP must be **enforced separately**, not merged into one key —
> a combined key lets an attacker reset the counter by cycling device IDs.

### 2.4 Routing
- Route to the application's service modules; terminate TLS at Nginx / the cloud load balancer.
  **One deployable, not eight services** — see STRATEGY_AND_OPS.md §2.

---

## Module 3 — User Profile & Onboarding

**Architecture component:** `Beginner Profile & Pain History Intake` *(diagram name; the
flow now collects conditions from a closed list, not free-text pain history — 3.2)*

### 3.1 Onboarding Intake
- Collect: age (no minimum — see *Out of Scope* item 17), fitness goal, experience band, and **health conditions —
  selected from a closed list, never free text** (3.2)
- Persist to PostgreSQL
- Return explicit "profile saved" confirmation to client

### 3.2 Health Condition Selection — closed list, three outcomes

Users **pick from a fixed list**. There is no free-text field anywhere in this flow.
This is what makes the safety model work: an unrecognised condition cannot exist.

Every condition in the catalogue sits in exactly one bucket:

| Bucket | Behaviour |
|---|---|
| **Supported** (~10 musculoskeletal conditions) | Program built from templates, filtered by the safety grid (13.3) |
| **Refer** (serious/clinical conditions) | **"Please consult a doctor."** No exercises, no video links, no program. |
| **Not listed** | Not shown in the app at all |

Plus two mandatory options at the end of the list:

- **"None of these"** → standard program
- **"Something else / not listed"** → **treated as Refer.** This is the fail-closed
  default and it replaces what would otherwise be a free-text hole.

> **Both options are real rows in the `conditions` table** — `none` with bucket
> `supported` and no grid entries, `other_unlisted` with bucket `refer`. Without rows
> they cannot be stored in `user_conditions`, and the fail-closed choice would be
> forgotten the moment the user reopened the app.

> A user selecting a Refer-bucket condition gets a clear referral message and can still
> log activities manually. What they must not get is exercise guidance the app has no
> reviewed basis for.

### 3.3 Timezone
- Single market, single timezone (IST). Store it for future-proofing; do not build
  per-user timezone logic.
- Quota windows and scheduling use IST throughout

### 3.4 Profile Read/Update
- Standard CRUD; read path used by the rehab service for context
- Adding a condition later runs the same three-bucket logic (3.2) **and** the program
  removal flow (14.4)

### 3.5 Unit Preference
- Distance units (km default for India; miles available) stored per user
- Used as the default when a voice-logged activity omits its unit (see 12.3)

---

## Module 4 — Task & Habit Tracking

**Architecture component:** `Beginner Task & Habit Service`

### 4.0 Activity Taxonomy — **define before building 4.1 or 12.2**

A **closed list with a full prescription**, not just a list of names. Both the logging
API and the voice parser validate against it.

**Four prescription shapes.** A three-way distance/reps/duration split gets stretches
wrong: a stretch is not one long duration, it is several *held rounds* — which is a set
in every way that matters for prescription and rest.

| Shape | Prescribed as | Example |
|---|---|---|
| `reps` | sets × repetitions | 2 × 5 glute bridge |
| `hold` | sets × seconds held | 2 × 20s calf stretch |
| `distance` | one distance | 1.5 km walk |
| `duration` | one continuous block | 15 min session |

| `activity_type` | Shape | Sets × amount | Per side | Rest | Max | Ceiling |
|---|---|---|---|---|---|---|
| `walk` | distance | 1 × 1.0 km | — | — | 5.0 km | 15 |
| `neck_isometric` | hold | 2 × 5s | — | 20s | 3 × 20s | 180 |
| `shoulder_circles` | reps | 2 × 5 | — | 20s | 3 × 15 | 150 |
| `lower_back_side_stretch` | hold | 2 × 10s | ✓ | 15s | 2 × 30s | 240 |
| `glute_bridge` | reps | 2 × 5 | — | 30s | 3 × 15 | 150 |
| `dead_bug` | reps | 2 × 4 | — | 30s | 3 × 12 | 120 |
| `hamstring_stretch` | hold | 2 × 15s | ✓ | 15s | 2 × 30s | 240 |
| `quad_stretch` | hold | 2 × 15s | ✓ | 15s | 2 × 30s | 240 |
| `calf_stretch` | hold | 2 × 10s | ✓ | 15s | 2 × 30s | 240 |
| `ankle_circles` | reps | 2 × 5 | ✓ | 15s | 2 × 15 | 150 |
| `wrist_circles` | reps | 2 × 5 | ✓ | 15s | 2 × 15 | 150 |

**`per_side` is explicit, never implied.** Seven of the eleven are unilateral. Leaving
it implicit is how a prescribed 2 × 15s stretch silently becomes 2 × 30s of actual
work — and the source audit flagged exactly this ("confirm whether the baseline is per
side or total") on four separate rows.

**`total_volume` = sets × amount × sides.** One comparable number whatever the shape.
Adherence (15.2), the implausibility ceiling (4.6) and the soreness reduction (14.6d)
all need a single figure, and this is it.

**Progression is double progression.** Raise `amount` by `progression_step` until it
reaches `max_amount`, then add a set and reset `amount` to its starting value. One rule
covering all four shapes, which is why the shapes had to be modelled rather than
special-cased.

**Distance activities carry `est_pace_min_per_km`** so a walk can show an honest time
estimate ("1 km · about 12 min") without storing a second target that could drift out of
sync with the first.

Constraints enforced in the database, not in application code:

- `distance` and `duration` **cannot have sets** — one continuous effort, and sets would
  double-count into `total_volume`
- `plausible_max_total` **must exceed a fully progressed prescription**, or a user who
  completes the programme exactly as designed gets flagged as an outlier
- `max_amount >= default_amount`, `max_sets >= default_sets`

`activity_type` is a **stable code**. It is written into every activity log and program
row; changing it orphans data. `display_name` may be edited freely.

**This draft lists 11 activities.** Product owns the final list (*Out of Scope* item 5).
The safety grid is conditions × activities, so the count scales directly: at 10 supported
conditions and 11 activities the grid is **110 cells**.

### 4.1 Activity Logging
- Log walking, pushups, check-ins
- Commit to PostgreSQL (source of truth)
- Return check-in confirmation + updated streak
- **Single entry point for both manual and voice input** — voice-logged activities
  (Module 12) are parsed to the same structured payload and go through this same
  endpoint, with identical validation, streak logic, and storage. Voice is an input
  method, not a parallel pipeline.

### 4.2 Streak Computation
- Streak + inactivity status, computed from PostgreSQL
- **Never cached as the authority** — the reminder cron reads from here

### 4.3 Progress & Trend Analytics
- Historical progress queries
- Growth charts + streak data for the post-login dashboard
- Must return a sensible empty state for brand-new users

### 4.4 Custom Exercises

When a user logs something outside the taxonomy (4.0), **never reject it**. Let them
create it once, then treat it like any other activity.

**Flow:** unrecognised activity → prompt *"New exercise: burpees. How do you count it?
[Reps] [Distance] [Time]"* → save as that user's custom exercise → recognised from
then on, with full tracking, streaks, and charts.

**Rules:**
- **Personal, never global.** A custom exercise belongs to the creating user only.
  One person's mishearing or typo must never appear in anyone else's app.
- **Never create silently.** Always an explicit confirm step — speech recognition
  errors would otherwise generate junk ("bush ups", "push cups")
- **Deduplicate before creating.** Normalise (lowercase, trim, singular/plural) and
  check against both the taxonomy synonyms and the user's existing custom exercises.
  "jogging" must resolve to `run`, not create a duplicate.
- **Users can rename and delete** their custom exercises — mistakes will happen
- **Cap at ~20 per user.** Nobody needs more; a cap contains accidental junk.
- **Carries a plausibility ceiling too**, set from its measurement type's default (4.6).
  Custom exercises are not progressed, but they do feed streaks and charts — a
  "1,000,000 burpees" entry would still corrupt those.
- **Tracking only, never suggested.** Custom exercises must not appear in the app's
  recommended/suggested set. In a pain and rehab product, what the app *recommends*
  carries clinical weight; what it *tracks* does not.

### 4.5 Unmatched Phrase Log

Every phrase the parser and LLM both fail to resolve, and every custom exercise
created, is recorded (phrase, normalised form, count, first/last seen, **platform and
OS version**).

> Record platform and OS version because on-device speech recognition quality varies by
> device and OS build. Without it you cannot distinguish *"our parser is weak"* from
> *"speech-to-text is poor on this Android version"* — two problems with completely
> different fixes.

This is the mechanism for growing 4.0: after a few weeks it gives a ranked list of
what users actually do, in their own words. Promotion into the taxonomy is a
deliberate admin action, informed by this data — never automatic.

> Store the phrase and the derived structure only. This log must not retain audio,
> and it is in scope for account deletion (1.10).

**Treat this table as sensitive.** People say things like *"couldn't run today, my
back's been bad since the surgery"* — that is health information sitting in a table
that does not look like health data, and which people will otherwise query casually
for product insight.

- **Auto-purge after 90 days.** Promotion decisions happen inside that window.
- Access restricted to a named role, not general engineering access
- **Rows are attributed to a `user_id`** — without it, account deletion (1.10) cannot
  reach them and the promise to delete would be false. Aggregate counts for taxonomy
  decisions are produced by query, not by storing anonymous tallies.

### 4.6 Implausible Entry Handling

Each activity carries a plausibility ceiling (4.0). An entry above it — usually a voice
mishearing ("20 pushups" heard as "80", confirmed by a user tapping through) — is:

- **Still saved.** Never silently discard or reject what a user entered.
- **Flagged** and **excluded from progression input** (15.2)
- Surfaced gently at confirmation: *"That looks unusual — is it right?"*

> Without this, Module 15 reads a typo as extraordinary progress and raises the user's
> target, compounding weekly. The guard belongs here, at the data boundary.

---

## Module 5 — Rehab & Pain Guidance (AI)

**Architecture component:** `Grounded Rehab & Pain Service`

### 5.1 Query Intake
- Accept joint/back pain protocol requests

### 5.2 Context Retrieval
- Read the user's selected conditions (`user_conditions`) and profile from PostgreSQL
- Semantic query against the Verified Medical Vector DB

### 5.3 LLM Synthesis
- Synthesize guidance **only** from retrieved verified content
- Guardrails enforced — this is the safety boundary of the product
- **The model never names or prescribes an exercise.** Any exercise mentioned in a
  response comes from the safety grid (13.3) for that user's conditions, with its
  curated video link attached. The model explains and contextualises; the grid decides.
- If the user's conditions place them in the *Refer* bucket, this module returns the
  referral message and no guidance at all (13.3b)

### 5.4 Daily Quota — max 5/user/day
- Durable counter in PostgreSQL (never a volatile cache)
- Window is the **IST calendar day** (single market — 3.3)
- **Must be atomic:** `UPDATE quota SET count = count + 1 WHERE user_id = ? AND day = ? AND count < 5`
  — a separate read-then-write allows two concurrent requests to both pass at count=4
- Row updated → allow. No row updated → block.

### 5.5 Quota-Exhausted Fallback
- Return a clear "try again tomorrow" response — never fail silently

### 5.6 Acting on the Conversation — add asks, removal tells

A pain question often implies a program change. The chat may surface both, but the two
directions behave differently **and deliberately so**.

**Identifying the condition — the user picks, never the model**

A message like *"my lower back hurts when I sit for long hours"* is a **description, not
a condition**. The model interprets it and offers matching options from the closed list
(3.2) for the user to choose:

> *"That sounds like it could be one of these — which fits?"*
> **[Chronic back pain] · [Something else] · [Just sore from exercise]**

The model never records a condition the user did not select. Everything downstream —
the grid, the program, the referral — depends on this being right, so it is a tap, not
an inference.

**Adding — ask first**

Where the grid marks an activity `safe` or `modify` for their condition, the chat may
offer it:

> *"Stretching may help with back pain. Add it to your dashboard?"*

- The activity comes from **the grid**, never from the model's own knowledge
- Explicit confirmation required (16.5); ignoring the offer changes nothing
- On confirmation, written via Module 14 with its normal validation (14.3)

**Removing — act, then tell**

Where an activity in their program is now `avoid`, it is **suspended immediately** by
14.4 and the chat reports what happened:

> *"I've paused squats — it may not be safe with back pain. If your doctor says it's
> fine for you, you can add it back."*

> **This asymmetry is intentional.** Asking *"shall I remove this?"* leaves a risky
> exercise active whenever the user doesn't answer — closes the app, taps "not now",
> gets distracted — and the app keeps recommending it. Suspending first makes the safe
> outcome the default, and the undo (14.7) preserves the user's control. Adding carries
> no such risk, so consent comes first there.

**Where a risky entry can even exist.** With write validation (14.3) in place, an
`avoid` activity can only be in a program because the user added it as an override
(14.7), because the condition was just reported for the first time (14.4), or because
the grid changed (14.8). In the second and third cases the suspension has already
happened — the chat is narrating it, not deciding it.

---

## Module 6 — Video Delivery

**Architecture component:** `YouTube Video Agent`

**Design constraint:** users receive a **clickable YouTube link** only. There are no
embeds and **no live YouTube API calls on the user request path.**

### 6.1 Two-Tier Lookup
1. Check Redis video metadata cache (free)
2. On miss, check the PostgreSQL persistent video store (free) → refresh Redis from it

### 6.2 Response
- Return curated video link(s); user clicks to open in the YouTube app/browser
- If not in the curated catalog: return "not available in curated library"
  (this is **not** a quota error — no quota is involved on this path)

### 6.3 Refer-Bucket Gate

**Check the user's conditions before returning any link.** If the user has a
Refer-bucket condition (3.2), return the referral message and **no video content**
(13.3b).

> Without this check, a user who correctly saw "please consult a doctor" at onboarding
> could still reach exercise videos through this endpoint. The gate belongs on every
> exercise-content path, not only on suggestions.

---

## Module 7 — Content Curation & Ingestion (Admin)

### 7.1 Video Catalog Curation Job
**Architecture component:** `Video Catalog Curation Job (Manual/Periodic)`
- Admin-triggered, low frequency
- The **only** component permitted to call YouTube `search.list` (100 units/call)
- Own daily usage cap, UTC window, separate from any user-facing limit
- Persist to PostgreSQL: video ID, title, thumbnail URL, YouTube watch link
- Warm the Redis cache from the persisted catalog

> **Quota math:** `videos.list` = 1 unit, `search.list` = 100 units, daily project
> quota = 10,000 units. Live per-user search would be ~50× over budget; the curated
> catalog is what keeps this viable.

### 7.2 Medical Content Ingestion Job
**Architecture component:** `Medical Content Ingestion Job (Admin-Triggered)`
- Embed + index **human-reviewed sources only** into the Vector DB
- Record provenance: source, reviewer, version — audit trail for indexed content
- This is the highest-consequence component in the system; treat review as mandatory

**7.2b Content Retraction — required before launch**

If reviewed content later proves wrong, there must be a way to pull it back:

- Mark the provenance record **retracted**; purge its chunks from the vector store
- Keep a **retention-limited log linking answers to the provenance IDs that informed
  them**, so affected users can be identified and notified
- Documented runbook for performing a retraction

> Think of it as a product recall: you need to know which batch went out and who
> received it. Provenance alone records who approved something — it does not let you
> undo it.

### 7.3 Internal Admin Console
- Staff-only, separate role from end users (see 1.7)
- Dispatches both jobs above

---

## Module 8 — Notifications & Reminders

### 8.1 Reminder Scheduler
**Architecture component:** `Daily Cron - Streak & Habit Reminders`
- **Runs once daily at the target IST hour.** Single-market, single-timezone means one
  moment in time serves every user — no hourly sweep, no per-user timezone selection.
- Reads streak + inactivity status from PostgreSQL (source of truth)
- Enqueues computed alerts to the high-priority queue

### 8.2 High-Priority Worker (Push Delivery)
**Architecture component:** `Async Worker - High Priority (Push Notifications)`
- Consumes the high-priority queue only
- **Idempotency check before sending** — key per alert ID; skip if already delivered
  (protects against duplicate sends on job retry after a crash)
- Look up the user's device push token, deliver via FCM/APNs

### 8.3 Device Push Token Lifecycle
- Register/update token from the mobile client after login
- Store per user, per device in PostgreSQL
- **Delete on unregistered/invalid response** from FCM/APNs (app uninstalled or
  token expired) — otherwise dead tokens accumulate forever
- Revoke on logout and on theft detection

### 8.4 Low-Priority Batch Worker
**Architecture component:** `Async Worker - Low Priority`
- Consumes video curation + content ingestion batches on a **separate queue**
- Exists so long-running batches cannot starve time-sensitive push alerts

---

## Module 9 — Data & Persistence

### 9.1 PostgreSQL Cluster
Stores: users, profiles, **activity logs** (workout data — not application logs, which
go to Module 10), LLM quota counters, YouTube curation API usage counter, persistent
video metadata (curated links), hashed refresh tokens, device push tokens, medical
content provenance log. Full schema in the **Data Model** section.
- UNIQUE constraint on email
- **Automated backups + point-in-time recovery** — replication is not backup

### 9.2 Connection Pooling
- **Use Supabase's built-in pooler in transaction mode. Do not self-host PgBouncer** —
  that would put a second pooler in the path for no benefit.
- Pooling is required because connection count scales with **process count**, not user
  count: web plus workers, each with its own pool, can exhaust `max_connections` at low
  traffic.

### 9.3 Redis — two isolated instances

| Instance | Policy | Contents |
|---|---|---|
| **redis-durable** | `noeviction`, persistence, primary + replica | Celery queues (high/low priority), revocation denylist, `tokens_valid_after`, rate-limit counters |
| **redis-cache** | `allkeys-lru` | Curated video link cache |

> **The split that matters is evictable vs. durable, not one instance per purpose.**
> Rate-limit counters carry short TTLs and expire on their own, so they are safe in the
> durable instance. Only the video cache may be evicted — a miss there falls through to
> PostgreSQL (6.1) and costs nothing.
>
> Eviction policy is per-*instance*, not per-database, which is why the cache must be
> physically separate. Use separate logical databases within `redis-durable` for
> namespace hygiene.

> **Deployment note.** The architecture diagram shows eight logical services; they are
> built as eight *modules in one application*, not eight deployables. See
> STRATEGY_AND_OPS.md §2–3.

---

## Module 10 — Observability

**Server-side only.** Client crash and error reporting is 11.9 — server telemetry
cannot see a mobile-only failure.

### 10.1 Centralized Logging & Metrics
- Request logs, latency, error rates from the Gateway/edge
- Application logs + metrics from the service modules, correlated by request ID across
  web and worker processes

### 10.2 Alerting / On-Call
Must page on at minimum:
- Refresh token theft detected (session force-revoked)
- Repeated push delivery failures / queue backlog

---

## Module 11 — Mobile Client (Flutter / React Native)

**Mobile application only. There is no web client.** The backend is a pure JSON API —
it serves no HTML, templates, or static assets. The app is distributed through the
App Store and Google Play, which introduces release, review, and privacy-declaration
obligations that a web product does not have (see *Mobile Release & Store Submission*
in STRATEGY_AND_OPS.md).

### 11.1 Auth Screens
- Signup, login, logout
- Secure storage of access + refresh tokens
- **Silent token refresh** — send refresh token, receive new access + refresh pair;
  handle rotation correctly (always store the newly returned refresh token)

### 11.2 Onboarding Flow
- Age (no minimum — see *Out of Scope* item 17), fitness goal, experience band, and **health conditions chosen from the
  closed pick-list** (3.2) — **never a free-text field**
- If a Refer-bucket condition or "something else" is selected → show the referral
  message; do not build a program (13.3b)
- Wait for save confirmation before advancing to the dashboard

### 11.3 Post-Login Dashboard
- Growth charts + streak data
- Today's suggested activities with their video links (13.5)
- **Discomfort check-in cards** for any open episode (14.6b) — *"Is squats still causing
  discomfort?"* with two buttons. **This is the required delivery channel**, not a push
  notification: notifications can be disabled, and an episode that is never answered
  leaves the entry held indefinitely.
- Empty state for new users

### 11.4 Activity Logging UI
- Walking, pushups, check-ins → confirmation + updated streak

### 11.5 Rehab & Pain Query UI
- Submit query, render synthesized guidance
- Show remaining daily quota (from `GET /v1/rehab/quota`) including when it resets;
  handle the exhausted state gracefully

### 11.6 Video Links
- Render curated links; open in YouTube app/browser on tap
- Handle "not available in curated library"

### 11.7 Push Notifications
- Request permission, register FCM/APNs token after login
- Re-register on token rotation

### 11.8 In-App Voice Assistant (push-to-talk)
- **Assistant button** on the dashboard — always one tap away (floating action button
  or equivalent persistent placement)
- Tap to start → mic opens → user speaks → tap again to stop → parse
- Recording indicator while listening (pulse / waveform) so it's obvious the mic is live
- **Live interim transcript** displayed as the user speaks — both platforms return
  partial results; showing them makes the assistant feel responsive and lets users
  catch a misheard number immediately
- **Auto-stop safeguards:** stop after a few seconds of silence, and enforce a maximum
  recording duration — users forget to tap stop
- Microphone permission flow with a clear explanation of why it's needed
- Flows into the parsed preview → confirm/edit → submit path (see 12.4)

### 11.9 Crash Reporting & Client Observability

Module 10 covers the **server**. None of it reveals that the app is crashing on a
particular Android build or that speech recognition is failing on certain devices.
Without client instrumentation, the first signal of a mobile-only failure is a
one-star review.

- Crash and error reporting in the client (Crashlytics, Sentry, or equivalent),
  reporting OS version, device model, and app version
- Report non-fatal errors too: permission denials, STT initialisation failures,
  failed offline replays
- **Never send transcripts, health condition data, tokens, or any user content to the
  crash reporter.** Metadata and stack traces only.
- Watch: crash-free session rate, per-OS-version error rates, and adoption spread
  across app versions (which tells you how long old clients linger — see 11.10)

### 11.10 Minimum Supported Version / Force Update

API versioning (see *API Contract*) keeps old clients working, which is correct as a
default. But a security fix or a genuinely breaking change eventually needs old clients
off the network, and **the store cannot force anyone to update**.

- Server publishes a minimum supported app version
- The client checks it on launch and on resume
- Below the minimum → blocking screen with a link to the store; the app does not proceed
- Support a **soft** state as well: an update prompt that can be dismissed, used well
  before any hard cut-off
- Cheap to build now, painful to retrofit — an app with no version gate has no way to
  retire a bad release

---

## Module 12 — Voice Activity Logging

**New capability.** Lets a user say *"I ran 1km, walked 2km, and did 20 pushups"*
instead of filling in the manual form. Output is identical to manual input and is
submitted through the existing logging endpoint (4.1).

**Trigger: in-app push-to-talk only** (11.8). The user taps an assistant button to
start listening and taps again to stop. There is no wake word and no background
listening — see *Explicitly Not Built* for why.

### 12.1 On-Device Speech-to-Text
- Native platform STT: iOS `SFSpeechRecognizer`, Android `SpeechRecognizer`
  (Flutter: `speech_to_text` package wraps both)
- **On-device recognition preferred** — audio never leaves the phone
- **Audio is never uploaded and never stored.** Only the resulting transcript is
  used, and only the structured result is persisted
- Handle: permission denied, no speech detected, recognition unavailable offline
- Note: iOS supports true on-device recognition; Android may fall back to Google's
  servers depending on device/config — verify per platform and disclose accordingly

### 12.2 Activity Parser (rule-based)
Converts a transcript into zero or more structured activity entries.

- **Deterministic / rule-based — no LLM call.** Matches against the closed activity
  taxonomy in **4.0**: a verb (from the synonyms column), a quantity, and a unit
- Must handle **multiple activities in one utterance** → multiple separate entries
- Normalises spoken numbers ("twenty" → 20) and unit variants ("kilometre" → km)
- Validates unit type against the activity per 4.0 — reject "20 reps of walking"
- **Runs client-side.** This is forced by two other requirements: the live preview
  (11.8) needs parsing without a round-trip, and offline queuing (12.5) needs it to
  work with no network at all.
  *Trade-off, accepted:* changing parser rules or adding a synonym requires an app
  release. Mitigate by keeping the taxonomy table (4.0) fetchable from the server and
  cached locally, so vocabulary can be updated without shipping a build.

> **Do not route this through the rehab LLM.** That model has a 5/day quota reserved
> for medical guidance (5.4). Logging a workout must never consume a user's medical
> question budget. The voice LLM fallback (12.6) is a **separate model with a separate
> quota bucket** — see the *LLM Strategy* section.

### 12.2b Resolution Order

Each step is attempted only if the previous one fails:

| # | Step | Cost | Where |
|---|---|---|---|
| 1 | Speech → text | **Free** — on-device, unlimited | Phone |
| 2 | Rule-based parser (12.2) | **Free** — instant, works offline | Phone |
| 3 | LLM fallback (12.6) | Paid — **counts against 5/day** | Server |
| 4 | Manual form, transcript pre-filled | Free | Phone |

> **Never rate-limit steps 1 and 2.** On-device speech recognition and the rule parser
> cost nothing. Capping them would throttle the app's primary input method for no
> benefit — a user logging a morning walk, an afternoon workout, and a couple of
> corrections would hit the wall by dinner. The limit belongs on step 3 only, which
> most utterances never reach.

### 12.3 Ambiguity Handling
| Case | Required behaviour |
|---|---|
| Bare number, no unit ("walked 2") | Apply the user's unit preference (3.5); show it in the preview so it can be corrected |
| Unrecognised activity | Don't guess — surface it as unparsed and offer manual entry |
| Nothing parseable | Fall back to the manual form with the transcript pre-filled |

### 12.4 Confirmation Step — **mandatory**
- Always show the parsed result and allow edit before saving
- **Never auto-commit a voice-logged activity.** Speech recognition misreads numbers
  routinely ("20 pushups" → "28 pushups"), and this data drives streaks and progress
  charts — silently wrong entries corrupt the core of the product
- Corrections made at this step are a useful signal for improving 12.2

### 12.5 Offline Behaviour
- If transcription + parsing succeed but the network is unavailable, queue the
  confirmed entry locally and submit when connectivity returns
- Queued entries must be idempotent on submit so a retry can't double-log
- **The LLM fallback (12.6) is unavailable offline.** If the rule parser fails with no
  network, go straight to the manual form — do not queue a pending LLM call.

### 12.6 LLM Fallback

Invoked **only** when the rule parser fails (12.2b step 3). Handles messy phrasing
("went for a bit of a jog, maybe 3k, then knocked out like 20ish pushups") and
transcription errors the parser can't recover from.

- **Server-side.** Needs network and the key must never reach the client (see
  *LLM Strategy*)
- **Separate quota bucket from Module 5.** Voice and medical questions never consume
  each other's budget. Same durable, atomic counter pattern as 5.4; separate
  `quota_type`.
- **Limit: 5 LLM fallbacks per user per day**, enforced **server-side** — a
  client-side limit is trivially bypassed
- **Constrained output only.** The model returns structured data matching the
  taxonomy (activity, quantity, unit) — never prose. Validate the response against
  4.0 before showing it; reject anything that doesn't conform.
- **Confirmation still mandatory (12.4).** An LLM can misread a quantity just as a
  speech recogniser can. It produces a better guess, not a trusted one.
- On quota exhausted, model unavailable, or invalid output → fall through to the
  manual form (12.2b step 4). Never fail with a dead end.

---

## Module 13 — Exercise Suggestions

What the app recommends a user *should* do, as distinct from what it lets them log.
This is the guided-program side of the product.

> **Suggesting is not tracking.** Module 4 accepts almost anything a user did.
> Module 13 recommends a small, deliberate set. A recommendation carries weight in a
> pain and rehab product; a log entry does not. The two must never share a list.

### 13.1 Suggestion Source
- Drawn **only** from the curated taxonomy (4.0)
- **Never** from user-created custom exercises (4.4) — those are tracking-only
- Small set per day. This is a beginner product; a short list is the feature

### 13.2 Personalisation Inputs
| Input | Source | Effect |
|---|---|---|
| Fitness goals | Onboarding (3.1) | Biases which activities appear |
| **Health conditions** | Onboarding (3.2) | **Excludes** activities via the safety grid — see 13.3 |
| Current streak / tenure | Streaks (4.2) | Drives progression (13.4) |
| Recent activity history | Activity logs (4.1) | Avoids repeating the same thing daily |

Suggestions must recompute when the user updates their profile — conditions change, and
stale exclusions are the dangerous kind.

### 13.3 The Safety Grid — **the safety-critical artefact**

A reviewed matrix of **every supported condition × every taxonomy activity**. Roughly
10 × 7 = **70 cells** at the current 7-activity taxonomy (~120 if it grows to 12), each marked `safe` / `avoid` / `modify`, signed and dated by a
qualified reviewer.

| | walk | pushup | squat | plank | stretch | … |
|---|---|---|---|---|---|---|
| chronic back pain | safe | modify | avoid | modify | safe | |
| herniated disk | safe | avoid | avoid | avoid | modify | |
| arthritis of hip | safe | safe | avoid | safe | safe | |

**Rules:**

- **Static and reviewed. Never LLM-generated, never inferred at runtime.** Deterministic,
  auditable, attributable to a named person.
- **Complete coverage is mandatory.** Every condition in the supported bucket must have
  a row covering **every** activity. A cell may say "safe" — it may not be absent.
- **Enforced at build time.** CI asserts full coverage and fails the build if any
  condition/activity pair is unreviewed. It must be impossible to ship a gap by
  accident.
- **Fail closed at runtime too.** If coverage is somehow missing for a user's condition,
  suppress suggestions entirely rather than defaulting to allow.
- **Applies to suggestions and program writes only.** If a user chooses to *log* an
  avoided activity, accept it — their body, their decision. Never block logging and
  never lecture at the point of entry.
- Decisions are logged, so "why was this suggested / not suggested?" is answerable
  afterwards.

> **This closes the fail-open hole.** With a closed condition list (3.2) and mandatory
> full coverage, there is no path by which an unreviewed condition reaches the
> suggestion engine — the free-text route that would otherwise produce "no exclusions
> found, therefore everything is safe" no longer exists.

### 13.3b Refer-Bucket Conditions

If a user's profile contains any **Refer**-bucket condition (3.2), the app returns a
referral message and **no exercise content at all** — no suggestions, no program, no
video links. Manual logging remains available.

This takes precedence over every other rule in Modules 13, 14 and 16.

### 13.4 Progression
- Suggestions should scale with sustained activity, not jump
- Beginners are the target; over-prescribing on day three is how people quit or get hurt
- Progression rules are product-owned and should be written down, not emergent

### 13.5 Delivery
- Surfaced on the post-login dashboard (11.3) alongside streak data
- Paired with the relevant curated video link where one exists (Module 6)
- Defined refresh cadence — daily is the expected default

### 13.6 Positioning and Boundaries
- Suggestions are **general fitness guidance, not a treatment plan or medical advice**
- Required disclaimer wording is a compliance decision (*Out of Scope*, item 1)
- Distinct from Module 5: the rehab service *answers questions* on request; Module 13
  *proactively recommends* activity. The second carries more risk precisely because the
  user didn't ask.

### 13.7 Relationship to the Training Program (Module 14)

Once a user has a program, **Module 13 becomes the daily view over it** rather than an
independent recommender: it selects which of the user's program entries to surface
today, at the targets Module 14 currently holds.

The generic path described in 13.1–13.5 remains the fallback for users who have not yet
completed onboarding and therefore have no program.

---

## Module 14 — Training Program

The persistent, per-user set of exercises with current targets. Module 13 displays it,
Module 15 evolves it, Module 16 proposes changes to it.

> This is the first **stateful** part of the guidance side of the product. Suggestions
> (13) are computed fresh each time and are harmless if wrong once. A program entry is
> written down and repeated for weeks — so every write is validated (14.3).

### 14.1 Composition
Each program entry holds: activity, current target (quantity + unit), status
(`active` / `suspended`), source (`template` / `agent` / `manual`), and when it was added.

- **Curated taxonomy activities only (4.0).** User-created custom exercises (4.4) are
  tracked but never programmed — same boundary as 13.1.
- **One entry per activity per user — enforced by a UNIQUE constraint.** Adding an
  activity already present updates the existing entry rather than creating a second.
  Without this, a logged activity has no unambiguous target to score adherence against
  (15.2) and discomfort state splits across duplicate rows (14.6).
- Small by design. A beginner program is a handful of entries, not a catalogue.

### 14.2 Program Creation
Built at onboarding from a **reviewed program template**, selected by:

| Input | Source |
|---|---|
| Sport / primary goal | Onboarding (3.1) |
| Experience band | Onboarding — `0–3 months` or `3–6 months` |
| Health conditions | Onboarding (3.2) → applied via the safety grid (13.3) |

- **Templates are a reviewed static table, not LLM output.** Same review and provenance
  treatment as the safety grid (13.3).
- **Starting intensity comes from the template.** The model never chooses a starting
  volume.

### 14.3 Write Validation — one gate, one exception

Every insert or update is checked against the **safety grid** (13.3) **at write time**,
regardless of origin.

| Write source | `avoid` verdict | No verdict in grid |
|---|---|---|
| `template`, `agent`, `manual` | **Rejected** | **Rejected** (fail closed) |
| `user_override` (14.7) | **Permitted** — warned, confirmed, recorded, never progressed | **Rejected** — an unreviewed pair is not something a user can knowingly override |

- **Refer-bucket users get no program at all** (13.3b) — checked before anything else,
  including overrides.
- This is the hard gate. It runs after any LLM involvement (16.3), so a model that
  misbehaves cannot place a contraindicated exercise into a user's program — the agent
  has no access to the override path except by relaying an explicit user decision (16.1).

> **The override is deliberately narrow.** It applies only where the grid holds a
> reviewed `avoid` verdict the user is knowingly choosing against. Where the grid is
> *silent*, there is nothing to override and the write is refused.
>
> **A silent cell should never occur in production.** CI enforces complete coverage
> (13.3), so every supported condition has a verdict for every activity. If the gate
> ever encounters a missing verdict at runtime, that is a **defect in the data, not a
> user decision** — refuse the write and **page engineering** (Module 10). Warning the
> user that something is "risky" would be dishonest here: the app has not assessed it
> and does not know.

### 14.4 New Condition Handling — **removal, not just addition**

When a user reports a new condition — in chat (Module 16) or via a profile update:

1. Record it in `user_conditions` (from the closed list — 3.2)
2. **If it is a Refer-bucket condition → suspend the entire program and show the
   referral message (13.3b). Stop here.**
3. Otherwise re-run the safety grid against the user's current program
4. Suspend every entry now marked `avoid`
5. Tell the user plainly what was removed and why

**Steps 2–4 are unconditional and never quota-limited.** They happen whether or not the
user accepts any new suggested exercise, whether or not they continue the conversation,
and whether or not they have LLM quota remaining. **Removing unsafe exercises is a
safety action, not a feature** — it must always work.

Adding safe exercises while leaving harmful ones in place would be the worst possible
outcome of this feature.

Suspended entries are retained, not deleted — if the condition is later removed from
their profile, they become eligible again (subject to 14.3).

### 14.5 Scope Boundary
- Designed for **0–6 months of training experience**. Not for advanced or professional
  users, whose needs this progression model does not serve.
- A training program, **not a treatment plan** — see 13.6.

### 14.6 Per-Exercise Discomfort Reporting

A way for the user to say *"this one hurt"* about a specific program entry — available at
logging time and from the program screen.

This is **not** a condition report (3.2) and must not be treated as one — it is feedback
about one exercise, not a diagnosis.

#### 14.6a Triage — two taps, because "pain" is two different things

**Blocking every exercise a beginner reports discomfort on would gut the product.**
Soreness is the *expected* result of starting to train or of doing more than planned.
Injury is not. The app must tell them apart before deciding what to do.

Ask exactly two questions:

1. **Where?** → **Muscle** (the meaty part) · **Joint** (knee, shoulder, back, elbow)
2. **When?** → **During the exercise** · **Afterwards / next day**

| Answer | Type | Action |
|---|---|---|
| Muscle + afterwards | `soreness` | **Reduce target immediately (14.6d). Do not block.** Explain that this is normal. |
| Muscle + during | `strain_suspected` | **Hold** at current target — no increase, no reduction |
| Joint + either timing | `injury_suspected` | **Block** the entry; open an episode |

- **Two questions is the maximum.** Beginners will not complete a longer form, and an
  abandoned triage means an untriaged report.
- **Default to the safer branch on abandonment** — if the user reports discomfort but
  does not answer, treat it as `strain_suspected` (hold), never as soreness.

**Use the overshoot signal.** If the logged volume greatly exceeded the target, say so
plainly: *"You did 5 km when your target was 2 km — some soreness is expected."* The app
already has this data, and it explains the cause rather than just reacting to it.

> **The mapping above is a reviewed table, not code.** The physiotherapist owns the
> question wording, the type assignment, and the reduction percentage — see *Knowledge
> Base* table 6. Engineering implements the routing; it does not choose the thresholds.
>
> These questions **route between the app's own behaviours**. They do not name a
> condition, and nothing here is a diagnosis.

#### 14.6b Episode lifecycle — `strain_suspected` and `injury_suspected` only

`soreness` reduces the target and ends there. It opens **no episode** and counts toward
**no recurrence**.

| State | Behaviour |
|---|---|
| **Open** | Entry held (strain) or blocked (injury); never progressed (15.3) |
| **Check-in** | After ~7 days, ask *"Is [exercise] still causing discomfort?"* Re-ask weekly until answered. Delivered as a **dashboard card** (11.3) — never only a push, which can be switched off |
| **Resolved** | User confirms it has passed. Episode closes; entry returns to normal and becomes progressable |
| **Still present** | Episode stays open, entry stays held |
| **Entry deleted while open** | Episode closes as **unresolved** — it still counts toward recurrence. If the activity is later re-added, it returns **held**, and the check-in resumes |

- **Resolution is always an explicit user action.** An entry must never un-hold itself by
  timeout — silence is not recovery.
- The app assumes discomfort persists until the user says otherwise.

#### 14.6c Recurrence — the third episode escalates

Episodes are counted **per user per activity**, across program entries, so removing and
re-adding cannot reset the count. **Only `strain_suspected` and `injury_suspected` count.**

- **1st and 2nd episode** — normal hold-and-resolve cycle
- **3rd episode** — the pattern is no longer incidental:
  - **Suspend the entry**; stop suggesting the activity
  - Referral: *"You've reported this three times. It's worth discussing with a doctor."*
  - Re-adding requires the **risk confirmation in 14.7** — same dialog, same consent
    record, same never-progressed treatment

> **Counting soreness here would break the feature.** Every beginner is sore in their
> first weeks; if that escalated, everyone would be told to see a doctor by week three
> and the referral would stop meaning anything. Only joint and during-movement reports
> carry the signal.

#### 14.6d Soreness Reduction — immediate, floored, and escalating

Applied **at the moment of reporting**, not at the next weekly run. The user reported
discomfort today; the target must ease today.

```
new_target = max(
    0.5 × (default_sets × default_amount),          # floor, on total volume
    round_to_increment( current_target × (1 − reduction_pct) )
)
```

- `reduction_pct` comes from the physio-reviewed triage table (14.6a), not from code
- Rounding follows the measurement type (4.0)
- **Floor is half the beginner baseline.** Below that it is no longer a training target.

**The real stopping condition is escalation, not the floor.** Count consecutive soreness
reports for the same activity — reset by any week with a clean log:

| Consecutive soreness reports | Behaviour |
|---|---|
| 1st, 2nd | Reduce per the formula above |
| **3rd** | **Stop reducing. Hold at current target.** Surface: *"This keeps causing soreness — consider resting it or swapping it for something else."* |

> **The third soreness report must not produce a doctor referral.** That escalation is
> reserved for strain and injury (14.6c). Soreness is what starting to train feels like;
> referring on it would send every beginner to a doctor and make the referral meaningless.
> The response is to stop shrinking the target and suggest a change, not to alarm them.

**Interaction with progression.** A mid-week reduction changes the target the weekly job
sees. Adherence for that window is computed against **the target that was in force for
the majority of the window**, recorded on the entry. Without this, a reduction on Tuesday
makes Wednesday's adherence look strong and the job *raises* the target of someone who
reported pain two days earlier.

### 14.7 Manual Program Control — user-driven add and remove

The app does not provide doctor services. When a user consults a doctor and is told to
stop or swap an exercise, they need to act on that themselves.

**Removing an entry**
- Available from the program screen at any time, for any entry
- **Confirmation required** — *"Remove this from your plan?"*
- Removal is always permitted. Never argue with the user about it.

**Adding an entry from the catalogue**
- User picks from the curated taxonomy (4.0) — never free text, never a custom exercise
- **Starting target:** the program template row for their goal and band if one exists
  (14.2); otherwise the activity's **`default_sets × default_amount`** (4.0). Templates only cover the
  two or three activities in a given program, so most self-added activities take the
  baseline — without it the feature would dead-end for nearly every choice.
- The new entry then **calibrates over its first few sessions** (14.9) before joining the
  normal weekly cycle
- **Refer-bucket users cannot open the catalogue at all** (13.3b) — the gate applies to
  `GET /v1/program/catalogue`, not only to suggestions and videos

**When the grid says `avoid` for their condition — the risk confirmation**

A doctor who examined the patient outranks a generic table. The app cannot verify that a
doctor said it — so it permits the choice, but does not adopt it as its own advice.

The user sees a blocking dialog naming their condition and the verdict, to this effect:

> **This exercise may not be safe for [condition].**
> Only continue if a doctor has advised you to do it after examining you.
> **[No, keep it blocked]  ·  [Yes, my doctor advised this]**

- **"No" leaves it blocked.** Nothing is written; the entry does not appear.
- **"Yes"** writes the entry with `source = 'user_override'`.
- **Identical wording and behaviour on every path** — manual add (14.7), the agent
  (16.1), and any future entry point. There must be exactly one risk-confirmation flow.
- **Never progress an overridden entry** (15.2). The app tracks it; it does not escalate
  the intensity of something it does not consider safe.
- Suggestions (Module 13) still never *recommend* it — it appears because the user added
  it, not because the app endorses it.
- The user can re-block it at any time by removing the entry.

**Consent recording**

Every override writes a row to `override_consents`: user, condition, activity, grid
verdict and **grid version** at the time, the exact warning text shown, and the
timestamp. This makes "what was this user told, and when?" answerable later.

**On account deletion, the identity is stripped and the record kept** — `user_id` is
nulled, everything else remains. You keep proof that the warning system worked and what
it said; you no longer hold health data about an identifiable person. This resolves the
otherwise direct conflict between retaining evidence and honouring deletion (1.10).

> Record it, but do not treat it as your protection. A click-through carries less weight
> in consumer health than it appears to, particularly under Indian consumer protection
> law. **The physiotherapist-signed grid is the real defence** — the consent record is
> supporting evidence that the user was warned, not a transfer of responsibility.

> This is the same principle already applied to custom exercises: **track anything,
> progress only what has been reviewed.**

**Refer-bucket users** cannot add program entries at all (13.3b). Removal remains
available.

### 14.8 Safety Grid Version Change — re-validate existing programs

When a reviewer changes a grid verdict after launch — typically correcting `safe` or
`modify` to `avoid` — **every existing program must be re-checked against the new
version.**

- Triggered on grid version publish, as a batch job on the low-priority queue
- Entries now marked `avoid` are **suspended**, with the reason recorded
- Affected users are notified: what was removed and why
- `user_override` entries (14.7) are also suspended — the user may re-add them, but the
  app must not silently keep recommending against its own updated guidance

> 14.4 handles the user reporting a new condition. **This handles the reviewer changing
> their mind, which is the more likely event** — grid corrections are exactly what
> post-launch review produces. Without it, a verdict fixed in the table stays wrong in
> every plan already built from the old version.

### 14.9 Calibration — finding the right level for a newly added exercise

A self-added activity starts from a generic baseline (4.0), which will be wrong for most
people. Calibration corrects it from what they actually do, then hands the entry to the
normal weekly cycle.

**Window:** the first **3 logged sessions**, or 7 days, whichever comes first.

```
observed   = median( logged volumes for this activity in the window )
new_target = clamp( observed × 0.9,  0.5 × baseline,  3 × baseline )
```

| Choice | Why |
|---|---|
| **Median**, not mean | One heroic day or one mis-heard number must not set the target. Logs of 10, 12, 50 give a mean of 24 but a median of 12. |
| **× 0.9** | The target should be reliably achievable, not their absolute maximum. A beginner who fails most days stops opening the app. |
| **Ceiling at 3 × baseline** | Someone who turns out to be fitter than assumed still must not leap to an advanced load in three days. Weekly progression takes them higher gradually — that is what it is for. |
| **Floor at 0.5 × baseline** | Same floor as soreness reduction (14.6d), for the same reason. |

**Rules:**
- **No weekly progression while calibrating** (15.2) — two systems adjusting one target at
  once is how targets go wrong
- **Fewer than 2 logs when the window closes → keep the baseline**, skip calibration, join
  the weekly cycle. There is nothing to calibrate from.
- **A soreness report ends calibration.** The 14.6d reduction sets the target instead —
  demonstrated discomfort outranks demonstrated capacity.
- Calibration runs **once per entry**. Removing and re-adding an activity does not grant a
  fresh calibration; the previous target is restored.
- Applies to any newly added entry, whether added manually (14.7) or via the agent (16.1)

---

## Module 15 — Progression Engine

Adjusts program targets over time based on what the user actually completed.
**Rule-based arithmetic. No LLM.**

### 15.1 Cadence
Runs weekly (configurable to biweekly) per user, as a scheduled batch job on the
low-priority queue (8.4).

### 15.2 Input
Adherence per program entry over the window: completed volume ÷ target volume, computed
from `activity_logs` (4.1).

- **Implausible entries are excluded** (4.6) — a mishearing must not read as progress
- **Adherence is capped at 150%** for progression purposes. Doubling your target is
  excellent; it must not compound into a larger jump than the rules allow.
- **Lagged window.** The week ending Sunday is processed on **Wednesday**, giving three
  days for offline entries (12.5) to sync before adherence is computed. Backdating is
  capped at 7 days so old entries cannot rewrite settled history.
- **Curated activities only.** Custom exercises (4.4) never appear in `user_program` and
  are therefore never progressed — the app must not escalate intensity on something it
  never reviewed. Enforced by schema, not by convention (see Data Model).
- **`user_override` entries are never progressed** (14.7). The user may track an exercise
  the grid marks `avoid`; the app will not increase its intensity.
- **Entries in calibration are skipped** (14.9). Two systems adjusting one target in the
  same window is how targets go wrong.
- **Adherence uses the target that was in force for the majority of the window**, recorded
  on the entry — not whatever the target happens to be when the job runs. A soreness
  reduction applied mid-week (14.6d) would otherwise make the remaining days look strong
  and *raise* the target of someone who reported pain days earlier.

### 15.3 Rules

| Adherence in window | Action |
|---|---|
| ≥ 80% | Increase target by a capped percentage (beginner default: 5–10%) |
| 50–79% | Hold — no change |
| < 50% | Reduce target — it was set too aggressively. **Same formula and floor as 14.6d** |
| Open discomfort episode on this entry (14.6b) | **Hold. Never increase.** Third episode → suspend + referral |

### 15.4 Guards
- **Absolute cap per adjustment.** No single step may exceed the configured percentage,
  regardless of adherence — a data error must not produce a 300% jump.
- **Absolute floor: 0.5 × the default prescription volume** — the same floor as soreness reduction
  (14.6d), for the same reason. Every downward path in the system shares one floor;
  otherwise repeated low-adherence weeks walk a target to near zero while soreness
  reduction stops at half baseline, and the two paths disagree about what a target means.
- **A target already at the floor holds instead of reducing.** After two consecutive
  floored weeks, surface the same message as 14.6d — this activity may not be the right
  fit — rather than continuing to report a reduction that never changes anything.
- Never progress a suspended entry (14.4)
- Never progress past a defined ceiling for the experience band (14.5)
- Conservative by default. Over-prescribing to beginners causes injury and churn.

### 15.5 No LLM — deliberate
Three reasons, in order: this runs for every user every cycle and would be thousands of
calls for arithmetic; it must be deterministic and testable; and a hallucinated jump
from 20 to 60 reps is an injury, not a bad chat reply.

### 15.6 History
Every adjustment records: previous target, new target, adherence figure, and reason.
This drives the user-facing progress narrative and makes "why did my target change?"
answerable.

---

## Module 16 — Program Agent

The conversational layer that can **propose** changes to a user's program — for example
when they report a new problem mid-conversation.

### 16.1 Capability Boundary
- The agent **proposes**; it never writes. All writes go through Module 14 and its
  validation (14.3).
- It cannot alter progression rules or targets (Module 15 owns those)
- It cannot create new exercises (4.4 is user-driven, and custom exercises are never
  programmed)
- **The agent can do exactly what the user can do manually (14.7) — no more.** If the
  user says *"my doctor told me to stop squats and do stretching instead"*, the agent
  performs the same remove-and-add. Where the grid says `avoid`, it must surface the
  **identical risk-confirmation dialog** (14.7) — same wording, same two buttons, same
  consent record. The agent may not accept a spoken "yes" as the confirmation; the user
  taps it. It is a shortcut for the manual controls, never a wider permission.

### 16.2 Retrieval-Constrained Selection
The agent selects from a candidate set **already filtered by the safety grid** (13.3) for
that user's conditions. It picks from that set and explains the choice in plain language.

**The model never names an exercise that is not in the curated catalog.** It is a
selector and explainer, never an author.

### 16.3 Hard Gate
The safety grid is re-applied at write time (14.3), *after* the model has made its
selection. Two independent filters, one before and one after — a misbehaving model
still cannot reach the database with a contraindicated exercise.

### 16.4 Flow — new condition reported in conversation

Identical to 5.6; Module 16 is that behaviour reached conversationally rather than
through the chat's question path.

1. Recognise what the user described and **offer matching options from the closed
   condition list** (3.2) for them to tap. The agent never records a free-text condition.
2. **Update the profile first** (14.4) — this is new health information
3. **If Refer-bucket → referral message, suspend program, stop** (13.3b)
4. **Suspend now-unsafe entries immediately and report it** — *"I've paused squats…"*
   This is a statement, not a question (5.6)
5. Retrieve `safe`/`modify` candidates from the grid; model presents and explains
6. Attach the curated video link where one exists (Module 6)
7. **Ask explicitly:** *"Add this to your dashboard to track?"*
8. On confirmation → write via Module 14

Steps 2–4 happen regardless of whether the user accepts anything in step 7, and are
never blocked by quota (14.4). **Removal never waits on an answer; addition always does.**

### 16.5 Confirmation Required
Nothing enters a user's program without an explicit yes. Silent modification of a
training plan is not acceptable in a pain and rehab product.

### 16.6 Quota
The **conversation** consumes the rehab LLM quota (5.4) — same surface, same risk
profile, no separate budget.

**Safety actions are never quota-limited.** Recording a condition, suspending unsafe
program entries, and showing a referral message (14.4, 13.3b) always work, even at zero
remaining quota. A user out of questions still gets unsafe exercises removed — they
simply don't get a conversational reply.

---

## Knowledge Base — what must exist before the app can run

Six reviewed tables. **No AI is involved in producing any of them.** Once they exist,
the application is straightforward; until they exist, Modules 13–16 cannot be enabled.

| # | Table | Size | Who produces it | Effort |
|---|---|---|---|---|
| 1 | **Activity taxonomy** (4.0) | 11 activities in draft; product to finalise | Engineering. The prescription numbers (`default_sets`, `default_amount`, `max_*`, `plausible_max_total`) are authored, not clinically reviewed — `reviewer` records that | ~1 afternoon |
| 2 | **Supported conditions** (3.2) | ~10 musculoskeletal | Engineering, from the source dataset | ~1 hour |
| 3 | **Refer conditions** (3.2) | the remainder | Engineering, from the source dataset | ~1 hour |
| 4 | **Safety grid** (13.3) | conditions × activities — **70 cells at 7 activities** | **Qualified reviewer** | **1–2 days** |
| 5 | **Program templates** (14.2) | per goal × experience band | **Qualified reviewer** | **a few hours** |
| 6 | **Discomfort triage mapping** (14.6a) | 3 rows + reduction % | **Qualified reviewer** | **~1 hour** |

Plus one curated YouTube link per activity (Module 6, engineering).

### Source material

**Exercise catalogue — [Free Exercise DB](https://github.com/yuhonas/free-exercise-db).**
Released under the **Unlicense** (public domain): unrestricted commercial use,
redistribution and modification, no attribution required. ~800 exercises with muscle
groups, equipment, difficulty and instructions. Copy the dozen rows needed into table 1;
do not call it at runtime. Images are unnecessary — demonstrations come from curated
YouTube links.

**Condition list — the supplied disease dataset, used as a sorting input only.** Sort
its ~100 conditions into the three buckets of 3.2. Roughly 13 are musculoskeletal and
belong in *Supported*: chronic back pain, herniated disk, degenerative disc disease,
spinal stenosis, spondylosis, arthritis of the hip, bursitis, carpal tunnel syndrome,
brachial neuritis, sprain or strain, gout, and injuries to arm/leg/trunk.

**Safety grid and templates — published clinical guidelines, interpreted by a reviewer.**
No dataset maps conditions to exercise prescriptions, because that is clinical judgement
rather than data. Build from:

- **WHO Guidelines on Physical Activity and Sedentary Behaviour (2020)** — free,
  authoritative, covers adults living with chronic conditions
- **NHS condition-specific exercise pages** — back, knee, hip; plain-language and close
  to the register this app needs
- **NICE guidelines** — low back pain, osteoarthritis
- **AAOS / OrthoInfo** — structured orthopaedic exercise programmes
- **ICMR / Indian guidance** — worth checking given the market

The supplied dataset's `workout.csv` may be used as a **rough first draft to speed the
reviewer's work**. It is not a source of truth: it has no citations, no reviewer, and no
version, which 7.2 forbids ingesting as-is.

### Never ingest

| Source | Why |
|---|---|
| `medications.csv` | Names prescription drugs — SSRIs, benzodiazepines, antibiotics. A fitness app must never be one bug away from surfacing these. |
| `Diseases_and_Symptoms_dataset.csv` | 96k rows mapping symptoms → disease. This is a **diagnosis** training set; diagnosing conditions is regulated medical-device territory and far outside this product. |
| `diets.csv`, `description.csv`, `precautions.csv` | Out of product scope |

---

## LLM Strategy

The system has **two LLM uses with nothing in common.** Almost every rule below
follows from keeping them separate.

| | Voice parsing (12.6) | Medical guidance (Module 5) |
|---|---|---|
| Input | *"I did 20 pushups"* | User's **health conditions and profile** |
| Sensitive data | No | **Yes** |
| Consequence of a bad answer | User retypes it | User receives unsafe health advice |
| Quality required | Low | High |
| Model | Free / small is fine | **Paid, with a no-training data policy** |
| Quota bucket | 5/user/day (voice) | 5/user/day (medical) — separate counter |

### L1 — Minimise LLM use before choosing a model
The rule parser (12.2) handles most utterances for free. The LLM is a fallback, not
the default path. This cuts LLM volume before cost is even a consideration.

### L2 — Voice parsing may use a free model
Input contains no health data, so training/retention policies are not a concern here.
If the free endpoint is rate-limited or withdrawn, the user types the entry instead —
degraded, not harmful.

### L3 — Medical guidance must use a paid model with a documented no-training policy
This path carries health condition data and drives health advice. Volume is small — the
absolute ceiling is 5 short questions per user per day, and real usage will be far
below that — so cost is not the binding constraint on this path. Safety is.

### L4 — API keys live server-side only
Phone → your backend → provider. **Never** phone → provider. Keys belong in
environment variables or a secrets manager, never in the app bundle and never in git.
Anything shipped inside a mobile app is extractable; assume a key that ships is public.

### L5 — Free-tier rate limits are account-wide, not per-user
A free tier's requests-per-minute and per-day ceilings apply to your whole account,
shared by every user. Before committing, check the free tier's actual limits against
1,000 users × 5 calls/day. Per-user quotas reduce demand but do not raise this ceiling.

### L6 — Add a global spend/volume circuit breaker
Per-user quotas stop one user from abusing the system. They do **not** stop a bug — a
retry loop, a stuck job — from making thousands of calls overnight. Enforce a
system-wide daily ceiling that halts all LLM calls and pages on-call when hit
(Module 10). This should never trip; you will be glad it exists if it does.

### L7 — Define every failure response in advance
| Failure | Required response |
|---|---|
| Free model rate-limited (voice) | Fall back to manual entry |
| Free model withdrawn entirely | Fall back to manual entry; alert on-call to select a replacement |
| Paid model unavailable (medical) | Return "try again shortly" — **never silently substitute a weaker model** |
| Global circuit breaker tripped | Halt all LLM calls; page on-call immediately |

> The medical row is the one that matters. Quietly degrading to a cheaper model for
> health guidance is the single shortcut this system must never take.

### L8 — Verify and record the data policy before launch
For the medical path, confirm the chosen model does not retain or train on submitted
prompts. Record the provider, model name, policy, and date checked. The compliance
review (*Out of Scope*, item 1) will ask for exactly this.

---

## Data Model

Indicative schema — field types and indexes are the implementer's call, but the
entities, relationships, and constraints below are requirements.

### PostgreSQL

| Table | Key fields | Notes |
|---|---|---|
| `users` | `id`, `email` **UNIQUE**, `password_hash`, `email_verified_at`, `deleted_at` | Soft-delete via `deleted_at` (1.10) |
| `user_profiles` | `user_id` FK, `age`, `fitness_goals`, `experience_band`, `unit_preference` | No age floor (any age may sign up or be set up as a managed account — see *Out of Scope* item 17). No per-user timezone — IST throughout (3.3) |
| `conditions` | `id`, `name`, `bucket` (`supported`\|`refer`), `display_order` | The closed list (3.2). **Includes real rows for `none` and `other_unlisted`** — without them the fail-closed choice cannot be stored. No free text anywhere |
| `user_conditions` | `user_id` FK, `condition_id` FK, `recorded_at` | Multi-row per user; **sensitive — in scope for deletion (1.10)** |
| `safety_grid` | PK(`condition_id`, `activity_type`), `verdict` (`safe`\|`avoid`\|`modify`), `reviewer`, `version`, `reviewed_at` | 13.3. **CI asserts complete coverage** — every supported condition × every activity |
| `deletion_requests` | `user_id` (hashed), `requested_at`, `completed_at`, `purge_after` | **Outlives the deleted data** so a backup restore can re-apply it (1.10). Store the user ID **hashed** and purge the row once `purge_after` passes — set it just beyond the PITR window, so it lives exactly as long as a restore could resurrect the account and no longer |
| `activity_logs` | `id`, `user_id` FK, `activity_type` (enum per 4.0, nullable), `custom_exercise_id` FK (nullable), **`sets_done`**, **`amount_per_set`**, `unit`, **`total_volume`** (sets × amount × sides), `logged_at`, `source` (`manual`\|`voice`), `client_entry_id`, `implausible` (bool) | Exactly one of `activity_type` / `custom_exercise_id` set. `implausible` entries are kept but excluded from progression (4.6). `client_entry_id` UNIQUE per user — idempotency for offline replay (12.5) |
| `streaks` | `user_id` FK, `current_streak`, `longest_streak`, `last_activity_date` | Derived from `activity_logs`; rebuildable |
| `user_quota_counters` | PK(`user_id`, `quota_type`, `local_date`), `count` | `quota_type` ∈ {`rehab_llm`, `voice_llm`} — **separate buckets** (5.4, 12.6). Atomic increment target |
| `custom_exercises` | `id`, `user_id` FK, `name`, `normalised_name`, `prescription_type`, `created_at` | UNIQUE(`user_id`, `normalised_name`); cap ~20/user (4.4) |
| `unmatched_phrases` | `id`, **`user_id` FK**, `phrase`, `normalised`, `first_seen`, `platform`, `os_version` | One row per occurrence, **attributed to a user** — required to honour deletion (4.5). Aggregate counts are derived by query, not stored. Purge after 90 days. No audio, ever |
| `suggestion_history` | `id`, `user_id` FK, `activity_type`, `suggested_on`, `excluded_reason` | Auditability for 13.3; avoids repetition (13.2). **Purge after 180 days** — it grows unbounded otherwise and only recent history informs 13.2 |
| `program_templates` | **PK(`goal`, `experience_band`, `activity_type`)**, `starting_quantity`, `unit`, `reviewer`, `version`, `reviewed_at` | Reviewed static table (14.2). **`activity_type` must be in the key** — a template is several exercises, so one row per goal/band would allow only a single-exercise program. **Starting intensity is never LLM-chosen** |
| `user_program` | `id`, `user_id` FK, **`activity_type` NOT NULL**, **UNIQUE(`user_id`, `activity_type`)**, **`target_sets`**, **`target_amount`**, `unit`, `status` (`active`\|`suspended`), `source` (`template`\|`agent`\|`manual`\|**`user_override`**), `added_at`, `suspended_reason`, **`grid_version_at_write`**, **`calibration_state`** (`calibrating`\|`done`\|`skipped`), **`calibration_started_at`**, **`window_target`**, **`consecutive_soreness_count`** | The persistent program (14.1). `window_target` is the target in force for the majority of the current progression window — adherence is scored against it, not the live value (15.2). `consecutive_soreness_count` drives the third-report hold (14.6d). Discomfort state lives in `discomfort_episodes`, not as a flag here — a single timestamp cannot express recurrence (14.6). **No `custom_exercise_id` column exists** — custom exercises structurally cannot be programmed or progressed (15.2). Every write validated against the safety grid (14.3) |
| `discomfort_reports` | `id`, `user_id` FK, `activity_type`, `location` (`muscle`\|`joint`), `timing` (`during`\|`after`), `type` (`soreness`\|`strain_suspected`\|`injury_suspected`), `reported_at` | Every report, triaged (14.6a). `soreness` rows exist for analytics but open no episode |
| `activity_taxonomy` | PK `activity_type`, `prescription_type`, `default_sets`, `default_amount`, `amount_unit`, `rest_seconds`, `per_side`, `progression_axis`, `progression_step`, `max_sets`, `max_amount`, `plausible_max_total`, `est_pace_min_per_km`, `synonyms` | 4.0. The prescription, not just a name. `default_sets × default_amount` is the beginner starting point used by 14.7 and 14.9 when no template row exists |
| `discomfort_triage_map` | PK(`location`, `timing`), `type`, `action`, `reduction_pct`, `reviewer`, `version`, `reviewed_at` | **Reviewed table (14.6a)** — the physiotherapist owns the type assignment and reduction percentage, not engineering |
| `discomfort_episodes` | `id`, `user_id` FK, `activity_type`, `program_entry_id` FK, `type`, `opened_at`, `closed_at` (nullable), `closure` (`resolved`\|`unresolved`) | 14.6b. Opened only by `strain_suspected` / `injury_suspected`. Counted **per user per activity** — remove-and-re-add must not reset recurrence. Third episode → referral + suspension |
| `override_consents` | `id`, `user_id` FK (**nullable**), `condition_id` FK, `activity_type`, `grid_verdict`, `grid_version`, `warning_text_shown`, `confirmed_at` | Evidence the user was warned before an override (14.7). **On account deletion the `user_id` is nulled, not the row** — the record becomes *"someone was warned that X may be unsafe for Y, under grid version Z, on this date."* Keeps proof the warning system worked without retaining health data about an identifiable person |
| `progression_history` | `id`, `user_id` FK, `program_entry_id` FK, `old_target`, `new_target`, `adherence_pct`, `reason`, `applied_at` | Audit + user-facing "why did this change?" (15.6) |
| `admin_quota_counters` | PK(`quota_type`, `utc_date`), `count` | YouTube curation cap (7.1) — UTC window |
| `refresh_tokens` | `id`, `user_id` FK, `token_hash`, **`device_id`**, `expires_at`, **`chain_started_at`**, `rotated_at`, `grace_until`, `revoked_at` | `grace_until` + `device_id` implement 1.4. `chain_started_at` carries the original login time through every rotation, enforcing the 180-day absolute cap. **Deliberately named `device_id`, not `device_fingerprint` — IP must never be part of this value** |
| `device_push_tokens` | `id`, `user_id` FK, `token`, `platform`, `last_seen_at` | Deleted on unregistered response (8.3) |
| `video_catalog` | `id`, `youtube_video_id`, `title`, `thumbnail_url`, `watch_url`, `tags`, `curated_at` | Persistent store behind 6.1 tier 2 |
| `medical_content_provenance` | `id`, `source_ref`, `reviewer`, `version`, `indexed_at`, `vector_ref` | Audit trail for 7.2 |
| `password_reset_tokens` | `user_id` FK, `token_hash`, `expires_at`, `used_at` | Single use (1.9) |
| `email_verification_tokens` | `user_id` FK, `token_hash`, `expires_at`, `used_at` | Single use (1.8) |

### Redis key shapes

| Instance | Key | Value / TTL |
|---|---|---|
| Denylist | `revoked:jwt:{jti}` | present = revoked; TTL = token's natural expiry |
| Denylist | `user:{id}:tokens_valid_after` | timestamp; checked on every request (2.2) |
| Rate limits | `rl:user:{id}` / `rl:device:{id}` / `rl:ip:{ip}` | counter; short TTL per window (2.3) |
| Video cache | `video:{youtube_video_id}` | cached catalog row; LRU evictable |
| Celery | queues `high_priority`, `low_priority` | see 8.2 / 8.4 |

---

## API Contract

All endpoints under `/v1/`. **Version in the path is required** — the stores cannot
force anyone to update, so old app versions will call old endpoints indefinitely.
Pair this with the minimum-version gate (11.10) for cases where old clients genuinely
must be retired.

### Conventions
- Auth: `Authorization: Bearer <access_jwt>` on everything except the auth endpoints below
- Errors: consistent JSON shape — `{ "error": { "code": "...", "message": "..." } }`
- `429` carries `Retry-After`; `401` means re-authenticate; `403` means revoked or forbidden

| Method | Path | Auth | Module |
|---|---|---|---|
| POST | `/v1/auth/signup` | — | 1.1 |
| POST | `/v1/auth/login` | — | 1.2 |
| POST | `/v1/auth/refresh` | refresh token | 1.4 |
| POST | `/v1/auth/logout` | ✓ | 1.5 |
| POST | `/v1/auth/verify-email` | — | 1.8 |
| POST | `/v1/auth/password-reset/request` | — | 1.9 |
| POST | `/v1/auth/password-reset/confirm` | — | 1.9 |
| DELETE | `/v1/account` | ✓ | 1.10 |
| POST | `/v1/profile/onboarding` | ✓ | 3.1 |
| GET · PATCH | `/v1/profile` | ✓ | 3.4, 3.5 — condition changes trigger 14.4 |
| GET | `/v1/app/config` | — | 11.10 — minimum supported version, soft-update version. Unauthenticated; checked on launch before login |
| GET | `/v1/taxonomy/activities` | ✓ | 4.0 — lets the client refresh parser vocabulary without a release |
| GET | `/v1/conditions` | ✓ | 3.2 — the closed pick-list shown at onboarding |
| POST | `/v1/activities` | ✓ | 4.1 — accepts an **array** (one utterance → many entries); requires `client_entry_id` per entry |
| GET | `/v1/activities` | ✓ | 4.3 |
| GET | `/v1/dashboard/summary` | ✓ | 4.3 — streaks + growth charts |
| GET | `/v1/suggestions` | ✓ | 13.5 — today's view over the program (13.7), exclusions applied |
| GET | `/v1/program` | ✓ | 14.1 — current program entries and targets |
| POST | `/v1/program/entries` | ✓ | 14.3 — add a confirmed entry; validated against the safety grid at write time. Accepts an explicit `override: true` for the narrow case in 14.7; rejected if the grid is silent |
| DELETE | `/v1/program/entries/{id}` | ✓ | 14.1 — user removes an entry |
| POST | `/v1/program/entries/{id}/discomfort` | ✓ | 14.6a — report with triage answers (`location`, `timing`). Returns the resulting type and action |
| POST | `/v1/program/entries/{id}/discomfort/resolve` | ✓ | 14.6b — user confirms it has passed; closes the episode. **Explicit action only — never a timeout** |
| GET | `/v1/dashboard/checkins` | ✓ | 14.6b — open episodes needing a check-in card (11.3) |
| GET | `/v1/program/catalogue` | ✓ | 14.7 — activities the user may add, each flagged with its grid verdict so the client can warn before an override. **Returns the referral message and no activities for Refer-bucket users** (13.3b) |
| GET | `/v1/program/history` | ✓ | 15.6 — progression changes and why |
| POST | `/v1/chat/program` | ✓ | Module 16 — conversational agent; **proposes only**, consumes rehab quota (16.6) |
| POST | `/v1/rehab/query` | ✓ | 5.1 |
| GET | `/v1/rehab/quota` | ✓ | 5.4 — remaining count + reset time, for 11.5 |
| GET · POST | `/v1/exercises/custom` | ✓ | 4.4 — list / create user's own exercises |
| PATCH · DELETE | `/v1/exercises/custom/{id}` | ✓ | 4.4 — rename / delete |
| POST | `/v1/voice/parse` | ✓ | 12.6 — LLM fallback; consumes voice quota, returns structured entries |
| GET | `/v1/videos` | ✓ | 6.2 |
| PUT · DELETE | `/v1/devices/push-token` | ✓ | 8.3 |
| POST | `/v1/admin/jobs/video-curation` | staff | 7.1 |
| POST | `/v1/admin/jobs/content-ingestion` | staff | 7.2 |

---

## Build Order

Dependencies, not a schedule. Later phases assume earlier ones exist.

1. **Foundation** — Module 9 (Postgres, PgBouncer, Redis), Module 2 (Gateway), Module 1 (auth)
2. **Core loop** — Module 3 (profile/onboarding), Module 4 (incl. 4.0 taxonomy first), Module 11.1–11.4
3. **Engagement** — Module 8 (reminders, push), Module 10 (observability — before any real users)
4. **Content & guidance** — Module 7 (curation + ingestion), Module 6 (video delivery),
   **Module 13 (suggestions)**, **Module 14 (program)**, **Module 15 (progression)**,
   Module 5 (rehab AI)
5. **Voice** — Module 12 + 11.8 (depends on 4.0 and 4.1 being finished and stable)
6. **Conversational program changes** — **Module 16**. *Deliberately after launch* —
   see the phasing note below.

> Modules 13, 14 and 15 cannot ship before the safety grid (13.3) and the program
> templates (14.2) have been reviewed and signed off. They are buildable
> earlier; they must not be *enabled* for real users until those tables exist.

**Phasing note — Module 16 is intentionally last.** Templates (14.2) and rule-based
progression (Module 15) carry no LLM risk and deliver most of the guidance value. The
conversational agent introduces a genuinely new failure mode: a wrong selection is not
a one-off bad answer, it is written into a program and repeated for weeks. Ship it once
the exclusion table has been exercised against real users and is trusted.

> Module 10 belongs in phase 3, not last. Shipping to real users without logging or
> alerting means failures in Modules 5–8 are invisible.

---

## Cross-Cutting Requirements

| Requirement | Applies to |
|---|---|
| All quota increments must be a **single atomic conditional UPDATE** | Modules 5, 7, 12 |
| "Daily" means the **IST calendar day**, except admin quotas (UTC) | Modules 5, 8, 12 |
| Correctness-critical state never lives in an evictable cache | Modules 5, 9 |
| PostgreSQL is the source of truth; Redis is acceleration only | All |
| Every quota/limit needs a defined user-facing exhausted state | Modules 5, 12 |
| The rehab LLM quota is reserved for medical guidance only — no other feature consumes it | Modules 5, 12 |
| Voice and manual input converge on one endpoint with identical validation | Modules 4, 12 |
| Activity types come only from the closed taxonomy (4.0) — never free text | Modules 4, 12 |
| API paths are versioned (`/v1/`) — old app versions never stop working | All |
| Auth responses must not reveal whether an email exists | Modules 1.1, 1.9 |
| Signup is the last login screen an active user sees — sliding expiry keeps them in; only 30 days idle, the 180-day cap, or an explicit action ends it | 1.4 |
| LLM API keys live server-side only — never in the app bundle or git | L4 |
| Voice and medical LLM quotas are separate buckets and never share budget | 5.4, 12.6 |
| All quotas enforced server-side — client-side limits are advisory only | 5.4, 12.6 |
| No LLM path may dead-end: every failure falls through to a usable alternative | L7, 12.6 |
| User input is never rejected for being unrecognised — create or fall back | 4.4, 12.3 |
| What the app **suggests** and what it **accepts** are separate lists | 4.4, 13.1 |
| Safety-critical logic (the safety grid) is a reviewed static table, never LLM-generated | 13.3 |
| Health conditions come from a closed list — no free text anywhere in the product | 3.2 |
| Refer-bucket conditions and "not listed" both yield a doctor referral, never exercises | 3.2, 13.3b |
| The safety grid must have complete coverage; CI fails the build on a gap | 13.3 |
| Custom exercises are never programmed and never progressed — enforced by schema | 14.1, 15.2 |
| Implausible entries are saved but excluded from progression | 4.6, 15.2 |
| Where safety is uncertain, fail closed — omit rather than risk | 13.3, 14.3 |
| **The LLM talks; tables decide.** Models explain and select; exercises, exclusions, starting intensity and progression come from reviewed tables and arithmetic | 13.3, 14.2, 15.5, 16.2 |
| Every program write is validated against exclusions at write time, whatever its source | 14.3 |
| A newly reported condition removes unsafe entries from the existing program, unconditionally and without quota | 14.4 |
| Refer-bucket users get no exercise content on any path — suggestions, program, or video | 6.3, 13.3b |
| Discomfort is triaged before it is acted on — soreness reduces, injury blocks | 14.6a |
| Only strain/injury reports open episodes or count toward recurrence — never soreness | 14.6b, 14.6c |
| One program entry per activity per user, enforced by constraint | 14.1 |
| Held entries never un-hold by timeout — only an explicit user resolution | 14.6b |
| Nothing **enters** a program without explicit confirmation; unsafe entries **leave** without waiting for one | 5.6, 14.4, 14.7, 16.5 |
| A condition is always tapped from the closed list — never inferred from free text by a model | 3.2, 5.6, 16.4 |
| Users may override a grid verdict; the app tracks the entry but never progresses it | 14.7, 15.2 |
| The agent can do only what the user can do manually — never more | 16.1 |
| A safety grid version change re-validates every existing program | 14.8 |
| Logout requires `jti` in the JWT — without it nothing can be revoked | 1.3, 2.2 |
| Brute-force protection must be keyed on the account, not only IP/device | 1.2 |

---

## Out of Scope / Decisions Needed Before Launch

1. **Compliance review** — collecting health condition data and serving LLM-generated
   guidance is health-adjacent. Needs sign-off from whoever owns legal/compliance,
   independent of how well the technical guardrails work.
2. **Staff authentication approach** — see 1.7. Decide before the admin console ships.
3. **Access token lifetime** — 15–60 min; the security/convenience trade-off within
   that range is open. **Session longevity itself is now decided** (1.4: sliding 30-day
   refresh, 180-day absolute cap) — only the access-token window remains. Refresh token
   rotation is built either way; this just sets how often it fires.
4. **Medical content sourcing policy** — who reviews, what qualifies as verified,
   how often it's re-reviewed. This is the real safety boundary, not the code.
5. **Activity taxonomy contents** — the table in 4.0 is a starting set. Product owns
   the final list. It must be settled before Modules 4 and 12 are built, and every
   later addition touches three places (taxonomy, API enum, parser synonyms).
6. **Account deletion grace window** — how long a soft-deleted account is recoverable
   before hard purge (1.10). Needs to satisfy whatever the compliance review in item 1
   concludes.
7. **Unverified account permissions** — what a user can do before confirming their
   email (1.8). Recommended: everything except password reset.
8. **Model selection, per path** — which free model for voice (L2) and which paid,
   no-training model for medical guidance (L3). Two separate choices; do not collapse
   them into one.
9. **Global LLM spend ceiling** — the number at which the circuit breaker (L6) trips.
   Set it before launch, not after the first surprise bill.
10. **Safety grid sign-off** — who reviews and approves the safety grid cells in 13.3 (70 at the current taxonomy size), and how
    often it's re-reviewed. **Modules 13–16 cannot be enabled until this exists.** Same
    reviewer as items 4, 12 and 13 — realistically one physiotherapist, and finding them
    is a lead-time item, not a task. **Start recruiting now; it is the critical path.**
11. **Suggestion disclaimer wording** — how the app frames recommendations as general
    fitness guidance rather than medical advice (13.6). Compliance owns the text.
12. **Program template sign-off** — who reviews the starting programs and intensities
    in 14.2, per goal and experience band. **Gates Module 14**, same as item 10 gates
    Module 13, and likely the same reviewer.
13. **Progression parameters** — the adherence thresholds, step percentage, and
    per-band ceilings in 15.3–15.4. Conservative defaults are proposed; the numbers
    need sign-off from whoever owns training safety.
14. **Progression cadence** — weekly or biweekly (15.1). Product decision; weekly gives
    faster feedback, biweekly gives steadier data.
15. **App store privacy declarations** — Apple App Privacy details plus privacy
    manifest, and the Google Play Data Safety form. Both require declaring exactly what
    is collected and why; **health condition data has its own category.** These must
    match what the compliance review (item 1) concludes — a mismatch between the
    declaration and actual behaviour is its own problem. Someone must own producing these.
16. **Which ~10 conditions ship in v1** — deliberately keep this small. Ten fully
    reviewed conditions beat twenty half-reviewed ones, and it is what makes the
    reviewer's job finishable. Everything else sits in *Refer* or is not shown.
17. **Age gate — removed, not just undecided.** This was originally an 18+-only
    assumption throughout; it was deliberately dropped (DB migration
    `e1f2a3b4c5d6_remove_adults_only_age_floor`, age check now `age >= 1` everywhere)
    so Managed Plans could support an actual child/student/patient. The removal was
    then extended to self-signup as well, so there is no minimum age anywhere in the
    app today. **This still leaves the real compliance question the original item
    was flagging genuinely open and unresolved**: accepting minors brings parental
    consent requirements and different data-handling rules (COPPA-type exposure for
    a product with any US reach) that have not been built or reviewed. Someone
    should own resolving this — either building real parental-consent/youth-data
    handling, or deciding the product risk is acceptable for the current scale —
    before this goes in front of users outside the team.

---

## Explicitly Not Built

These were considered and deliberately excluded at this scale:

- **Web client / responsive site** — mobile app only. The backend serves JSON, never
  HTML, templates, or static assets. This is a scope decision, not an oversight.
- **WebSockets / real-time** — every flow is request/response
- **Object/blob storage (S3)** — no photo or media upload exists in this product
- **Semantic caching of LLM responses** — unnecessary at ≤5,000 calls/day, and
  risky for medical content where semantically similar queries can need different answers
- **Distributed tracing** — logs + metrics suffice for a single application at this scale
- **Microservice deployment** — the eight logical services are modules in one app; see
  STRATEGY_AND_OPS.md §2
- **Per-user YouTube API quota** — obsolete once video delivery became link-only;
  users never touch the live API
- **Cloud/server-side speech-to-text** — on-device STT keeps audio off the network,
  costs nothing per use, and is fast enough; a server round-trip buys nothing here
- **Audio storage** — voice input produces a transcript and a structured entry;
  the recording itself is discarded and never uploaded
- **LLM as the *primary* voice parser** — the rule parser runs first and handles most
  utterances for free. The LLM is a fallback only (12.6). *(Superseded: an earlier
  draft deferred LLM parsing entirely; it is now in scope as a quota'd fallback.)*
- **Rate-limiting speech-to-text or the rule parser** — both are free and on-device.
  Only the LLM fallback is metered (12.2b)
- **Automatic promotion of custom exercises into the global taxonomy** — user-created
  exercises stay personal. Promotion is a deliberate admin decision informed by the
  unmatched phrase log (4.5)
- **Per-user LLM provider accounts (e.g. users logging into OpenRouter)** — not
  viable. It would require every user to create and connect a third-party developer
  account before using voice logging. One server-side account for the whole app is
  the correct pattern (L4)
- **Symptom-based diagnosis** — the supplied dataset includes a 96k-row symptom→disease
  training set. Diagnosing conditions is regulated medical-device territory. This app
  asks users to *select* a known condition; it never infers one.
- **Free-text health input** — conditions are chosen from a closed list (3.2). Free text
  would reintroduce the fail-open hole the closed list exists to eliminate.
- **Per-user timezone logic** — single market, single timezone (IST). Revisit only if
  the product expands beyond India.
- **Paid exercise APIs (ExerciseDB / RapidAPI and similar)** — unnecessary. The app
  needs about a dozen exercises, copied once from a public-domain source, and demonstrations come
  from curated YouTube links. A per-request paid dependency on static data would add
  cost, a rate limit, and an external failure mode for no benefit.
- **Custom wake word ("Hey FitApp") / always-on listening** — not viable. iOS does
  not permit third-party apps to hold the microphone in the background for wake word
  detection; that is reserved for Siri. Android allows it via a foreground service,
  but costs a permanent notification, real battery drain, Play Store policy scrutiny,
  and would ship on only one platform. In-app push-to-talk (11.8) delivers the same
  outcome with none of that.
- **OS assistant integration (App Intents / Siri, Google App Actions)** — deferred,
  not rejected. This is the correct way to get true hands-free logging if it's wanted
  later: the OS owns the wake word, the app just handles the intent. Cheap to add on
  top of Module 12 because the parser and logging path already exist.
