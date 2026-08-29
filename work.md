# Work Log — Daily Work Log Refactor + Browser QA Fixes

**App:** `st_attendance_tracker`
**Session date:** 2026-08-29
**Full design spec:** `specs/daily-work-log-refactor.md`
**Phase tracker:** `PROGRESS.md`

This document is a plain-English account of everything changed in this session, why, and what's verified vs. not. Five rounds of work: (1) the schema refactor itself, (2) fixes from the user's hands-on browser QA of the result, (3) a real production-path 403 bug found during checkout, (4) a checkout performance fix for large attachments (plus a follow-up debug and an unrelated Gmail rejection traced to test data), (5) Redis caching for the `ST Attendance Settings` singleton.

---

## Executive summary — what shipped, the approach, and why it scales

### What was implemented today

One continuous piece of work, in five rounds, all in this session:

1. **Schema refactor** — replaced one-Frappe-document-per-task with a parent/child model (`Daily Work Log` + `Task Entry`), plus a one-time migration patch for existing data.
2. **Browser QA fixes** — recurring-task real-time sync, email date formatting, duplicate navbar/widget cleanup (found by the user clicking through the actual app).
3. **A real permission bug** (`upload_file` 403 on attachments) — root-caused to a core Frappe limitation, fixed with a dedicated endpoint.
4. **A real performance bug** (checkout hanging on large attachments) — root-caused to synchronous email/attachment encoding inside the request, fixed by moving it to a background job.
5. **Redis caching** for the `ST Attendance Settings` singleton, plus a correctness bug in the caching pattern itself (`expires=True`) found while writing the required test and fixed in both the new code and the pre-existing `_get_team_members()` it was copied from.

77 automated tests now cover check-in, checkout, rollover, recurring tasks, permissions, attachments, notifications, and caching. Full detail, including every bug found and how each was verified, is in the numbered "Round" sections below and in `PROGRESS.md`.

### The approach

The core idea: **model the real unit of work, not the individual line items inside it.** An employee's day is one thing — one document (`Daily Work Log`) — and the tasks inside it are rows in a child table (`Task Entry`), not separate documents. Attendance state (checked in / checked out) became two flags on that one document instead of two separate submittable documents. Task lineage across "carried forward" days became a flat, stable `series_id` (assigned once, copied forward) instead of a linked chain you have to walk backward through.

Everywhere the new code needed to make a decision that used to be baked into document *identity* (which doc is this, is it submitted, what's it linked from), it now checks a *field* on the one document that already exists — cheaper to query, cheaper to index, and there's only one document to have gone stale in the first place. The API surface (`get_page_state`, `get_team_dashboard`, the email templates, etc.) deliberately keeps returning the same flat JSON shape it always did, so nothing above the data layer — the frontend, the dashboards, the emails — had to change to accommodate the new schema underneath it.

The same "do the expensive thing once, not once per read" logic drove the other two fixes: check-in/checkout data no longer waits on email construction (moved to a queue), and settings/team-lookups that are read constantly but change rarely are now cached with explicit invalidation on save, rather than re-read from the database on every single request.

### Why it's scalable

Concrete, not hand-wavy — these are the actual numbers from the original problem statement and this app's own data:

- **Document volume:** the old model created one `Daily Task` document per task — ~100 employees × ~15 tasks/day ≈ 1,500 new documents *per day*. It had already produced 2,279 documents in about a month before this refactor, on a visibly slowing site. Projected forward: ~450,000 documents/year. The new model creates one `Daily Work Log` per employee per day: same headcount, ~30,000/year. **That's a ~15× reduction in document count**, and each remaining document is cheaper on its own (no per-task autoname series contention, no per-task version history, no per-task permission evaluation — Frappe evaluates all of that per-document).
- **Rollover cost stopped scaling with history length.** The old "has this task been completed anywhere in its history" check walked a linked chain backward one document at a time, with a hardcoded `MAX_CHAIN_DEPTH = 3650` safety cap — a sign the model was already straining under tasks carried forward for a long time. The new check is one indexed query — `UPDATE ... WHERE series_id = %s` — that costs the same whether a task has been carried forward for 2 days or 2,000.
- **Indexed for the queries the app actually runs.** Composite `(employee, date)` indexes on both new doctypes, matching every real lookup pattern in the app (registered the same way the old doctypes already did this, via `on_doctype_update` — not a new pattern, just extended to the new schema).
- **Per-request cost no longer scales with attachment size or recipient count.** Checkout used to base64-encode and write email records synchronously inside the HTTP request, twice, before responding — a large attachment made every employee's checkout slower. That work is now enqueued; the request finishes as soon as the attendance data is saved, regardless of how large the attachment is or how many people get notified.
- **Settings/team-membership reads no longer scale with request volume.** `ST Attendance Settings` (read on nearly every check-in, checkout, and dashboard load) and team-membership lookups are now cached in Redis with explicit invalidation on save — a setting that changes a handful of times a year no longer costs a database read on every one of the hundreds of daily check-in/checkout requests that depend on it.

None of this required touching the frontend, changing what any employee sees, or changing the business rules governing attendance — the scalability work is entirely underneath the same behavior, which is also why the same 77 tests that verify correctness are also the evidence that none of this broke anything while making it cheaper.

### Is this safe to install in production right now?

**Not yet, as-is — and this is a deliberate "not yet," not a hedge.** Confirmed with the user: this has only run against the `excel` dev site (~186 documents). It has never run against anything close to production scale (~2,279+ documents today, growing). That gap matters specifically for a schema migration, which is the one part of this work that's genuinely hard to reverse once it's touched real data.

What's solid: 77 passing tests covering the business logic, three rounds of real bugs found and fixed via hands-on verification (not just code review), the migration patch is idempotent (safe to re-run, wraps each employee-day in its own savepoint so one bad row can't take down the rest), and the old `Daily Task`/`Daily Task Log` tables are deliberately left installed and untouched as a rollback source — that part of the plan was designed for exactly this kind of caution from the start.

What's missing before a production install, in order:

1. **Take a real backup of the production database first**, independent of anything else here — `bench backup` (or your Frappe Cloud backup/snapshot flow) *before* running any migration, full stop.
2. **Run the migration against a copy of production data before running it against production itself.** Restore that backup onto a scratch/staging site and run `bench migrate` there. This is the single biggest gap: everything here has been proven correct at ~186 documents; production has ~2,279+ and is growing daily. Confirm the document counts and a sample of migrated records match expectations on that copy before touching the real site.
3. **Confirm production has a background worker process running** before deploying — the checkout performance fix (Round 4) depends on one. Verified locally that this dev bench has a worker; that's not evidence production does. If it doesn't, checkout emails will queue and silently never send, which is worse than the slow-but-working behavior before this fix.
4. **Deploy during a low-traffic window**, and check the error log and a few real checkins/checkouts immediately after — not because anything specific is expected to fail, but because this changed the storage model for attendance data, which is exactly the kind of change worth watching land in real time rather than assuming it went fine.
5. **Leave the old doctypes installed post-migration**, per the original plan's ~1-week verification window, before removing them in a follow-up release (Step 9 in `PROGRESS.md` — not started, deliberately).

Once 1–3 are done, this is in good shape to ship. Skipping straight to production without at least step 2 is the one thing here I'd actively push back on, not just flag.

---

## Part 1 — The schema refactor

### The problem

`Daily Task` was a standalone Frappe document — one per task, per employee, per day. At ~15 tasks/employee/day × 100+ employees, that's ~1,500 new documents/day (2,279 in the first month alone), each carrying full doctype overhead (autoname series, permission evaluation, version history). Attendance itself was tracked via two more submittable `Daily Task Log` documents per employee per day (Morning Check-In, End of Day). Projected: ~450,000 `Daily Task` documents within a year. The site was already measurably slower.

### The fix

Replaced the per-task-document model with a parent/child model:

- **`Daily Work Log`** (new, replaces `Daily Task Log`) — one document per employee per calendar day. Holds check-in fields (`login_time`, `work_location`, `half_day_session`, `is_late`), checkout fields (`logout_time`, `lunch_from`, `lunch_to`, `net_hours`, `working_hours`), lock flags (`morning_submitted`, `eod_submitted`, `locked_at`), and a `tasks` child table. Not submittable — locking is a flag (`eod_submitted`) checked in `validate()`, not Frappe's docstatus state machine (which exists for amend-as-new-version workflows this app never needed).
- **`Task Entry`** (new, replaces `Daily Task`) — a child doctype living only inside `Daily Work Log.tasks`. No autoname, no standalone permissions, no separate version history (inherits the parent's `track_changes`).
- **`series_id`** — every task gets a UUID the first time it's created; every rolled-over copy keeps the same one. Replaces the old `rolled_over_from` Link-chain (which required walking backward one document at a time, with a defensive `MAX_CHAIN_DEPTH = 3650` guard) with single indexed queries: "mark this lineage Done anywhere" is `UPDATE ... WHERE series_id = %s`.

Net effect: ~15× fewer documents (1 per employee-day instead of ~15), and rollover/completion tracking that no longer scales with how many days a task has been carried.

### Files created

- `st_attendance_tracker/st_attendance_tracker/doctype/daily_work_log/` — `daily_work_log.json`, `daily_work_log.py` (controller), `test_daily_work_log.py`
- `st_attendance_tracker/st_attendance_tracker/doctype/task_entry/` — `task_entry.json`, `task_entry.py`
- `st_attendance_tracker/patches/migrate_daily_task_to_work_log.py` — one-time data migration
- `specs/daily-work-log-refactor.md` — the design spec (approved before implementation)
- `PROGRESS.md` — phase-by-phase tracker

### Files changed

- `st_attendance_tracker/api.py` — every function touching `Daily Task`/`Daily Task Log` rewritten against the new schema: `submit_morning_log`, `submit_eod_log`, `get_page_state`, `_safety_rollover`, `_rollover_pending_tasks`, `_ensure_recurring_tasks`, `get_team_dashboard`, `get_management_dashboard`, `_build_team_data`, `get_employee_task_detail`, `get_my_history`, `get_history_day_detail`, `delete_carried_task`, `delete_carried_project`, `reset_morning_checkin`, `update_half_day_session`, attachment helpers, email/notification helpers. Deleted the now-dead `_get_root_task` (chain walk) and `_calc_net_hours` (its result was always immediately overwritten by the doc's own `validate()` anyway, even in the old code).
- `st_attendance_tracker/www/daily_checkin.py` — its server-rendered page context builder duplicated a lot of `get_page_state()`'s query logic directly against the old doctypes; rewritten the same way.
- `st_attendance_tracker/www/daily_checkin.html` — **only** a 3-line mechanical rename: two `frappe.client.set_value`/`upload_file` calls hardcoded `doctype: 'Daily Task'`, changed to `'Task Entry'`. Verified safe by reading Frappe's own permission-delegation code for child doctypes (`frappe/permissions.py`, `frappe/client.py`) before making the change, not assumed.
- `st_attendance_tracker/patches.txt` — registered the new migration patch under `[post_model_sync]`.
- `st_attendance_tracker/st_attendance_tracker/doctype/daily_task_log/test_daily_task_log.py` — trimmed to just the legacy controller's own regression tests (it's no longer exercised by `api.py`, but stays installed for the verification window).

**Why no other frontend changes were needed:** `api.py` keeps returning the exact same flat task-list JSON shape to every caller (via a small `_task_entry_dict` adapter that also synthesizes a `rolled_over_from`-style truthy marker for the email templates, which only ever check it for truthiness). So `my_history.html`, `team_dashboard.html`, `management_dashboard.html`, and the email rendering functions needed zero changes — this was a deliberate design choice while porting, not something the original spec assumed.

### Migration

`migrate_daily_task_to_work_log.py`:
1. Groups every `Daily Task` row and every submitted `Daily Task Log` row by `(employee, date)`.
2. For each employee-day: creates one `Daily Work Log`, copies check-in/checkout fields from whichever `Daily Task Log` rows exist (mapping `docstatus=1` → `morning_submitted=1`/`eod_submitted=1`), and inserts each `Daily Task` as a `Task Entry` child row.
3. Walks each `rolled_over_from` chain **once**, here, to assign one shared `series_id` per chain — the last time that chain-walk ever runs.
4. Wraps each employee-day in its own DB savepoint, so one bad row aborts only that group.
5. Skips any `(employee, date)` that already has a `Daily Work Log` — safe to re-run.
6. Leaves the old `Daily Task`/`Daily Task Log` tables untouched — read-only source data during the verification window.

Run against the real `excel` dev site: 186 `Daily Task` → 186 `Task Entry`, 94 `Daily Task Log` → 59 `Daily Work Log`. Re-ran multiple times (idempotent, no duplicates).

### Three real bugs found via hands-on testing (not just reasoning)

1. **Frappe silently re-stamps `nowtime()` into every unset `Time` field, on every `insert()`, not just at document creation.** `frappe.new_doc()` defaults every Time field to the current time (`frappe.model.create_new`), and — this is the part that isn't obvious — `Document.insert()` does it *again* via `_set_defaults() → update_if_missing()`, which treats a field as "missing" using `is None` and re-pulls a fresh `nowtime()` from a throwaway template. Harmless under the old two-doc model (a Morning Check-In log's unused logout/lunch fields were never read); fatal here, since all four time fields now live on one document — a same-day check-in with no checkout yet would silently inherit a garbage `logout_time`/lunch window and fail lunch-duration validation. Found this because it actually happened to a real employee's record on the shared dev site mid-session. Fixed centrally in `DailyWorkLog.__init__` using Frappe's documented `dont_update_if_missing` escape hatch, rather than patching every call site. Wiped and re-ran the migration after the fix; confirmed zero corrupted records afterward.
2. **Rollover tried to re-save an already-locked document.** `_rollover_pending_tasks`/`_safety_rollover` need to flip a task's status (Done/Rolled Over) on the *source* day — which is very often the day whose EOD was just submitted. Routing that through `work_log.save()` re-triggered the parent's own lock guard, blocking EOD submission the moment any task actually rolled over. Fixed by writing the source row's status directly via `frappe.db.set_value("Task Entry", ...)` — bypassing the parent's `validate()` for this one internal housekeeping step, the same pattern the old standalone-`Daily Task` code used for the identical case.
3. **Dropped a real business rule while merging the two log doctypes.** The old `Daily Task Log.validate()` required a resolvable `login_time` before allowing an "End of Day" save, throwing "Morning Check-In is required before submitting End of Day." Missed this while porting; caught by a ported test (`test_1_3_eod_without_checkin_blocked`) failing. Re-added to `DailyWorkLog.validate()`.

### Test suite (Round 1)

New `daily_work_log/test_daily_work_log.py` — happy-path check-in/EOD, double-submit blocks, lunch-time edge cases (reversed, >4h, outside shift, midnight wrap), BOLA/permission checks (cross-employee edit/delete, guest access, HR-only endpoints), lock-after-EOD enforcement, rollover idempotency, recurring-task-never-rolls-over, XSS/SQL-injection-safe storage, Team Leader email resolution.

Verified: `bench --site excel run-tests --app st_attendance_tracker` → 61/61 pass. `bench --site excel migrate` run clean from empty state multiple times.

---

## Part 2 — Browser QA round

The user tested `/daily-checkin` by hand in a browser and reported 5 issues plus 3 questions.

### Questions answered (from reading the actual current code, not assumption)

1. **Does the "x" button on a task delete it from the backend?** Yes — it calls `delete_carried_task`, which deletes the real `Task Entry` row. (A different "x" on a *not-yet-saved* new task row you're still typing is a pure client-side remove — nothing exists server-side yet to delete.)
2. **What happens to the document when an employee resets check-in?** Nothing is deleted and nothing new is created. `reset_morning_checkin` reverts the *same* `Daily Work Log`: clears check-in fields, sets `morning_submitted = 0`, reverts non-"Rolled Over" tasks to "Pending" (so typed tasks survive), deletes the `Employee Checkin` IN record, and sets a `was_reset_today` flag. Re-checking in reuses this same document.
3. **How many `Daily Work Log` documents get created per day?** Exactly one per employee per day — created once (at check-in, or earlier if a rollover/recurring task needed somewhere to live), then updated in place at checkout. That's the entire point of the refactor: old model made 2 submittable log docs/day plus one standalone doc *per task*.

### Issues fixed, each with a new regression test

- **Recurring tasks not syncing in real time.** Root cause: `Task Entry` had no link back to the `Recurring Task Template` it came from, so `_ensure_recurring_tasks` could only ever *add* missing instances — editing, deactivating, or deleting a template never touched an already-created (but still untouched) instance for today. Added a `recurring_template` Link field on `Task Entry`. `_ensure_recurring_tasks` now also updates or removes today's still-`Pending` instance to match the template; `save_recurring_task`/`delete_recurring_task` call it immediately rather than waiting for the next page load. An instance already marked In Progress/Done is left alone — a template change never overwrites work in progress. Deleting a template now passes `force=True` to `frappe.delete_doc`, since the new link field would otherwise make Frappe's own link-checker refuse to delete any template that's ever produced a task.
- **Email date format.** Subject lines were already `dd-mm-yyyy`; the "Date"/"Shift Date" row *inside* the email body was raw `yyyy-mm-dd`. Added a small `_format_email_date()` helper, applied at all 4 places a date appears in an email detail card.
- **Duplicate navbar buttons.** Removed "Additional Work" and "Team" from the top header bar in `daily_checkin.html` — both already live in the left sidebar. Left "Docs" and "History" alone (not flagged).
- **"My team today" widget.** Removed from the right sidebar of `daily_checkin.html` (the Team link already lives in the left sidebar) — including its now-dead JS (a `get_team_dashboard` fetch that only ever filled this widget) and CSS (`.tm-*`/`.av-*` rules).
- **Sticky "Task Completion" header on scroll.** The hand-off mechanism already exists (`window.addEventListener('scroll', ...)` watching `.pb-row`'s position against the sticky 70px `.tnav` header) and read as structurally correct on static review, but couldn't be reproduced without a live browser at the time. User later manually verified in a real browser that it works — resolved, no code change made or needed.

### Verification-gap follow-up

The user asked, reasonably, whether everything that used to work still works. Several `api.py` functions were rewritten for the new schema but had zero test coverage from Round 1: `get_my_history`, `get_history_day_detail`, `get_management_dashboard`, `get_employee_task_detail`, `get_task_attachments`, `delete_carried_project`, `update_half_day_session`. Spot-checked all of them live against real migrated dev-site data first (zero exceptions, sane output), then converted that into 8 permanent tests (`test_8_1`–`test_8_8`), including a test helper that fabricates an approved half-day `Leave Application` (bypassing HRMS's own leave-balance validation via `ignore_validate`, since the code under test only ever reads the raw fields) to exercise the actual "half-day session approved" success path, not just the rejection path.

Also confirmed (by diffing `daily_checkin.html` against git) that the pre-existing localStorage-based draft/edit persistence on page refresh (`st_morning_draft`, `st_eod_draft`, `st_morning_edits`, `st_eod_edits`) predates this session entirely and sits outside every file touched by either round of work — untouched, still intact.

### Test suite (Round 2 additions)

`test_8_1` history list, `test_8_2` history day-detail, `test_8_3` employee-task-detail (HR allowed / stranger denied), `test_8_4` task attachments (owner allowed / stranger denied), `test_8_5` delete-project (removes the right tasks, blocked after EOD), `test_8_6`/`test_8_7` half-day session (blocked without leave / succeeds with an approved one), `test_8_8` management dashboard. Plus 4 recurring-sync tests (`test_7_3`–`test_7_6`): template deletion removes today's pending instance, deactivation does too, editing syncs description in place, an already-started instance is left alone.

**Current total: 69/69 tests passing.**

---

## Part 3 — Real 403 found on checkout ("attach a file")

The user hit a live `403 FORBIDDEN` on `POST /api/method/upload_file` attaching a file to an existing task on the checkout page, and pasted the actual browser network trace. Root-caused via live reproduction against the exact failing user/record — not guessed.

**The mechanism:** Frappe's generic `upload_file` core endpoint checks write permission by calling `frappe.get_doc("Task Entry", name).check_permission("write")`. For a child (`istable`) doctype this is *supposed* to delegate to the parent document (`has_child_permission` in `frappe/permissions.py`) — the exact mechanism verified before making the original `Daily Task` → `Task Entry` rename in Part 1. What wasn't verified at the time: that delegation reads `getattr(child_doc, "parent_doc", child_doc.parent)` to find the parent — but `parent_doc` is a `@property` defined on *every* Frappe document (`base_document.py`), so it always "exists" (returning `None` on a standalone-loaded child) and `getattr` never falls through to the intended `.parent` name string. The recursive permission check on the parent `Daily Work Log` then runs with no document at all, so an if_owner-gated permission (ours — matching the old `Daily Task` doctype's own permission row) can never resolve "yes, this employee owns it" and is unconditionally denied.

Confirmed empirically with `frappe.permissions.has_permission(..., debug=True)` against the real user/task, which showed `if_owner: {"write": 0}` even though the employee genuinely owned the document. Also confirmed the *other* rename from Part 1 (`frappe.client.set_value`, used for inline description/remarks edits) is unaffected — it resolves the full parent document explicitly before calling `.save()`, so it never touches this broken code path. This is a core Frappe limitation for if_owner-restricted child doctypes reached via a bare `frappe.get_doc(child_doctype, name)`, not something fixable through DocType permission configuration.

**Fix:** added a dedicated `upload_task_attachment` whitelisted endpoint (`api.py`) that runs the same `_assert_task_owner` check already used elsewhere, then saves the `File` document directly with `ignore_permissions=True` — replicating exactly what core `upload_file` does once its own permission check passes. `daily_checkin.html`'s `uploadTaskFiles()` now routes uploads for an *existing* task through this endpoint; a brand-new, not-yet-saved task still uses core `upload_file` as an orphan upload (no doctype/docname sent), which was never affected since it never reaches `check_write_permission`'s doctype branch.

Added two regression tests: an ownership-denial test (fires before the file is ever touched — no request-context faking needed) and a successful-upload test (using a minimal fake-request stub, since `frappe.request` isn't populated outside a real HTTP request in the test harness). Re-ran the exact failing scenario — the same real user against the same real task — and confirmed it now succeeds.

**Current total: 71/71 tests passing.**

---

## Part 4 — Slow checkout with large attachments

The user reported checkout taking a long time when attaching images/zip/PDF files — the button stuck on "Submitting..." before eventually completing. This is precisely the item flagged (but explicitly deferred) in Part 1's spec, Section 8.3: "move notification emails fully off the request path" — now actually hit in practice.

**Root cause:** `frappe.sendmail(..., now=False)` doesn't send over SMTP synchronously, but it still has to base64-encode every attachment and write a complete Email Queue record *inside the request* before returning. `submit_eod_log` was doing this twice in a row — once for the HR/Team-Leader notification, once for the employee's own confirmation email — both carrying the same attachments, entirely inside the synchronous checkout HTTP request. A multi-megabyte attachment, encoded twice, is a genuinely multi-second delay before the response ever comes back.

**Fix:** extracted the notification-building logic for both check-in and checkout into two standalone functions, `_send_checkin_notifications`/`_send_eod_notifications`. Each re-fetches the employee and `Daily Work Log` fresh by name/date rather than closing over already-built objects — necessary because a background job runs in a separate worker process, not the request's own memory. `submit_morning_log`/`submit_eod_log` now hand these to `frappe.enqueue(..., queue="short", enqueue_after_commit=True)` instead of calling them inline. `enqueue_after_commit=True` guarantees the background worker never reads the just-saved `Daily Work Log` before this request's transaction is durably committed. The checkout HTTP response now returns as soon as the attendance data itself is saved; the emails go out moments later from a background worker.

This dev bench happens to have a real `bench worker` process running, so the fix was verified live, not just by reasoning: re-ran the exact enqueue call and watched `logs/worker.log` — the job (`st_attendance_tracker.api._send_eod_notifications`) is picked up and logged "Job OK" within milliseconds. The enqueue call itself took ~0.002 seconds regardless of attachment size, versus the old inline path whose duration scaled with how much there was to encode.

**A test-coverage side effect worth noting:** `bench run-tests` has no worker draining the queue, so every existing test that calls `submit_morning_log`/`submit_eod_log` silently stopped exercising the email-building code the moment it became "just enqueue this" — a real, if subtle, coverage regression introduced by the fix itself. Closed it with two direct tests (`test_9_1`, `test_9_2`) that call `_send_checkin_notifications`/`_send_eod_notifications` synchronously — the exact functions the enqueue call points at — including one with a real task attachment.

**Current total: 73/73 tests passing.**

**Follow-up debug (no code change):** asked to keep digging into where the remaining time actually goes. Measured for real with an 8MB test file and 3 HR/Team-Leader recipients: the `frappe.sendmail()` call itself costs ~0.4s per attached file (now backgrounded, was the direct request-blocking cost before this fix); separately, the scheduled flush job's `EmailQueue.build_message()` re-reads and re-base64-encodes each attachment **once per recipient** (~0.35s each) — confirmed via source that `EmailQueue.attachments_list` re-parses its stored JSON fresh every call, so nothing is cached across recipients. That's inherent to Frappe core's Email Queue design, not this app's code. The concrete fix (drop raw attachments from the multi-recipient HR/Team-Leader email, keep them only on the employee's own single-recipient confirmation email — HR/TL can still view/download via the app) was identified and presented, but the user chose to keep attachments on all emails as-is. No change made; documented here in case checkout volume ever makes this worth revisiting.

**A real Gmail rejection, traced to test data (no code change):** user forwarded a live `smtplib.SMTPDataError: 552 5.7.0 ... potential security issue` from Gmail. Inspected the actual stuck `Email Queue` record instead of guessing — it carried a legitimate 14MB PDF plus a 235KB zip literally named `st_attendance_tracker-develop(1).zip` (a GitHub "download as ZIP" of this app's own source code, left over from testing the Part 3 upload fix). Gmail's content scanner flags zip archives containing script/source files; this is a hard, permanent rejection on Gmail's side, unrelated to this app. Recommended discarding that specific queued email and not attaching code archives through the checkout feature going forward — ordinary document/image zips aren't affected.

---

## Part 5 — Redis caching for ST Attendance Settings

User asked for this with an already fully-specified implementation: cache the `ST Attendance Settings` singleton the same way `_get_team_members()` already caches team lookups — it's read via `frappe.db.get_single_value()` on nearly every check-in, checkout, and dashboard load (including once per save inside `DailyWorkLog.validate()`), but the settings themselves change only a handful of times a year.

Added `_get_attendance_settings()` next to `_get_team_members()` in `api.py`, plus `clear_attendance_settings_cache()` wired to `ST Attendance Settings`'s `on_update` in `hooks.py` (added alongside the existing `Employee` `doc_events`, which were left untouched). Replaced the direct reads in `_get_hybrid_office_days()` and `get_management_dashboard()` (`api.py`) and `DailyWorkLog._check_late()` (`daily_work_log.py`). Also found — and fixed — one the task didn't name: `www/daily_checkin.py` has its own literal duplicate of `_get_hybrid_office_days()` that doesn't import the one in `api.py`, and it also read the setting directly on every `/daily-checkin` load. Pointed it at the same cached helper rather than leaving a second uncached copy of the identical read.

**Deliberately left two call sites uncached, both for correctness, not oversight:**
- The legacy `daily_task_log.py` controller — frozen, unused by the live app since Part 1's refactor, out of the stated scope, and slated for removal.
- `tasks.py`'s scheduler dedup guard (`_already_sent_today()`), which reads and writes "last sent" marker fields on this *same* singleton under an explicit row lock, in the same transaction, specifically so a scheduler retry can't double-send a report. It writes via `frappe.db.set_single_value()`, which never fires `on_update` — caching this read would make the dedup guard blind to its own just-written value and defeat the reason it exists. Not the "changes a few times a year" case the caching rationale describes.

**A real bug, found only because the task required a "cache hit doesn't re-hit the DB" test:** Frappe core's `RedisWrapper.get_value()`, called without `expires=True`, writes a *negative* cache entry (`None`) into `frappe.local`'s per-request memory on a miss — and `set_value()` with a TTL never clears that local entry afterward. So the next `get_value()` call in the same request/process keeps returning the stale local `None`, never checking Redis again, even though Redis now holds the real value that was just written. This is invisible unless something actually calls the getter twice in one process and checks the result — exactly what the required test does. Fixed by adding `expires=True` to the read. Left `_get_team_members()` completely untouched per the explicit scope, even though it likely carries the identical latent issue — a separate, pre-existing thing for the user to decide on, not part of this task.

Added 3 tests: cache-miss returns the correct live value, a cache-hit provably skips the DB (`frappe.get_single` monkeypatched to fail if called), and saving the settings invalidates the cache so the very next read reflects the new value — each restores the real shared singleton's original value afterward. Final grep confirms zero remaining `get_single_value("ST Attendance Settings", ...)` calls anywhere except the two deliberately-excluded ones above.

**Current total: 76/76 tests passing.**

**Follow-up:** the flagged-but-unfixed gap in `_get_team_members()` (the function this whole caching pattern was copied from) — same one-line `expires=True` fix applied, nothing else about the function touched. New test (`test_10_4`) mirrors the one that caught the original bug: warm the cache, monkeypatch `frappe.get_all` to fail if called, confirm a same-process second call returns the correct cached value without re-querying. **Current total: 77/77 tests passing.**

---

## Full file inventory

**New:**
```
st_attendance_tracker/st_attendance_tracker/doctype/daily_work_log/daily_work_log.json
st_attendance_tracker/st_attendance_tracker/doctype/daily_work_log/daily_work_log.py
st_attendance_tracker/st_attendance_tracker/doctype/daily_work_log/test_daily_work_log.py
st_attendance_tracker/st_attendance_tracker/doctype/task_entry/task_entry.json
st_attendance_tracker/st_attendance_tracker/doctype/task_entry/task_entry.py
st_attendance_tracker/patches/migrate_daily_task_to_work_log.py
specs/daily-work-log-refactor.md
PROGRESS.md
work.md  (this file)
```

**Modified:**
```
st_attendance_tracker/api.py   (Round 1 rewrite; Round 3 added upload_task_attachment; Round 4 extracted + enqueued notification jobs; Round 5 added settings cache)
st_attendance_tracker/www/daily_checkin.py   (Round 5 pointed its duplicate _get_hybrid_office_days() at the cache)
st_attendance_tracker/www/daily_checkin.html   (Round 2 nav/widget cleanup; Round 3 uploadTaskFiles() routing)
st_attendance_tracker/hooks.py   (Round 5 added ST Attendance Settings on_update hook)
st_attendance_tracker/patches.txt
st_attendance_tracker/st_attendance_tracker/doctype/daily_task_log/test_daily_task_log.py
st_attendance_tracker/st_attendance_tracker/doctype/task_entry/task_entry.json   (recurring_template field, added in Round 2)
```

**Untouched by this session, present in the working tree from before it started** (an unrelated in-flight "Additional Work" self-service feature — noted here only so it isn't mistaken for part of this work): `st_attendance_tracker/hooks.py`'s `/additional-work` route, `st_attendance_tracker/st_attendance_tracker/doctype/additional_work/`, `st_attendance_tracker/www/additional_work.html`/`.py`, and the `Additional Work` CRUD functions + `_get_team_members()` Redis caching already present in `api.py`.

An earlier abandoned scaffold (`ST Daily Log`/`ST Daily Task` doctypes, wrong module path, unregistered patch) was found sitting in the repo at the start of this session and discarded before any of the above work began — it didn't match this spec's naming or field design and wasn't discoverable by Frappe anyway.

---

## What's verified vs. not

**Verified:**
- Full automated test suite: 77/77 passing
- `bench --site excel migrate` run clean, multiple times, from both empty and already-migrated state (idempotent)
- Real dev-site data migrated with zero corruption (checked directly via DB queries, not just assumed)
- Rewritten read endpoints spot-checked live against real data
- The Part 3 file-attachment fix re-verified against the exact real user/task that produced the original 403
- The Part 4 enqueue fix confirmed live against this bench's actual `bench worker` process — the job completes ("Job OK") within milliseconds of being queued, and the enqueue call itself is ~0.002s regardless of attachment size
- The Part 5 caching fix (the `expires=True` bug and the fix for it) confirmed with a direct, isolated repro script against real Redis before relying on the test suite alone

**Not verified:**
- No pre-commit/lint run (not installed in this environment)
- No full manual browser click-through of every page (the user did browser-test `/daily-checkin` directly and found the 5 issues above; other pages — `/my-history`, `/team-dashboard`, `/management-dashboard`, `/recurring-tasks` — have not had the same hands-on pass)
- File upload/view/delete through the actual browser UI (the upload endpoint and view/delete/list functions are all covered by tests or direct reproduction now, but no one has clicked "Attach" in a real browser since the Part 3 fix)
- The Part 4 fix was verified at the server/job-queue level (enqueue is fast, the job completes correctly) but not yet felt by the user in an actual browser checkout with a real large attachment — also assumes production has a worker process running, same as this dev bench does

## What's left

Per the spec, only **Step 9** remains: let the migrated data sit through a verification window (spec suggests ~1 week) under real usage, then remove the `Daily Task`/`Daily Task Log` doctypes and the now-legacy `test_daily_task_log.py`. Not started.

Also deliberately out of scope for this work (flagged in the original spec as same-release-but-separate performance items, not touched): further `get_page_state()` query consolidation, moving notification emails to `frappe.enqueue()`, HR-manager-email/settings-singleton caching, and a scheduler-job batch-safety audit.
