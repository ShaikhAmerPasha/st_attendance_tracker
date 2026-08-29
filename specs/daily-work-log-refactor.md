# ST Attendance Tracker — Daily Work Log Refactor
## Architecture & Implementation Specification

**App:** `st_attendance_tracker`
**Purpose of this document:** Full technical spec for replacing the current one-document-per-task design with a scalable parent/child model, ahead of an open-source marketplace release.
**Status:** Approved design — ready for implementation.

---

## 1. Problem Statement

### 1.1 Current behavior
- Every task an employee enters (at check-in or check-out) creates one standalone `Daily Task` document.
- Attendance is tracked via two `Daily Task Log` documents per employee per day (`Morning Check-In`, `End of Day`), both submittable (`docstatus` 0→1→2).
- ~100+ employees × ~15 tasks/day → **1,500 new `Daily Task` documents/day**.
- In ~1 month of production use: **2,279 `Daily Task` documents already**, and the site (on Frappe Cloud) has become noticeably slower.

### 1.2 Root cause
`Daily Task` is modelled as a standalone transactional document (autoname series, `track_changes: 1`, full DocType permission table, version history per save) when its real role is **one row in a per-day task list**. This mismatch multiplies document-table overhead by the number of tasks instead of the number of employee-days.

Additional load contributors identified in the current `api.py`:
- `get_page_state()` runs **8–10 DB queries per page load**, several uncached (safety rollover full-table scan, recurring task scan, HR manager email lookup via raw SQL every check-in/check-out).
- Only `_get_team_members()` is Redis-cached (5 min TTL); everything else hits MariaDB directly.
- `Daily Task Log` uses submit/cancel (`is_submittable: 1`) purely to lock edits after EOD — heavier than needed for that purpose.
- Rollover correctness today depends on walking a `rolled_over_from` parent-chain per task (bounded by an explicit `MAX_CHAIN_DEPTH = 3650` — a red flag that the model was already straining).

### 1.3 Scale projection (why this must be fixed now, not later)
| | Current | 10 days | 1 year |
|---|---|---|---|
| **As-is** (1 doc/task) | 2,279 docs (~1 month) | ~15,000 docs | ~450,000 docs |
| **After refactor** (1 doc/employee/day) | — | ~1,000 docs | ~30,000 docs |

Net reduction: **~15× fewer documents**, each cheaper (no autoname series contention, no per-task version rows, no per-task permission evaluation).

---

## 2. Goals

1. Cut document volume ~15× by moving tasks into a child table instead of standalone documents.
2. Cut per-page-load DB queries via targeted Redis caching (beyond the one function already cached).
3. Preserve **100% of current user-facing behavior** — every web page (`daily-checkin`, `my-history`, `team-dashboard`, `management-dashboard`, `recurring-tasks`, `additional-work`) must work identically from the employee's point of view.
4. Replace the fragile parent-chain rollover model with a flat, O(1)-lookup design.
5. Make the app installable/configurable cleanly for **third-party ERPNext sites** (open-source marketplace release) — no StandardTouch-specific assumptions baked into the schema.
6. Zero data loss for the existing 2,279 documents already in production.

---

## 3. New Data Model

### 3.1 `Daily Work Log` (new parent doctype — replaces `Daily Task Log`)

One document per **employee per calendar date**. Not submittable — locking is handled by a flag, not `docstatus`.

| Field | Type | Notes |
|---|---|---|
| `employee` | Link → Employee | required |
| `employee_name` | Data | fetch_from employee.employee_name |
| `department` | Link → Department | fetch_from employee.department |
| `date` | Date | required, default Today |
| `login_time` | Time | |
| `logout_time` | Time | |
| `lunch_from` / `lunch_to` | Time | |
| `net_hours` | Data | computed, same logic as today |
| `working_hours` | Float | sum of task actual_time, same as today |
| `is_late` | Check | computed against `ST Attendance Settings.late_checkin_threshold` |
| `work_location` | Select (Office/WFH/Remote) | |
| `half_day_session` | Select (First Half/Second Half) | |
| `morning_submitted` | Check, default 0 | replaces Morning Check-In docstatus=1 |
| `eod_submitted` | Check, default 0 | replaces End of Day docstatus=1 |
| `locked_at` | Datetime, read-only | set when `eod_submitted` flips to 1 — gives a clean "as of" timestamp for reports/audit, without submit/cancel machinery |
| `tasks` | Table → **Task Entry** | see below |

**Indexes** (`on_doctype_update`):
- `(employee, date)` — composite, covers every lookup pattern in the app (mirrors today's pattern on `Daily Task` and `Daily Task Log`)

**Why not submittable:** see Section 6 for full reasoning. Short version — submit/cancel exists for amend-as-new-version workflows (invoices, etc.); attendance only needs (a) immutability after a point, (b) a change audit trail, (c) a lock action. All three are satisfied by `eod_submitted` + `track_changes: 1`, at lower cost than the docstatus state machine.

### 3.2 `Task Entry` (new child doctype — replaces `Daily Task`)

Lives only inside `Daily Work Log.tasks`. No autoname series, no standalone permissions table, no version history (child rows inherit change tracking from the parent's `track_changes`).

| Field | Type | Notes |
|---|---|---|
| `series_id` | Data, indexed | **stable UUID**, generated once when a task is first created, copied unchanged onto every rolled-over copy — replaces the `rolled_over_from` chain entirely (see Section 4) |
| `origin_date` | Date | date this task's series first appeared |
| `description` | Small Text, required | |
| `project_name` | Data | optional, groups tasks |
| `task_type` | Select (Planned/Ad-hoc/Recurring) | |
| `status` | Select (Pending/In Progress/Done/Rolled Over/Dropped) | |
| `estimated_time` | Float | |
| `actual_time` | Float | |
| `remarks` | Small Text | |
| `sequence` | Int, hidden | preserves entry order, same as today |

Child rows get a real, unique `name` automatically from Frappe (even without explicit autoname) — this is what File attachments key against (Section 5).

**Index:** `series_id` (for the O(1) status-cascade query described in Section 4).

### 3.3 What gets removed
- `Daily Task` doctype (after migration + verification window — see Section 7)
- `Daily Task Log` doctype (folded into `Daily Work Log`)
- The `rolled_over_from` Link field and all chain-walking logic
- The submit/cancel (`is_submittable`) pattern for attendance logs

---

## 4. Rollover Redesign: `series_id` Replaces Parent-Chain Walking

### 4.1 The problem with the current design
Today, a task carried forward multiple days forms a chain:

```
Task-Wed → rolled_over_from → Task-Tue → rolled_over_from → Task-Mon (root)
```

Finding "has this task's lineage been completed anywhere?" or "mark every ancestor Done" requires **walking backward one document at a time**, with a hard-coded `MAX_CHAIN_DEPTH = 3650` guard against cyclic/corrupted data. This is O(n) per operation and was already showing strain (existing code has defensive depth-limiting and orphan-chain detection).

### 4.2 The fix
Every task gets a `series_id` (UUID) the **first time it's created**. Every rolled-over copy — regardless of how many days it's been carried — keeps the *same* `series_id`.

- **Marking a task Done, anywhere in its lineage:** `UPDATE tasks SET status='Done' WHERE series_id = %s` — one indexed query, no walking, no depth limit needed.
- **Preventing duplicate carry-forward:** check `EXISTS (series_id, task_date=next_date)` — same cost as today's "already" check, but without needing to resolve a root first.
- **Traceability preserved:** `origin_date` still tells you when the series started; `series_id` groups every copy. Nothing is lost versus today's chain — it's the same information, without the walk.

### 4.3 Rollover logic (functionally identical UX, simplified implementation)
`_safety_rollover` and `_rollover_pending_tasks` keep their exact current triggers and behavior (safety rollover on page load before check-in; EOD rollover on checkout) — only the internal lookup changes from chain-walk to `series_id` filter. Recurring tasks (`task_type = "Recurring"`) continue to be excluded from carry-forward, unchanged.

---

## 5. File Attachments: Per-Task, Keyed to Child-Row Identity

**Decision:** keep per-task attachments (confirmed requirement — employees attach files to individual tasks in the check-in/check-out UI).

**Mechanism:** Frappe child table rows have a real, unique `name` even without an explicit autoname configured. Attach files with:
```
attached_to_doctype = "Task Entry"
attached_to_name    = <task_entry_row.name>
```

This is an established Frappe pattern (ERPNext itself attaches files to child rows in several places) — it requires **no new wrapper doctype and no UUID field for this purpose specifically** (the `series_id` UUID serves rollover tracking, not file linkage; the row's own `name` handles attachments).

**Code impact — minimal, mechanical rename:**
- `_attach_task_files`: filter by `attached_to_doctype = "Task Entry"`, `attached_to_name in [child_row_names]`
- `_reparent_attachments`: same rename
- `get_task_attachments`, `view_task_attachment`, `delete_task_attachment`: same permission logic (`_assert_task_visible`, `_assert_task_owner`), just resolving the child row's parent (`Daily Work Log`) for the ownership check instead of the task's own `employee` field (child rows don't carry `employee` directly — read it via `parent`)

---

## 6. Why Merge `Daily Task Log` Into `Daily Work Log` (Drop Submit/Cancel)

This was the key open decision — resolved as: **merge, and use a flag instead of docstatus.**

### 6.1 What submit/cancel is actually for
Frappe's `docstatus` state machine (0=Draft, 1=Submitted, 2=Cancelled) exists to support **amend-as-new-version** workflows — e.g., a Sales Invoice that must never be edited after submission, only cancelled and recreated as a fresh, linked document (`amended_from`). This is the right tool for financial/legal documents where the *cancelled record itself* must be preserved immutably as evidence.

### 6.2 Why attendance doesn't need that pattern
What compliance/audit actually requires for attendance is three simpler things, each already available more cheaply:

| Requirement | Mechanism | Cost vs. docstatus |
|---|---|---|
| Immutability after EOD | `eod_submitted` Check field + `validate()` guard | One field read (already loaded with the doc) vs. Frappe's full docstatus permission/workflow check |
| Change audit trail | `track_changes: 1` (Frappe's built-in Version doctype — unchanged from today) | Same either way — already free |
| Lock action | Set `eod_submitted = 1` in the same save | No separate `.submit()` call, no `on_submit()` special-casing |

### 6.3 Concrete cost today that goes away
The current code already shows friction from forcing submit/cancel onto this data: `DailyTaskLog.on_submit()` has to call `self.db_update()` manually after `.submit()` because submittable doctypes don't behave like normal saves. Every submit/cancel also runs Frappe's docstatus-specific permission and workflow-state checks — overhead paid on every one of the ~200–400 daily check-in/check-out actions, for a feature (amend-as-new-version) never used here.

### 6.4 Net effect
One `Daily Work Log` document per employee per day, holding both check-in and check-out data, locked via `eod_submitted` instead of two separate submittable documents. Simpler for third-party implementers to reason about ("a flag, not a state machine"), and the existing `track_changes` Version log already gives any client who wants a stricter audit trail full field-level history for free.

---

## 7. Migration Plan (2,279 existing documents)

**Approach:** Frappe patch (`patches.txt` entry), run once via `bench migrate`.

1. Create new doctypes (`Daily Work Log`, `Task Entry`) — additive, no impact on existing data.
2. Group existing `Daily Task` rows by `(employee, task_date)`.
3. For each group:
   - Create one `Daily Work Log` for that `(employee, date)`.
   - Pull matching `Daily Task Log` rows (Morning Check-In / End of Day) into the new parent's fields directly (`login_time`, `logout_time`, `lunch_from/to`, `net_hours`, `is_late`, `work_location`, `half_day_session`) — `docstatus=1` on either old log maps to `morning_submitted=1` / `eod_submitted=1` respectively.
   - Insert each `Daily Task` as a `Task Entry` child row, preserving `sequence`, `status`, `estimated_time`, `actual_time`, `remarks`, `project_name`.
   - Map `rolled_over_from` chains → generate one `series_id` per chain (walk each existing chain **once**, at migration time only, to assign a shared UUID — this is the last time chain-walking code runs) and set `origin_date` from the existing field.
4. Wrap each employee-day group in `frappe.db.savepoint()` so a bad row aborts only that group, not the whole migration.
5. **Do not delete old doctypes/tables yet.** Keep `Daily Task` and `Daily Task Log` installed but unused by the UI for a verification window (suggested: 1 week) before removing them in a follow-up release.

**Risk:** low — data volume is small (2,279 docs), migration is idempotent per employee-day (safe to re-run), and the old tables remain as a rollback source until verified.

---

## 8. Performance Work Beyond the Schema Change

The schema change fixes document *volume*. These fix per-request *query count*, and should ship in the same release:

### 8.1 Expand Redis caching beyond `_get_team_members()`
Currently only team-membership lookups are cached (5 min TTL). Add caching for:
- `_get_hr_manager_emails()` — changes rarely, currently a raw SQL join run on every single check-in/check-out
- `ST Attendance Settings` singleton reads (`late_checkin_threshold`, `hybrid_office_days`, `standard_workday_hours`) — read on nearly every request, changes essentially never

### 8.2 Reduce `get_page_state()` query count
Currently 8–10 queries per page load. With the new schema, several collapse naturally (one `Daily Work Log` fetch instead of separate `Daily Task Log` + `Daily Task` list queries). Remaining candidates for consolidation:
- Combine the shift lookup + leave lookup into a single batch call where the HRMS API allows
- Cache "does this employee have any active Recurring Task Templates" per employee (changes rarely) instead of scanning on every page load

### 8.3 Move notification emails fully off the request path
`frappe.sendmail(..., now=False)` already queues to Email Queue (good — not a synchronous SMTP call). The remaining win: wrap `_notify_hr_and_team_leader` and the employee confirmation email call itself in `frappe.enqueue()`, so the DB lookups those functions perform (HR email list, team leader resolution, task summary rendering) happen in a background worker instead of inside the check-in/check-out HTTP response.

### 8.4 Scheduler job audit (lower priority, not blocking this release)
The four cron jobs (checkin reminder, combined report, EOD-missing report, checkout reminder) should be checked for per-employee query loops vs. batch queries — flagged for a follow-up pass, not required before launch.

---

## 9. Marketplace / Open-Source Readiness Checklist

Since this is heading to public release, not just an internal tool:

- [ ] No hardcoded company assumptions in schema or logic (branding, `device_id` string, settings singleton — mostly already clean; audit for stragglers during implementation)
- [ ] Migration patch is idempotent and safe to run against any client's existing data shape, not just this dataset
- [ ] Test coverage extended to the new parent/child model before the old doctypes are removed (existing `test_daily_task_log.py` is the baseline to build from)
- [ ] Composite indexes documented and applied via `on_doctype_update` hooks (pattern already established — extend to new doctypes)
- [ ] Scheduler jobs reviewed for batch-safety at arbitrary client scale (Section 8.4)

---

## 10. Summary of What Changes vs. What Stays the Same

**Changes:**
- `Daily Task` (standalone doc) → `Task Entry` (child table row)
- `Daily Task Log` ×2 submittable docs/day → `Daily Work Log` ×1 non-submittable doc/day
- `rolled_over_from` chain → flat `series_id`
- File attachments: `attached_to_doctype` target renamed from `Daily Task` to `Task Entry`
- Redis caching expanded to HR email list + settings singleton
- Notification emails moved to `frappe.enqueue()`

**Stays identical (by design — this was a stated goal):**
- All employee-facing web pages and their behavior (`daily-checkin`, `my-history`, `team-dashboard`, `management-dashboard`, `recurring-tasks`, `additional-work`)
- Rollover UX (pending tasks still carry forward the same way, just cheaper underneath)
- Email report formatting (`_render_screenshot_task_table`, `_render_grouped_task_summary` — both already operate on flat dicts, need zero logic changes)
- Ownership/BOLA guards (`_check_ownership`, `_assert_task_owner`, `_assert_task_visible`) — same rules, re-pointed at the new schema
- Recurring task self-service page and logic

---

## 11. Next Implementation Steps

1. Doctype JSON for `Daily Work Log`
2. Doctype JSON for `Task Entry` (child)
3. Migration patch script (Section 7)
4. Rewritten `api.py` functions for the parent/child model
5. Rewritten controller logic (`daily_work_log.py` replacing `daily_task.py` + `daily_task_log.py`)
6. Updated front-end JS (`daily_checkin.html`, `my_history.html`, etc.) to read/write the new nested task list structure instead of a flat task-document list
7. Test suite extension
8. Verification window, then removal of old doctypes

---

## Implementation Log (this session)

- **2026-08-29:** Discarded an earlier abandoned scaffold found in the repo (`ST Daily Log`/`ST Daily Task`, wrong module path, unregistered patch — did not match this spec's naming or Section 3 field design). Started clean.
- Steps 1–8 of Section 11 implemented and verified against the real `excel` dev site (migrated existing data, ran the full test suite). Full detail, including three real bugs found via hands-on testing (a Frappe `Time`-field auto-default landmine, a rollover/lock-guard interaction, and a dropped business rule) and their fixes, is in `PROGRESS.md`.
- One deliberate deviation from Section 11 step 6 ("Updated front-end JS ... for the new nested task list structure"): it turned out unnecessary. `api.py` keeps returning the same flat task-list JSON shape to every caller (via a small `_task_entry_dict` adapter), so `my_history.html`/`team_dashboard.html`/`management_dashboard.html` needed no changes at all. Only `daily_checkin.html` needed touching, and only because it has two spots that call Frappe's generic `frappe.client.set_value`/`upload_file` directly against a hardcoded `doctype: 'Daily Task'` string — a 3-line mechanical rename to `Task Entry`, verified safe by reading Frappe's own permission-delegation code for child doctypes before making the change (not just assumed, per Section 5's "no new wrapper doctype" claim).
- Only step 9 (verification window, then remove the old doctypes) remains — not started, per the spec's own suggested ~1 week window.
