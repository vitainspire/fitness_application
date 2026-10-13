# Managed Plans: Teacher / Doctor / Parent Portal — Implementation Plan

Status: DRAFT v2 — open questions resolved, for review before any code is
written.

## 1. What this feature is

Today, every `UserProgram` entry belongs to exactly one person, and that person is
the only one who can add activities to it (`add_program_entry`,
`app/profile/service.py:614`) or benefit from the weekly auto-progression engine
(`run_weekly_progression`, `app/progression/service.py:845`).

This feature adds three new ways a *different* person can build and manage
someone else's plan:

- A **teacher** builds a sports-training plan for a student.
- A **doctor** builds a rehab plan for a patient.
- A **parent** builds a habit-building plan for a child.

Confirmed in conversation, not guessed:

- Teachers/doctors/parents sign up and log in through the **exact same**
  `/v1/auth/signup` / `/v1/auth/login` flow everyone else uses — no separate
  portal login system.
- For **each individual activity** in a managed plan, the creator chooses,
  per activity, whether the app's existing weekly auto-progression engine is
  allowed to touch it, or whether it stays exactly as set until the creator
  manually changes it. Example: a teacher sets running to auto-upgrade and
  stretching to fixed, in the same plan.
- Even for an auto-upgrading activity, the creator can always override
  whatever number the app proposes.
- The creator should be able to see what the managed person is actually
  doing (real logged activity), not just the plan.
- A plan is tied to one specific managed person via a unique link; clicking
  it takes that person through normal signup/login into the **main app** (not
  a stripped-down portal view) — which also means they see the same ads and
  count toward the same active-user numbers as everyone else (a monetization
  side-effect, not something this plan needs to build — it falls out of
  "land them on the main app" for free).
- **There is no separate teacher/doctor/parent account type.** Every account
  that exists today already has access to this — it's a new section
  alongside Library, Dashboard, Workout Player, and the chatbot, exactly like
  those are already visible to everyone. Any logged-in user can open it and
  tap "Create a plan for someone," pick who this plan is for in plain terms
  (child, student, patient, or another relation), and that label describes
  **this one relationship**, not the user's account. The same person could
  manage a plan for their own child AND be a student under a teacher's plan
  AND use the app fully for themselves, all on one account.
- A managed person can also use the app completely normally on their own —
  their own dashboard, their own additional self-added activities, nothing
  about their account is restricted by being managed.

## 2. What already exists that this can reuse

Grounding this in the real code, not assumptions:

- **Program entries**: `UserProgram` (`app/profile/models.py:212`) is already
  a per-user, per-activity row with `target_sets`, `target_amount`, `status`,
  `source`. Nothing here is reps/sports/rehab-specific — any of the three
  manager types can create entries in exactly this table.
- **The weekly progression engine already supports "never touch this
  entry"**: `run_weekly_progression` excludes any entry where
  `UserProgram.source == 'user_override'` (`app/progression/service.py:859`),
  and `apply_escalation` refuses to act on one at all
  (`app/progression/service.py:1082`). **Important: don't reuse this field for
  the new "fixed by manager" concept.** `source='user_override'` currently
  means something specific and different — "a doctor-in-real-life overrode an
  `avoid`-verdict safety warning" (`add_program_entry`,
  `app/profile/service.py:658-661`). Conflating "safety override happened"
  with "a manager chose not to auto-progress this" would corrupt a field
  other code already depends on. This plan adds a **new, separate** column
  instead (see §3).
- **The safety grid still applies.** A doctor assigning an exercise to a
  patient with real conditions must still go through the same
  `_worst_verdict` / `avoid`-verdict confirmation flow every other add goes
  through (`app/profile/service.py:629-658`) — a doctor overriding it in the
  app is the exact `OverrideConsent` path that already exists, not a new one.
- **No existing "edit the target directly" endpoint.** Today, a
  `UserProgram` entry's target only ever changes via the automated weekly job
  or a confirmed `apply_escalation` chat flow — never via a direct "set this
  number" user action, for anyone (grep confirms no
  `PUT/PATCH /program/entries/...` route exists, only `POST` and `DELETE`,
  `app/profile/routes.py:206-217`). A manager's manual-override capability is
  genuinely new, not something to extend from an existing admin tool.
- **No role system beyond a staff flag.** `User.is_staff`
  (`app/auth/models.py:48`) is an internal admin/content-reviewer flag,
  deliberately kept minimal ("a role system nothing here needs yet") and set
  only by directly editing the database — it is unrelated to and not reusable
  for teacher/doctor/parent, which are real, self-serve customer-facing
  roles.

## 3. Data model changes

### 3.1 No account-level role column — confirmed

Resolved: there is **no** `users.role`/`manager_role` column. Every account
already has this feature available, the same way every account already has
Library/Dashboard/Workout Player. Nothing is added to `User` at all for this
feature.

### 3.2 `managed_relationships` (new table)

```
id                 uuid primary key
manager_user_id    uuid references users(id) on delete cascade
managed_user_id    uuid references users(id) on delete cascade, nullable
                   -- null until the invite link is actually claimed (see 3.4)
relation_type      varchar(16)   -- 'child' | 'student' | 'patient' | 'other' —
                                 -- describes THIS relationship, chosen by the
                                 -- manager when they create it, not an account
                                 -- property. Purely a label for plan-builder/
                                 -- dashboard wording (sports vs rehab vs habit
                                 -- framing) — grants no extra privilege.
status             varchar(16)   -- 'pending' | 'active' | 'revoked'
invite_token       varchar(64) unique  -- the unique link's token, see 3.4
created_at         timestamptz
accepted_at        timestamptz nullable
revoked_at         timestamptz nullable

unique(manager_user_id, managed_user_id) where managed_user_id is not null
```

One manager can have many managed users (a teacher has a roster, a doctor has
patients); one managed user could, in principle, have more than one manager
(a child with both a parent and a doctor) — this table supports that
naturally without extra modeling. The real permission check everywhere is
always "does an `active` row here link this manager to that specific managed
user," never anything about who the user generally is.

### 3.3 Two new columns on `UserProgram`

```
managed_by_relationship_id   uuid references managed_relationships(id), nullable
auto_progress                boolean not null default true
```

- `managed_by_relationship_id` is null for every entry that exists today (a
  normal user's own self-built program) and for any entry a managed user adds
  to their *own* plan themselves — it is only set when a manager created or
  is actively responsible for that specific entry. This is what a manager's
  "my managed plans" view filters on, and what authorizes a manager to edit
  that one entry.
- `auto_progress` is the new, clean "does the weekly engine touch this" flag
  — independent of `source`, so it says nothing about safety overrides.
  Defaults to `true` so every existing row and every normal self-built entry
  keeps today's behavior unchanged.
- `run_weekly_progression`'s entry-selection query
  (`app/progression/service.py:855-861`) gets one more condition:
  `UserProgram.auto_progress.is_(True)`. Same for `apply_escalation`'s guard
  at `app/progression/service.py:1082` — refuse to escalate a
  `auto_progress=False` entry exactly like it already refuses a
  `user_override` one.
- **Two managers on the same activity (resolved — v1 behavior, not the final
  answer):** `UserProgram`'s primary key (`user_id`, `activity_type`) only
  ever allows one row per activity. For now, if a manager tries to add an
  activity_type that's already `managed_by_relationship_id` of a
  **different, still-active** relationship, the add is blocked with a clear
  error naming who already manages it — never a silent takeover. Properly
  supporting two managers coordinating on one activity (shared visibility,
  hand-off, notification) is real future work, not solved here.

### 3.4 The invite link

A manager action ("add a student/patient/child") generates one row in
`managed_relationships` with `status='pending'`, `managed_user_id=null`, and a
random `invite_token`. The link is `https://app.example.com/invite/<token>`.

Clicking it:

1. Loads the **existing** signup/login screen (not a new one), with the
   invite token carried through (query param or stored client-side across
   the auth flow).
2. The person signs up or logs in exactly as anyone else does today —
   `POST /v1/auth/signup` or `/login`, completely unchanged.
3. Immediately after that succeeds, the client calls one new endpoint,
   `POST /v1/relationships/claim` with the token. This endpoint:
   - Looks up the `managed_relationships` row by token, confirms it's still
     `pending`.
   - Sets `managed_user_id = g.user_id`, `status='active'`,
     `accepted_at=now()`.
   - A token is single-use — claiming it is atomic (`UPDATE ... WHERE
     status='pending'` style, same spirit as the refresh-token rotation's
     `FOR UPDATE` pattern this codebase already uses elsewhere) so two tabs
     or a retried request can't double-claim it or claim it onto two
     different accounts.
4. The person lands on the normal home screen (`go("home")` in
   `frontend/src/main.js`) — nothing about their experience from here on is
   different from any other user. This is deliberate, per your requirement:
   they see the same ads and count in the same active-user numbers as
   everyone else.

No "auto-login with one tap" mechanism is needed or being built — this
directly reflects your clarification that managers and managed users both go
through the identical, existing registration/login flow.

## 4. Plan-builder flow (manager side)

For an activity in a managed plan, the manager's "add activity" screen is the
existing one (`app/profile/service.py:614`'s taxonomy-driven add flow) plus
exactly one new control: a toggle, "Let the app adjust this automatically" /
"Keep this fixed," defaulting to on. Everything else — taxonomy picker, sets,
amount, the avoid-verdict confirmation dialog if the managed person has a
relevant condition — is the same UI and the same backend call, just carrying
`managed_user_id` (resolved server-side to "does a relationship authorize
this manager for this user") instead of always being `g.user_id`.

New/changed endpoints:

- `POST /v1/relationships` (manager) — create a pending invite, returns the
  link.
- `GET /v1/relationships` (manager) — list my managed users (pending +
  active), each with their most recent activity summary for the roster view.
- `POST /v1/relationships/claim` (managed user, via the invite link) — as in
  §3.4.
- `DELETE /v1/relationships/<id>` (manager) — revoke access (sets
  `status='revoked'`, `revoked_at=now()`). Does **not** delete the managed
  user's `UserProgram` entries, history, or touch `auto_progress`/targets at
  all — see §5.1 for exactly what this means with a worked example.
- `POST /v1/program/entries` (existing route, `app/profile/routes.py:206`) —
  extended to accept an optional `managed_user_id` + `auto_progress`; the
  service layer checks for an `active` `managed_relationships` row before
  allowing it.
- **New**: `PUT /v1/program/entries/<activity_type>/target` (manager only,
  for a `managed_by_relationship_id` entry) — the first-ever direct
  "set this exact number" endpoint in the app, since nothing like it exists
  for self-managed entries today. This is what "the teacher can still
  manually edit if they don't like the app's suggestion" calls for.

## 5. What happens on revocation (resolved, with an example)

**Decision: revoking a relationship changes nothing about the managed
entries themselves.** `auto_progress` and the target stay exactly as they
were the moment before revocation. Only `managed_relationships.status`
changes, and only the manager's ability to see/edit that entry going
forward is removed. The entry simply becomes an ordinary part of the managed
user's own program from then on — indistinguishable from something they'd
added themselves, except `managed_by_relationship_id` still points at the
(now revoked) relationship as a historical record of who set it up.

**Worked example:** Dr. Mehta manages Priya's "Walking" entry:
`target_amount=2km`, `auto_progress=True`. Over several weeks of real
supervised treatment, the normal weekly-progression engine (unchanged by
this feature) grows it: 2km → 2.3km → 2.6km, same curve math as any regular
user. Priya is discharged; Dr. Mehta revokes the relationship.

- From that moment, **nothing changes about Walking** — it stays at 2.6km,
  `auto_progress=True`, and keeps growing on its own exactly like it would
  for any user who'd added it themselves — because by this design, that is
  now effectively what it is. No one has to remember to "unfreeze" anything.
- If Dr. Mehta had instead set Walking to `auto_progress=False` (fixed) —
  say, deliberately holding her at 2km for a medical reason — revoking the
  relationship leaves it fixed at 2km too, not auto-reset to `True`. Nothing
  silently starts moving a number a doctor deliberately chose to freeze.
- **The gap this reveals, worth knowing now:** today, only a manager can
  flip `auto_progress` or hand-edit a target (§4's new endpoint is
  manager-only). If Priya, now on her own, wants to change that fixed 2km
  target herself, there's currently no direct toggle for her — her only path
  is to remove and re-add Walking (`add_program_entry`'s existing behavior
  resets it to the taxonomy default, `app/profile/service.py:678-682`),
  losing the specific number rather than adjusting it. This plan does not
  propose building a self-service edit toggle for ordinary users right now —
  flagging it as a natural, likely-near-term follow-up once this ships,
  not a blocker to building this feature first.

## 6. Monitoring / "what are they actually doing" (the part you asked me to design)

Proposal: one new endpoint, `GET /v1/relationships/<id>/activity`, gated on
the caller being the `manager_user_id` of an `active` relationship with that
id. It assembles, by calling the **existing**, already-built functions
against the managed user's `user_id` — no new analytics engine needed:

- `streak()` (`app/profile/service.py:1352`) — are they actually active.
- `suggestions_today()` (`app/profile/service.py:1559`) — today's plan vs
  what's done.
- `weekly_volume()` / `trend()` (`app/profile/service.py:1440`,`1399`) — the
  existing 7/30-day chart data, for a real adherence picture over time.
- `activity_log_detail()` (`app/profile/service.py:1453`) — the itemized
  "what exactly did they log, on which day" view, for "I want to see the
  actual sets/reps, not just a percentage."
- For a doctor specifically: surface any `OverrideConsent` rows
  (`app/profile/models.py` — the existing avoid-verdict override audit trail)
  for that patient, since a doctor overriding their own earlier guidance is
  exactly the kind of thing a doctor's view should foreground.

This reuses every piece of math this app already has — the only new work is
one authorization-gated aggregator endpoint and a manager-facing frontend
screen to render it (a roster list → tap a managed user → the above). No new
"analytics engine" needs building; the adherence/streak/trend math already
exists and is already correct.

## 7. Migrations needed

One new migration:

- `managed_relationships` table (as in §3.2), with a check constraint on
  `relation_type` and `status`. Nothing added to `users` (§3.1).
- `user_program.managed_by_relationship_id` (nullable FK) and
  `user_program.auto_progress` (boolean, default true, backfilled true for
  every existing row — so nothing about today's behavior changes for anyone
  not using this feature).

## 8. Decisions (previously open questions — all resolved)

1. **No account role** — this is a feature every account already has, not
   something anyone "becomes." See §1/§3.1.
2. **A managed user uses the app fully on their own, unrestricted** — see
   §1.
3. **Revocation changes nothing about the managed entries** — see §5's
   worked example. A known, accepted follow-up: ordinary users don't yet
   have a self-service way to toggle `auto_progress` or hand-edit a target
   themselves, only managers do. Not a blocker for this plan.
4. **Two managers on one activity: blocked, not merged, for v1** — see §3.3.
   Proper coordination between multiple managers is explicitly deferred to
   future work, not solved here.

## 9. Suggested build order

1. Migration (§7) — zero behavior change on its own, safe to ship first.
2. `managed_relationships` CRUD + invite-link claim flow (§3.4, §4) — gets
   the account linking working end-to-end, testable on its own.
3. Extend `add_program_entry` + the new target-edit endpoint for
   `auto_progress`/manager-authorized writes (§3.3, §4), including the
   blocked-second-manager check.
4. Wire `auto_progress=False` into `run_weekly_progression` and
   `apply_escalation`'s exclusion checks (§3.3) — small, surgical, highly
   testable in isolation (mirrors the existing `source=='user_override'`
   exclusion test pattern already in the test suite).
5. The monitoring endpoint (§6) — purely additive, reuses existing functions.
6. Frontend: the new "Create a plan for someone" section (visible to every
   user), relation-type picker, roster screen, plan-builder toggle,
   monitoring view, invite-link landing flow.

Each step ships independently and is testable on its own — nothing here
requires the whole feature to land in one go.
