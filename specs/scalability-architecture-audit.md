# ST Attendance Tracker — Scalability & Architecture Audit

**Date:** 2026-08-27
**Scope:** `Daily Task` and related domain (Daily Task Log, Recurring Task Template, Employee Department Assignment) — architecture, database design, API, permissions, dashboards, scheduler, caching.
**Method:** Static code inspection (Frappe app source, DocType JSON, controllers, API, hooks, scheduler, frontend `www/*`). No production DB was queried; no load test was run.

**Evidence legend** used throughout:
- **[CODE]** — directly observed in this repository (file:line cited).
- **[FRAPPE]** — general Frappe/MariaDB framework behavior, not specific to this app's code.
- **[ASSUMPTION]** — reasonable inference, not directly verifiable from code alone.
- **[NEEDS METRICS]** — requires a production measurement to confirm; a method is given, no number is invented.

---

## Executive Summary

The core concern — "one Frappe document per Daily Task, 2,161 rows in 15 days, will this scale?" — is **not the actual risk**. One-document-per-business-event is standard, well-supported Frappe/MariaDB design and remains fine well past a million rows, provided the table is indexed for its real query patterns. It is not the case here yet.

The real findings:

1. **Indexing is incomplete.** Daily Task has exactly one composite index (`employee, task_date`) **[CODE]**. Filtering the list/dashboards by `status`, `task_type`, `project_name`, or `rolled_over_from` alone currently falls back to a scan of the `employee,task_date` index or a table scan, depending on the optimizer and filter combination **[FRAPPE]**. This does not hurt today at ~2,000 rows, but will show up first in `EXPLAIN` and slow queries once the table is in the tens of thousands.
2. **Several genuine N+1 patterns exist** in `api.py` and `tasks.py` (rollover, root-chain walks, recurring-task generation, scheduler holiday checks, per-employee synchronous email sends). Most are bounded by *team size* or *tasks-touched-per-request*, not by total table size, so they degrade with **usage growth**, not **row-count growth** — an important distinction the audit keeps separate throughout.
3. **No application-level Redis caching exists.** The one Redis-related call in the app (`frappe.clear_cache()` in `www/daily_checkin.py`, run unconditionally on every load of the highest-traffic page) is a site-wide cache **invalidation**, not a cache — this is a mild anti-pattern worth fixing, independent of the caching question.
4. **Management Dashboard** is the one endpoint whose cost scales with **organization size** (department count × per-department query batch) rather than with Daily Task table size — worth watching as headcount grows, not as task count grows.
5. **Permissions are enforced manually per-endpoint**, not via Frappe's `permission_query_conditions`/`has_permission` hooks. This is consistent everywhere in this app's own API, and is acceptable at current scale, but it means any *new* code path (custom report, desk list view, direct `frappe.client` call) bypasses these checks unless it also re-implements them.

None of this requires redesigning the Daily Task model. It requires: a handful of indexes, fixing a handful of specific loops, and light, targeted caching. See [Final Verdict](#final-verdict) for the prioritized list.

---

## Current Architecture

```text
Browser (www/daily_checkin.html, team_dashboard.html, management_dashboard.html,
         my_history.html, recurring_tasks.html, additional_work.html)
    │  frappe.call(...) — JSON-RPC over HTTP
    ▼
Frappe Website Layer
    │  www/*.py  get_context()  — server-rendered page context, one-time per load
    ▼
Whitelisted APIs (st_attendance_tracker/api.py, ~20 @frappe.whitelist() functions)
    │  input normalization, employee resolution, manual authorization checks
    ▼
Business logic (helper functions in api.py: _build_team_data, _get_root_task,
                 _rollover_pending_tasks, _ensure_recurring_tasks, _attach_task_files, ...)
    ▼
DocTypes: Daily Task · Daily Task Log · Recurring Task Template ·
          Employee Department Assignment · ST Attendance Settings · Report Recipient
    ▼
MariaDB (tabDaily Task, tabDaily Task Log, tabRecurring Task Template, ...)

Redis (framework-managed): sessions, background job queue, Frappe's own doctype/meta
cache, Frappe Single-doctype value cache — used automatically by the framework
[FRAPPE]. Not explicitly used by this app's business logic except one
frappe.get_cached_value() call and one frappe.clear_cache() call, both in
www/daily_checkin.py.

Scheduler (hooks.py cron entries → tasks.py):
  10:30 send_employee_checkin_reminder     (Mon-Sat)
  11:30 send_morning_combined_report       (Mon-Sat)
  22:00 send_eod_missing_report            (Mon-Sat)
  22:30 send_employee_checkout_reminder    (Mon-Sat)
Each job scans all active Employees, checks Daily Task Log for that date,
sends email via frappe.sendmail (queued or synchronous, see Scheduler Analysis).
```

No custom background job queue (`frappe.enqueue`) is used anywhere in this app **[CODE — confirmed absent from api.py/tasks.py/hooks.py]**. All work — including per-task inserts during check-in/checkout and all email sends — happens synchronously in the web request or in the cron-triggered scheduler job, not offloaded to a worker queue.

---

## Current Data Model

### Daily Task (`daily_task.json` / `daily_task.py`)

- Standalone DocType, `autoname: "ST-TASK-.YYYY.-.#####"`, not submittable, `track_changes: 1` (every save writes a `Version` row).
- Fields: `employee` (Link), `employee_name`/`department` (fetched, read-only), `task_date`, `task_type` (Select: Planned/Ad-hoc/Recurring), `status` (Select: Pending/In Progress/Done/Rolled Over/Dropped), `project_name` (Data), `description` (Small Text, required, title field), `remarks`, `estimated_time`/`actual_time` (Float), `origin_date`, `rolled_over_from` (self-referencing Link, no index), `daily_task_log` (Link), `sequence` (Int, hidden).
- **One document per task is normal, sound Frappe design.** A task is a real independent business entity: it has its own lifecycle (Pending → In Progress → Done/Rolled Over/Dropped), its own owner, its own audit trail via `track_changes`, is independently queryable, independently permissioned, independently reportable, and can outlive the day it was created on (via rollover). Collapsing multiple tasks into one JSON blob or one "day" document would destroy per-task queryability, per-task permissions, and per-task audit history — there is no technical justification for that here, and the prompt's instruction not to recommend it is correct.
- **Not over-normalized, not under-normalized.** `employee_name`/`department` are fetched/denormalized copies of Employee fields — appropriate use of `fetch_from` for read/display performance and point-in-time historical correctness (an employee's department at task-creation time survives later department changes). This is the right amount of denormalization, not excessive.
- `rolled_over_from` models a **linked-list rollover chain** (today's undone task links back to yesterday's, etc.). This is queryable and correct as a domain model, but it is **not indexed**, and it is walked iteratively in application code up to 3,650 times (`_get_root_task`, `api.py:975-1001`) rather than resolved with a single recursive/CTE query — see [N+1 Audit](#n1-query-audit).
- Only index: `frappe.db.add_index("Daily Task", ["employee", "task_date"])`, created imperatively in `on_doctype_update()` (`daily_task.py:105`) — not declared in the JSON. This is a valid Frappe pattern (`on_doctype_update` is a recognized framework hook, distinct from `doc_events`), but it means the index isn't visible by reading the DocType JSON alone, and no other field/combination is indexed.

### Daily Task Log (`daily_task_log.json` / `daily_task_log.py`)

- **Standalone DocType, not a child table**, and correctly so — it is the attendance check-in/check-out record (one row per employee, per date, per `log_type` ∈ {Morning Check-In, End of Day}), a distinct business concept from a task item. It is `is_submittable: 1` (the only submittable doctype in this domain), which gives it a proper amendment/cancellation lifecycle that Daily Task doesn't need.
- Relationship to Daily Task: **loose, by convention** (`employee` + date), not a hard FK both ways — Daily Task has an optional `daily_task_log` Link field, but Daily Task Log has no reverse link. This is workable but means correlating the two always requires a query, not a join-free lookup. Acceptable at this scale; if reporting across the two grows heavier, consider whether the reverse link is worth adding (P3 — not urgent).
- Duplicate prevention (`_validate_no_duplicate`) is **enforced only in application code** via `frappe.db.exists`, not a DB-level unique constraint. This is a real (if unlikely) race-condition gap: two concurrent submits for the same employee/date/log_type could both pass the `exists` check before either commits. At 2,000 rows this has essentially never mattered; it becomes marginally more likely under heavier concurrent traffic, not under larger table size. Low priority (P2) unless duplicate-log incidents are observed.
- `_calculate_working_hours` runs a `SUM(actual_time)` SQL aggregate over `tabDaily Task` filtered by `employee, task_date` on **every End of Day save** — this correctly benefits from the existing `employee,task_date` index, so it stays cheap even as Daily Task grows, because it's always scoped to one employee/one date.
- Only index: `add_index("Daily Task Log", ["employee", "date"])` (`daily_task_log.py:213`), same pattern as Daily Task.

### Recurring Task Template (`recurring_task_template.json` / `.py`)

- Standalone DocType, hash-named, not submittable. Fields: `employee`, `description`, `project_name`, `estimated_time` (stored as **Data/string**, not Float — a minor modeling inconsistency vs. Daily Task's Float field of the same name, worth normalizing if the template is ever used for aggregation, P3), `is_active`, `recurring_days` (newline-delimited day names).
- Generates Daily Task rows via `_ensure_recurring_tasks` (`api.py:1159-1203`), called on every check-in-page load. **No custom index** on `employee, is_active`, despite that being exactly the filter used (`api.py:1171-1173`) on every such call — the one doctype in this domain that has per-row query traffic but no supporting index at all.
- **Idempotency weakness [CODE]:** the dedup check (`api.py:1182-1187`) matches an existing Daily Task by `employee + task_date + task_type="Recurring" + description text`, not by a template foreign key. Renaming a template's description will not match prior-generated tasks, and could cause a duplicate task to be generated for that day. This is a data-model gap, not a scale problem — worth fixing (P1: cheap, correctness bug) by adding a `recurring_task_template` Link field to Daily Task and matching by that instead of by string.

### Employee Department Assignment

- Child table (`istable: 1`) on Employee, holding `department` + `team_leader`. Frappe gives child tables an implicit index on `parent`, but `team_leader` itself — the field actually filtered on in `_get_team_members` (`api.py:145-150`) — has no explicit index. Currently cheap because child-table rows per Employee are few; becomes relevant only if this table is ever queried standalone across the whole company at scale (it currently is, in `get_management_dashboard`'s eventual team-resolution paths — see [Index Analysis](#index-analysis)).

### ST Attendance Settings / Report Recipient

- ST Attendance Settings is a Single doctype — Frappe stores and caches Single values efficiently by design **[FRAPPE]**; no scale concern here regardless of Daily Task growth.
- Report Recipient is a child-table doctype with **zero references found** in `api.py`/`tasks.py` — HR notification recipients are resolved dynamically by role (`HR Manager`) instead. This appears to be **dead/legacy code**, not a scalability matter, but worth a note: either wire it up or remove it (P3, cleanup only).

### Patches touching this data

`patches.txt` — `cleanup_task_times`, `backfill_work_location`, `backfill_task_sequence`, `backfill_employee_work_type`, `backfill_employee_department_assignments`, `backfill_team_lead_role`. Of these, `backfill_task_sequence.py` has an N+1 shape (one query per employee/date group, one `set_value` per row) — this is a **one-time migration cost**, already paid, not an ongoing concern. Flagged only so a future similar backfill is written batched instead.

**Verdict on the data model: KEEP the current Daily Task = 1 document design.** See [Architecture Alternatives](#architecture-alternatives) for the full comparison.

---

## Current Production Growth

Given: 2,161 Daily Task documents created in approximately 15 days.

- Rate ≈ 2,161 / 15 ≈ **144 tasks/day** (blended average; actual daily rate almost certainly varies with weekday/weekend and headcount — **[ASSUMPTION]** that 15 days is representative, not verified against per-day breakdown).

This is the only real production number available. Everything below is a projection built on it for capacity-planning purposes, not a forecast.

---

## Scale Projections

All projections extend the observed **144 tasks/day** blended rate. **[ASSUMPTION — see caveat above.]**

| Horizon | Scenario A (1×, current rate) | Scenario B (3×) | Scenario C (10×) |
|---|---|---|---|
| 1 month | ~4,300 | ~13,000 | ~43,000 |
| 3 months | ~13,000 | ~39,000 | ~130,000 |
| 6 months | ~26,000 | ~78,000 | ~260,000 |
| 1 year | ~52,000 | ~157,000 | ~525,000 |
| 2 years | ~105,000 | ~315,000 | ~1,050,000 |
| 3 years | ~158,000 | ~473,000 | ~1,575,000 |
| 5 years | ~263,000 | ~788,000 | ~2,625,000 |

**Scenario A — current usage.** Same team, same creation rate. Table reaches ~52K in year 1, ~260K by year 5. This is comfortably within normal MariaDB/InnoDB single-table performance envelopes given adequate indexing **[FRAPPE]**.

**Scenario B — 3× growth.** Larger team or heavier per-person task logging. ~157K in year 1, ~788K by year 5. Still a normal InnoDB table size; index quality starts to matter in practice around this range, not just in theory.

**Scenario C — 10× growth.** Company-wide large-scale adoption. Crosses 1M within 2 years. This is where archival/partitioning conversations become worth having in earnest (see [Should We Archive Old Tasks?](#should-we-archive-old-tasks)) — not because MariaDB can't hold 1–2M rows in one table (it routinely does), but because unindexed or badly-filtered queries against that size become materially slower than the same query at 50K rows, and the report/dashboard/list-view query patterns identified in this audit would need the indexing fixes recommended below to still perform well.

Do not treat any single cell above as a commitment — re-derive the rate quarterly from actual row counts (see [Monitoring Plan](#monitoring-plan)).

---

## Database Analysis

### Will large numbers of documents slow Frappe? — direct answer

**No, not inherently.** 2,000, 10,000, 100,000, and 1,000,000 rows in a single MariaDB InnoDB table are all, by themselves, unremarkable **[FRAPPE]**. MariaDB/InnoDB is routinely used for tables far larger than any projection above. "One task = one Frappe document" is not a bad design at any of these scales.

What actually causes degradation, in order of relevance to this specific app:

1. **Missing indexes for the actual filter/sort combinations used** — causes full table scans or filesort, and this is the one confirmed gap in this codebase (see [Index Analysis](#index-analysis)). This is the dominant risk here.
2. **N+1 query patterns** — cost scales with *rows touched per request* (tasks-per-checkin, employees-per-team, templates-per-employee), not directly with total table size. Confirmed present in several places (see [N+1 Query Audit](#n1-query-audit)); most are bounded by human-scale numbers (a person doesn't carry 500 unfinished tasks) and are low risk; a few (root-chain walk, scheduler per-employee holiday check) deserve fixing regardless because they're cheap to fix and their worst case is unbounded-ish.
3. **Unbounded aggregation/reporting queries** — `get_management_dashboard`'s per-department loop scales with department count, not task count; still worth watching as the org grows (see [Dashboard Analysis](#dashboard-analysis)).
4. **Excessive/unscoped cache invalidation** — the `frappe.clear_cache()` call on every daily-checkin page load doesn't get *worse* as Daily Task grows, but it is unconditional, site-wide, per-request overhead that's unrelated to what that page actually needs to invalidate. Independent finding, fix regardless of scale.
5. **Synchronous email sends inside scheduler loops** (`now=True` per employee) — this scales with *active employee count*, not Daily Task row count. A company with hundreds of employees would see this scheduler job's wall-clock time grow linearly; with the current headcount implied by 2,161 tasks/15 days, this is not yet a concern.
6. **`track_changes: 1` on Daily Task / Daily Task Log** — every save writes a Version document. This is intentional (audit trail) and standard Frappe behavior; it does add one extra row-write per save and one more table (`tabVersion`) that grows alongside Daily Task. Not a defect, but relevant to [Storage Analysis](#storage-analysis).

None of "excessive document hooks," "large text/blob fields," or "attachment storage" are structural problems in this app specifically: hooks are lean (a handful of single-row lookups per save, not scans), fields are all small Data/Select/Float/Date types (no long-text or blob fields on Daily Task itself), and attachments are queried in a batched, non-N+1 way (`_attach_task_files`, single `IN (...)` query).

---

## Index Analysis

Confirmed via repository-wide search: **zero** `"index": 1` / `"unique": 1` flags in any DocType JSON in this app. All three indexes that exist are created imperatively:

| Table | Index | Source |
|---|---|---|
| `tabDaily Task` | `(employee, task_date)` | `daily_task.py:105`, `on_doctype_update` |
| `tabDaily Task Log` | `(employee, date)` | `daily_task_log.py:213`, `on_doctype_update` |
| `tabAdditional Work` | `(employee, work_date)` | `additional_work.py:57`, `on_doctype_update` |

Every other table in this domain (`Recurring Task Template`, `Employee Department Assignment`) has no custom index at all, beyond whatever MariaDB/Frappe adds automatically (primary key on `name`, implicit `parent`/`parenttype` index on child tables **[FRAPPE]**).

### Recommendations

For each: `Query pattern / Existing index / Problem / Recommended index / Reason / Trade-off`.

**1. Daily Task filtered by status/task_type/project_name without employee/date**
- Query pattern: dashboard/report/list-view queries that filter by `status` alone (e.g., "all Pending tasks"), or `task_type` alone, or `project_name` alone — none of which is prefixed by `employee` or `task_date`.
- Existing index: `(employee, task_date)` — cannot be used to satisfy a filter that doesn't start with `employee`.
- Problem: MariaDB falls back to a table scan for these filters **[FRAPPE]**. At 2,000 rows this is invisible; at 100K+ rows a "all Pending tasks across the company" style query (which `get_management_dashboard`/reporting could plausibly need) becomes a full scan.
- Recommended index: `(status, task_date)` if status-first filtering with a date bound is the real pattern (matches how `_safety_rollover`/`_rollover_pending_tasks` already filter by status + date range, `api.py:1049,1105-1111`).
- Reason: covers the actual rollover/report query shape without duplicating the existing `(employee, task_date)` index's purpose.
- Trade-off: one more index to maintain on every insert/update that changes `status` or `task_date` (which is most saves) — acceptable given how frequently this filter shape is used internally (`_rollover_pending_tasks`, `_safety_rollover` both scan by status+date today without index support).

**2. Recurring Task Template filtered by employee + is_active**
- Query pattern: `frappe.get_all("Recurring Task Template", filters={"employee": employee, "is_active": 1})` — `api.py:1171-1173`, executed on every check-in page load per employee.
- Existing index: none.
- Problem: table scan of Recurring Task Template on every single check-in-page load, company-wide. Currently cheap only because the table itself is small (bounded by templates-per-employee, likely low hundreds total); becomes worth fixing before this table grows into the thousands.
- Recommended index: `(employee, is_active)`.
- Reason: exact match for the only query pattern against this table found in the codebase.
- Trade-off: negligible — small table, infrequent writes (only on template create/edit/toggle), cheap index to maintain.

**3. Employee Department Assignment filtered by team_leader**
- Query pattern: `frappe.get_all("Employee Department Assignment", filters={"team_leader": ...})` — `api.py:145-150`, used to resolve a Team Leader's managed employees, called (uncached) from multiple request paths (`_assert_task_visible`, `get_page_state`, `get_team_dashboard`, `www/daily_checkin.py`, `www/team_dashboard.py`).
- Existing index: only the implicit `parent`/`parenttype` index Frappe gives child tables **[FRAPPE]** — does not help a `team_leader`-only filter.
- Problem: child-table scan on every team-leader-resolution call, and this function is called **repeatedly, uncached, per request** in several places. This is the higher-priority half of the fix — the missing index matters less than the missing cache here (see [Redis/Cache Analysis](#redis--cache-analysis)).
- Recommended index: `(team_leader)`.
- Reason: matches the actual filter; child tables can carry custom indexes like any other Frappe table.
- Trade-off: negligible at current scale (this table's row count tracks Employee count, not Daily Task count, so it stays small regardless of task growth).

**4. Daily Task rolled_over_from (ancestor-chain walk)**
- Query pattern: `_get_root_task` (`api.py:975-1001`) walks `rolled_over_from` iteratively via `frappe.db.get_value`/`frappe.db.exists`, one hop at a time, up to 3,650 iterations; also used in `submit_eod_log`'s future-task lookup (`filters={"rolled_over_from": root, "task_date": [">", date]}`, `api.py:1839`).
- Existing index: none on `rolled_over_from`.
- Problem: each hop is a point lookup by `name` (primary key, always fast) — so the *walk itself* isn't hurt by a missing index. The `filters={"rolled_over_from": root, ...}` lookup, however, **is** a table scan for that field today.
- Recommended index: `(rolled_over_from, task_date)`.
- Reason: directly supports the future-tasks-under-a-root query in `submit_eod_log`, which currently has no index support.
- Trade-off: small — `rolled_over_from` is only set on rollover-created tasks (a minority of rows), so index maintenance cost is proportionally lower than a full-column index would suggest.

### General caution on indexing

Every index above adds INSERT/UPDATE cost and storage. These four are recommended because each maps to an actual, already-executing query pattern in the code — not speculative "might filter by this someday" additions. Do not add more without a concrete matching query pattern (per the user's own instruction).

---

## Query Analysis

Confirmed patterns and their risk classification:

| Query location | Pattern | Risk at current scale | Risk driver |
|---|---|---|---|
| `_rollover_pending_tasks` / `_safety_rollover` (`api.py:1043-1156`) | `status IN (...) AND task_date < X`, unbounded lower bound | Low now, grows with **table size** (not just usage) since there's no lower date bound | Table scan risk once Daily Task is large and old Pending/In-Progress tasks accumulate |
| `get_management_dashboard` per-department loop (`api.py:2009-2130`) | N queries where N = department count, each batched internally | Low-medium, grows with **org size** | Query count, not row scan |
| `get_my_history` (`api.py:2178-2228`) | Raw SQL with `LIMIT/OFFSET`, properly paginated | Low, bounded | None significant |
| `get_additional_work` (`api.py:1273-1289`) | Proper `start`/`page_length` pagination | N/A — **dead code**, not called from any frontend page | None — flag for removal or wiring up (P3) |
| Daily Task desk list view | Frappe default list-view query, sort `task_date DESC`, backed only by `(employee, task_date)` index | Low now; degrades for filters not employee/date-prefixed as table grows | See [Index Analysis](#index-analysis) |

### Full-table-scan candidates once Daily Task is large
- `_safety_rollover`'s unbounded-lower-bound date scan (`api.py:1105-1111`) — worth adding a reasonable lower bound (e.g., 90 days) once historical volume grows, both for query cost and because "roll over a task from 2 years ago" is unlikely to be desired product behavior anyway. **P2.**
- Any future report/list filter on `status`/`task_type`/`project_name` alone — covered by the index recommendations above.

### Filesort / temporary table risk
- The Daily Task desk list view sorts `task_date DESC` — this is supported by the existing composite index's second column when also filtered by `employee`, but an unfiltered company-wide sort by `task_date` alone (e.g., an HR user browsing without an employee filter) is not covered and would use filesort **[FRAPPE]** at larger row counts. Low priority unless that specific browsing pattern is common — verify via slow query log ([Monitoring Plan](#monitoring-plan)) rather than pre-optimizing.

---

## API Analysis

All ~20 `@frappe.whitelist()` functions live in `api.py`. Summary of the ones that touch Daily Task or drive the dashboards:

| API | Purpose | Queries | Pagination | Caching | Bottleneck risk |
|---|---|---|---|---|---|
| `get_page_state` | Full check-in page payload | ~8-10 sequential single-row/small queries, calls `_safety_rollover`/`_ensure_recurring_tasks` inline | N/A (single employee/day) | None | Low at current scale; inline rollover/recurring-generation adds latency to every page load pre-checkin |
| `submit_morning_log` | Commit morning check-in + carried/new tasks | Row lock + N+1 loops over carried tasks and new tasks (see [N+1 Audit](#n1-query-audit)) | N/A | None | Scales with tasks-per-submission (small, bounded by one person's daily task count) |
| `submit_eod_log` | Commit EOD + task updates | Row lock + N+1 loops, root-chain walks, future-task delete loop (see [N+1 Audit](#n1-query-audit)) | N/A | None | Same as above; worst case scales with rollover chain depth |
| `get_team_dashboard` | Team Leader's team view | `_get_team_members` (2-3 queries) + `_build_team_data` (batched, ~4 queries total for whole team) | None (team size implicitly small) | None | Low — correctly batched, not per-employee looped |
| `get_management_dashboard` | HR/Management org-wide view | Per-department loop (~5 queries × dept count) + 7 more global queries | None (whole org in one response) | None | Medium — scales with department count; see [Dashboard Analysis](#dashboard-analysis) |
| `get_employee_task_detail` | Single employee's day detail (drill-down) | ~4 single-row/batched queries | N/A | None | Low |
| `get_my_history` | Employee's own history, paginated | 2 raw SQL queries, properly `LIMIT/OFFSET` | Yes, correct | None | Low |
| `get_history_day_detail` | One day's task detail (lazy-loaded) | ~3 queries, client-side cached after first load | N/A | Client-side only | Low |
| `get_recurring_tasks` | List employee's templates | 1 query | None (small per-employee set) | None | Low |

No API in this list returns an unbounded, un-paginated, company-wide Daily Task result set — the closest is `get_management_dashboard`, whose response size scales with department/employee count, not with historical Daily Task volume (it's always scoped to one date). This is a meaningful distinction: **the dashboards are safe from Daily-Task-table growth**; they are only sensitive to **organization growth** (more departments/employees), which is a much slower-moving number.

---

## Permission Analysis

- **No `has_permission` or `get_permission_query_conditions` hook is registered anywhere in this app** [CODE — confirmed absent from `hooks.py` and every doctype controller]. Row-level access control (Employee self-service vs. Team Leader vs. HR/Management) is implemented as **manual checks inside each whitelisted function** — `_assert_task_visible`, `_get_team_members`, explicit role checks in `get_team_dashboard`/`get_management_dashboard`/`get_employee_task_detail`.
- This is consistent and correctly applied across this app's own API surface. Its scalability implication is **not about row count** — these are single/small-set lookups, index-friendly once the [Index Analysis](#index-analysis) recommendations are applied — it's an **architectural risk**: any access to Daily Task/Daily Task Log outside this app's whitelisted functions (a Frappe Desk list view, a custom Report doctype, `frappe.client.get_list` called directly) is **not** scoped by these rules, because Frappe's own permission-query-condition framework isn't hooked in. The DocType-level `permissions` block (Employee `if_owner:1`, HR Manager full access) is the only enforcement Desk/Report/direct-API access gets — Team Leaders have **no** DocType-level row permission for Daily Task, so a Team Leader using Desk's list view directly (bypassing `/team-dashboard`) cannot see their team's tasks there at all, and conversely nothing stops an Employee-role Desk user from writing a custom Report against Daily Task without the app's manual scoping applying.
- `_get_team_members` (`api.py:131-152`) is called **repeatedly and uncached**, 2-3 queries each time, from multiple request paths per page load. Not a row-count risk (Employee/assignment tables stay small relative to Daily Task), but a per-request query-count and latency issue — see [Redis/Cache Analysis](#redis--cache-analysis) for a concrete caching recommendation.
- Permission checks are index-friendly (once [Index Analysis §3](#index-analysis) is applied) and do not compound per Daily Task row — they resolve team membership once, then filter Daily Task by the resolved employee list in a single batched query (`_build_team_data`). No per-row permission check was found.

---

## UI / List View Analysis

- Daily Task desk list view: `sort_field: task_date`, `sort_order: DESC`; `in_list_view` fields: `employee`, `task_date`, `task_type`, `status`, `project_name`, `description`. **No field has `in_standard_filter` set** — no doctype-specific quick filters beyond Frappe's global default filter UI. No custom list-view JS exists for Daily Task (`doctype_list_js` not registered).
- This means the desk list view runs entirely on Frappe's default list-view query behavior, backed only by the `(employee, task_date)` index. Filtering by `status`/`task_type`/`project_name` in the desk UI today falls back to the scan behavior described in [Index Analysis](#index-analysis).
- At 100K rows: filtered-by-employee-or-date browsing stays fast (indexed); filtering by status/type alone gets progressively slower without the recommended `(status, task_date)` index. At 500K-1M: same shape, more pronounced — this is precisely the scenario the index recommendations are aimed at pre-empting.
- None of this requires the desk list view itself to change — it requires the underlying index, which benefits the list view for free.

---

## Dashboard Analysis

| Page | Data flow | API calls (page load) | Major queries | Complexity | Bottleneck | Scalability rating |
|---|---|---|---|---|---|---|
| `/daily-checkin` | Server context (`get_context`) + client calls on submit | 1 (`get_page_state`) load, 1 each on submit actions | ~10 small/single-row queries | O(1) per employee/day | `frappe.clear_cache()` called unconditionally on every load (site-wide invalidation, unrelated to page data) | **Needs Improvement** (the clear_cache call, not the query pattern) |
| `/team-dashboard` | 1 API call on load, client-cached in-memory for drill-down | 1 (`get_team_dashboard`) | ~5 queries, all batched by `employee IN (...)` | O(team size) | None significant | **Good** |
| `/management-dashboard` | 1 API call on load, 1 per employee-card click (drill-down) | 1 + N (on demand) | ~5 × department count + 7 global | O(department count) | Grows with org size; not row-count-driven | **Acceptable**, watch as departments grow |
| `/my-history` | Paginated load + lazy per-day detail, client-cached | 1 per page ("load more"), 1 per day expand (cached after first) | Raw SQL, `LIMIT/OFFSET` | O(page size) | None | **Good** |
| `/recurring-tasks` | Full refetch after every mutation | 1 load + 1 per save/toggle/delete (full list refetch each time) | 1 query, unbounded but small (per-employee templates) | O(templates per employee) | Minor: full-list refetch after single-field toggle is wasteful but cheap at this table size | **Acceptable** |
| `/additional-work` | Form-only, no list rendering | 1 call **per task item** on submit (confirmed client-side N+1) | 1 insert per item, sequential/awaited | O(items submitted) | Real, user-visible latency scaling with items entered in one submission | **Needs Improvement** |

**Management Dashboard is correctly flagged as the one to watch**, per the user's specific concern — but the growth axis is **department/employee count**, not Daily Task row count. Its per-department loop (`api.py:2022-2032`) issuing ~5 queries per department is the mechanism; fixing it (batch across departments in one query using `department IN (...)`, matching the pattern `_build_team_data` already uses for employees) is a worthwhile P2 improvement well before it becomes a real problem, since the fix is straightforward and the current shape is the only place in this app where the same data (Daily Task Log for a date) is queried multiple times redundantly (once per department internally, then again 2-3 times globally for rankings).

---

## Scheduler Analysis

`hooks.py` registers 4 cron jobs, all Mon-Sat, all implemented in `tasks.py`. All four:

- Are gated by `_already_sent_today` (`tasks.py:414-433`) — a row-locked (`SELECT ... FOR UPDATE`) Single-doctype date marker checked before any work runs. **This makes all four jobs duplicate-safe**: a second scheduler fire on the same calendar day is a no-op. Per the code's own transaction-boundary assumption (marker write rides in the scheduler wrapper's commit/rollback), a job that raises mid-run should not falsely mark itself sent — this relies on Frappe's scheduler transaction handling **[FRAPPE, not independently verified in this audit]**.
- All call `_get_expected_employees(date)` (`tasks.py:436-455`), which does a **full scan of all active Employees** on every run, then loops per-employee calling `_is_holiday` (`tasks.py:459-465`) — one query per employee, **not deduplicated by shared holiday list**. This scales with **active employee count**, not with Daily Task volume. At current headcount this is cheap; if headcount grows substantially, this loop (and the per-employee synchronous `frappe.sendmail(..., now=True)` calls in `send_employee_checkin_reminder`/`send_employee_checkout_reminder`) will make the job's wall-clock time grow linearly with employee count. **P2**: memoize `_is_holiday` results by `holiday_list_name` within a single job run (most employees likely share one of a small number of holiday lists), and consider queuing emails (`frappe.sendmail(..., now=False)`) rather than sending synchronously per recipient inside the cron job.
- **None of the four jobs' query cost grows with Daily Task table size** — they all filter Daily Task Log by `date` (today), which is a bounded, indexed lookup regardless of how many historical rows exist.
- Jobs are idempotent (verified above) and implicitly batchable within a run (queries are already `IN (...)`-batched by employee list, not per-employee looped for the DB reads — only the holiday check and the email send are looped).

---

## Redis / Cache Analysis

**1. Is Redis available/used by Frappe?** Yes — Frappe uses Redis automatically for caching, the background job queue, and session storage **[FRAPPE]**. This is present and working regardless of anything this app does.

**2. Does this application explicitly use Redis?** Effectively no.
- `frappe.get_cached_value("Shift Type", ...)` in `www/daily_checkin.py:179` — the one genuine read-through cache usage in the app.
- `frappe.cache().delete_value(...)` ×2 and `frappe.clear_cache()` in `www/daily_checkin.py:11-13` — these are cache **invalidation** calls, not caching, and they run unconditionally on **every single load** of the app's highest-traffic page. `frappe.clear_cache()` specifically is a site-wide cache clear (hooks/bootinfo/doctype-meta caches for all users, not scoped to this request) **[FRAPPE]**. This looks like leftover debugging/workaround code rather than intentional cache management — recommend removing it and, if it was added to fix a specific stale-cache symptom, root-causing that symptom instead. **P1** — cheap fix, currently adds unnecessary per-request overhead and needless cross-user cache churn, independent of Daily Task scale.
- No other `frappe.cache`/`redis`/`cached_value` usage exists in `api.py` or `tasks.py` — confirmed by repository-wide search.

**3. What is currently cached?** Only Shift Type lookups (via `get_cached_value`) and whatever Frappe caches automatically at the framework level (doctype meta, sessions, Single values).

**4. What is not cached?** Team membership resolution (`_get_team_members`), which is recomputed via 2-3 fresh queries on every call and is called repeatedly per request across multiple endpoints.

**5. Where would caching actually help?**

| Cache key | Value | TTL | Invalidation | Expected benefit | Staleness risk |
|---|---|---|---|---|---|
| `st_att:team_members:{team_leader_employee}` | List of managed employee names | 5-10 min | Time-based only, or on Employee Department Assignment save (`on_update` hook) | Removes 2-3 queries per call from `_assert_task_visible`, `get_page_state`, `get_team_dashboard`, both `www/*.py` context builders — this is the single highest-value cache in the app given how often it's called uncached today | Low — team membership changes infrequently; a few minutes of staleness means a newly-reassigned employee's tasks are briefly invisible/visible to the wrong Team Leader, acceptable for a dashboard view, not for an authorization-critical write path (do **not** rely on this cache for the actual authorization check itself — keep `_assert_task_visible`'s final decision on a fresh lookup or a short TTL) |
| `st_att:hr_manager_emails` | List of HR Manager user emails | 15-30 min | Time-based, or on User/Has Role change (harder to hook cleanly — time-based is simpler and adequate) | Removes a raw-SQL join from every notification send (currently re-run per check-in/checkout/report) | Very low — HR Manager role assignment changes rarely, and a few minutes of staleness on a notification recipient list has no correctness impact |

**6. Where would caching create stale-data problems (do not cache these)?**
- Daily Task / Daily Task Log contents themselves — these change constantly within a single check-in/checkout flow; caching them would immediately create correctness bugs.
- `_check_ownership`/`_assert_task_visible`'s final authorization decision — caching the *input* (team membership) is fine with a short TTL; caching the *decision* itself is not, since it directly gates data access.
- Anything already backed by Frappe's own Single-value cache (`ST Attendance Settings` reads via `frappe.db.get_single_value`) — Frappe already caches Singles efficiently **[FRAPPE]**; adding another caching layer on top would be redundant, not beneficial.

Do not cache Daily Task list/dashboard results wholesale — none of the dashboard queries are expensive enough at current or Scenario-B scale to justify the staleness risk; revisit only if [Monitoring Plan](#monitoring-plan) shows management-dashboard latency becoming a real problem as department count grows.

---

## Storage Analysis

Tables whose storage will grow alongside Daily Task usage:

- `tabDaily Task` — primary growth table.
- `tabDaily Task Log` — grows with check-in/checkout activity (2 rows/employee/day at most), much slower than Daily Task.
- `tabVersion` — grows because `track_changes: 1` is set on both Daily Task and Daily Task Log; every edit adds a Version row storing a JSON diff. This is the storage cost most likely to be *underestimated*, since it's not visible by looking at the Daily Task table alone. **[NEEDS METRICS]** — measure this table's size relative to Daily Task's (method below).
- `tabComment`, `tabFile` — grow only with actual comment/attachment usage; attachments are already queried in a batched, non-N+1 way, so this is a pure storage question, not a query-performance one.
- Frappe's own `Error Log` / `Email Queue` tables — grow with failures/notifications sent; the scheduler's per-employee `frappe.sendmail` calls (4 jobs × active-employee-count, up to 6 days/week) are the main driver of Email Queue volume in this app specifically.
- Index storage — each recommended index in [Index Analysis](#index-analysis) adds its own on-disk structure; InnoDB secondary indexes are typically a modest fraction of table size, exact figure depends on data — measure, don't estimate.

**How to measure (safe, read-only), from a bench/MariaDB shell:**

```sql
-- Row count and approximate size of a specific table
SELECT table_name, table_rows,
       ROUND(data_length/1024/1024, 2)  AS data_mb,
       ROUND(index_length/1024/1024, 2) AS index_mb
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND table_name IN ('tabDaily Task', 'tabDaily Task Log', 'tabVersion',
                      'tabRecurring Task Template', 'tabComment', 'tabFile');

-- All tables, largest first — identify what's actually consuming storage
SELECT table_name, table_rows,
       ROUND((data_length + index_length)/1024/1024, 2) AS total_mb
FROM information_schema.tables
WHERE table_schema = DATABASE()
ORDER BY (data_length + index_length) DESC
LIMIT 20;
```

Or via bench: `bench --site <site> mariadb`, then run the above. `SHOW TABLE STATUS LIKE 'tabDaily Task'\G` gives a quicker single-table summary. None of these commands modify data.

---

## Current Bottlenecks

Issues visible today in the code, independent of future scale:

1. `frappe.clear_cache()` on every `/daily-checkin` page load (`www/daily_checkin.py:11-13`) — unconditional, site-wide, unrelated to page data. **[CODE]**
2. `additional_work.html`'s submit handler issues one sequential `frappe.call` per task item instead of a batched submit (unlike the equivalent daily-checkin flows). **[CODE]**
3. Recurring Task Template dedup matches by description string, not a template FK — rename desyncs idempotency, risking duplicate generated tasks. **[CODE]**
4. `Report Recipient` doctype exists with a full permission/field model but is never queried by the app — dead code. **[CODE]**
5. No index on the `Recurring Task Template(employee, is_active)` filter used on every check-in page load. **[CODE]**

None of these are scale-driven — they are correctness/cleanliness issues that happen to also be worth fixing before volume grows, because they get slightly more expensive (not qualitatively worse) at higher row/request counts.

## Future Bottlenecks

**Near-term (tens to low hundreds of thousands of Daily Task rows):**
- Status/type/project-only filters on Daily Task without employee/date scoping become measurably slower without the recommended `(status, task_date)` index.
- `_safety_rollover`'s unbounded lower-date-bound scan grows with total historical Pending/In-Progress task volume.
- `_get_team_members` uncached, repeated per request, becomes a larger fraction of request latency as request volume (not row count) grows.

**Long-term (very large scale, hundreds of thousands to millions):**
- Management Dashboard's per-department query loop, if department count grows substantially (this tracks org structure, which grows far slower than task volume).
- Scheduler's per-employee holiday-check + synchronous email loop, if active employee count grows substantially.
- `tabVersion` storage growth from `track_changes: 1`, worth periodic measurement rather than a code change.

None of these are urgent architectural risks; they are the specific, concrete things to revisit as the relevant driver (row count vs. request volume vs. org size — three genuinely different axes) actually moves.

---

## Capacity Thresholds

Not hard Frappe limits — engineering checkpoints to re-run this audit's measurement steps at.

| Daily Task rows | What to check |
|---|---|
| < 10K | Nothing — current design is comfortable here regardless of indexing. |
| 10K – 100K | Confirm the 4 recommended indexes are in place; check `EXPLAIN` on the desk list view's most common filters; check management-dashboard latency if department count has also grown. |
| 100K – 500K | Re-measure `tabVersion` size vs. Daily Task size; confirm `_safety_rollover`'s date-scan hasn't become a slow-query-log regular; verify Daily Task Log's duplicate-guard hasn't caused any observed race (still no DB unique constraint at this point unless added). |
| 500K – 1M | Revisit whether `_get_team_members` caching (recommended above) has actually been implemented — this is the point where its uncached cost is most likely to be noticeable in aggregate request volume, not row count per se. |
| 1M+ | Formally evaluate archival (see next section) using actual query patterns against historical data (how often is a task older than N months actually read?) rather than assuming it's needed. |

---

## Architecture Alternatives

| Approach | Queryability | Frappe fit | Permissions | Concurrency | Reporting | Storage | Maintainability | Scalability | Complexity |
|---|---|---|---|---|---|---|---|---|---|
| **Current: 1 Daily Task = 1 document** | Excellent — native filters/sorts/reports | Native | Standard DocType permissions + manual app-level scoping | Standard row-level locking, no contention issues found | Native Frappe reports work directly | 1 row/task, proportional | High — idiomatic Frappe | Good, with the indexing fixes above | Low |
| **Parent "day" document + child table of tasks** | Worse — child-table rows aren't independently reportable/permissioned/linkable the way standalone docs are; querying "all Pending tasks across employees" becomes a child-table join instead of a simple filter | Non-native for this use case — fights how tasks actually behave (independent status transitions, rollover across days, per-task attachments/comments) | Harder — child tables inherit parent permissions, can't independently restrict a single task | Editing one task means saving the whole parent "day" document — a real concurrency regression if two processes touch different tasks the same day | Worse for per-task analytics | Similar total storage, less flexible per-row metadata (Version/Comment/File targeting a child row is awkward in Frappe) | Lower — fights the rollover-across-days model that already exists | Not better — introduces new problems without solving one that exists | Higher — a real rewrite |
| **Event/ledger model** (append-only task-state-change log) | Good for "what happened when," worse for "what is the current state of task X" (requires materialized/derived current-state view) | Non-native — no framework support, would be entirely custom | Would need entirely custom, non-standard permission logic | Better for high-concurrency append-only workloads — not a problem this app has | Requires building aggregation logic from scratch; Frappe reports don't understand ledger patterns | More total rows (every state change, not just current state) | Lower — team must maintain custom query logic Frappe doesn't provide out of the box | Solves a concurrency/audit problem this app doesn't have | Highest |
| **Archive DocType / partitioning** | Same as current for active data; requires a second query path for historical data | Frappe has no native partitioning support; archival would be an app-level "move old rows to a second table" pattern | Same rules would need re-implementing for the archive table | No change | Cross active+archive reporting requires a UNION or two queries | Reduces active-table size, doesn't reduce total storage | Adds a second code path to maintain (query active vs. archive, or both) | Genuinely helps only past very large scale (see below) | Medium |

**Verdict: KEEP the current model.** Parent+child and event/ledger alternatives would each solve problems this app does not have (concurrent editing of a shared parent document, high-frequency state-change auditing) while making the problems it does need to solve (per-task queryability, per-task permissions, rollover-across-days) harder. **OPTIMIZE** via the indexing and query fixes in this audit is the correct action, not a structural redesign.

---

## Should We Archive Old Tasks?

Not yet, and not automatically at any specific age. Archival becomes useful when:
- Historical rows are rarely read but their presence measurably slows queries against *active* data — this requires the [Index Analysis](#index-analysis) fixes to be ruled out first as the actual cause, since an unindexed query against 1M rows looks identical to "we need archival" when the real fix is a missing index.
- Storage cost of retaining full history becomes a real concern — not indicated by anything in this audit; MariaDB comfortably stores millions of small rows.
- Compliance/retention policy requires it — a business decision, not a technical one; nothing in the codebase or this prompt indicates such a requirement exists today.

Archival creates real ongoing complexity (a second query path, permissions re-implemented for a second table, cross-table reporting) — per [Architecture Alternatives](#architecture-alternatives). Recommend **deferring** this decision until the Capacity Thresholds table's 500K-1M checkpoint, and only after confirming (via `EXPLAIN`, per [Query Profiling](#query-profiling)) that indexing fixes alone are insufficient. This is a P3 item.

---

## Improvements Needed Now

See [Final Verdict](#final-verdict) P0/P1 rows for the concise list. In prose:
- Fix `frappe.clear_cache()` misuse in `www/daily_checkin.py` (cheap, currently active overhead on the highest-traffic page).
- Fix `additional_work.html`'s per-item sequential submit loop (cheap, currently user-visible latency).
- Add the 4 recommended indexes (cheap, prevents a known-shaped future slowdown).
- Fix Recurring Task Template's string-based dedup (cheap, active correctness risk, not just a performance one).

## Improvements Needed Later

- Cache `_get_team_members` and HR manager email resolution (moderate effort, benefit grows with request volume).
- Batch `get_management_dashboard`'s per-department loop into fewer queries (moderate effort, benefit grows with org size).
- Deduplicate the scheduler's per-employee holiday check by holiday list; consider queuing scheduler emails instead of synchronous per-recipient sends (moderate effort, benefit grows with headcount).
- Add a lower date bound to `_safety_rollover`'s scan (small effort, benefit grows with historical task volume).
- Revisit Daily Task Log's app-level-only duplicate guard if any race-condition incident is ever observed.

## Monitoring Plan

Track (all via read-only queries/commands, no production load testing):

| Metric | How | Cadence |
|---|---|---|
| Daily Task / Daily Task Log row counts | `SELECT COUNT(*) FROM \`tabDaily Task\`;` (and same for Daily Task Log) | Weekly |
| DB size, largest tables | `information_schema.tables` query from [Storage Analysis](#storage-analysis) | Monthly |
| Slow queries | MariaDB slow query log (`slow_query_log = ON`, review via `mysqldumpslow` or `pt-query-digest`) | Weekly once enabled |
| Request latency for dashboard endpoints | Frappe's own request logging / APM if configured, or manual timing via browser dev tools for `/team-dashboard`, `/management-dashboard` | Weekly |
| Background worker queue depth | `bench --site <site> doctor` or RQ queue inspection (`frappe.utils.background_jobs`) | Weekly — currently low-relevance since this app enqueues no background jobs, but worth tracking if that changes |
| Scheduler job duration | Frappe's Scheduled Job Log doctype (Desk: Scheduled Job Log list, filter by this app's job names) | Weekly |
| CPU / RAM / disk | Standard OS-level monitoring (`top`, `df`, or existing infra monitoring) | Daily/automated if available |
| Redis memory | `redis-cli info memory` | Monthly |
| Tasks created/day | `SELECT DATE(creation), COUNT(*) FROM \`tabDaily Task\` GROUP BY DATE(creation) ORDER BY 1 DESC LIMIT 30;` | Weekly — this is what actually validates or invalidates the growth projections above |

## Benchmark Plan

Design only — **do not execute** without a disposable staging/dev site.

- **Datasets:** generate synthetic Daily Task rows (via a script using `frappe.get_doc(...).insert()` in batches, or bulk SQL insert for speed, on a **non-production** site) at 10K / 50K / 100K / 500K / 1M scale, following realistic `employee`/`task_date`/`status` distributions (not all-identical values, which would misrepresent index selectivity).
- **What to benchmark per dataset size:**
  - Daily Task desk list view load (default filters, then status-only filter, then unfiltered sort)
  - `/daily-checkin` full page load (`get_page_state`)
  - `/team-dashboard` load (`get_team_dashboard`)
  - `/management-dashboard` load (`get_management_dashboard`) — scale department/employee count alongside task count here specifically, since that's its real driver
  - `submit_morning_log` / `submit_eod_log` with a realistic (5-15) task count per submission
  - `_ensure_recurring_tasks` generation path
  - `get_my_history` pagination
- **What to measure:** wall-clock response time, DB query count per request (Frappe's own query-count debug tooling or `frappe.db.sql` call counting), query execution time (via `EXPLAIN`/slow log), CPU/RAM during the run, worker queue depth if background jobs are introduced later.
- **Before vs. after:** run once against current indexing, once after applying the [Index Analysis](#index-analysis) recommendations, to get a concrete before/after number rather than a theoretical one.

## Query Profiling

Queries worth profiling first, in priority order, once a staging dataset of realistic size exists:

```sql
-- 1. Daily Task filtered by status only (the primary index gap)
EXPLAIN SELECT name, employee, task_date, status FROM `tabDaily Task`
WHERE status = 'Pending' ORDER BY task_date DESC LIMIT 20;

-- 2. Safety rollover's unbounded-lower-bound scan
EXPLAIN SELECT name FROM `tabDaily Task`
WHERE task_date < '2026-08-27' AND status IN ('Pending', 'In Progress');

-- 3. Recurring Task Template's per-check-in filter
EXPLAIN SELECT name FROM `tabRecurring Task Template`
WHERE employee = 'HR-EMP-00001' AND is_active = 1;

-- 4. Employee Department Assignment team-leader resolution
EXPLAIN SELECT parent FROM `tabEmployee Department Assignment`
WHERE team_leader = 'HR-EMP-00001';

-- 5. Management dashboard per-department pattern (representative)
EXPLAIN SELECT employee FROM `tabEmployee` WHERE department = 'Engineering' AND status = 'Active';
```

Use `EXPLAIN ANALYZE` (MariaDB 10.4+) in a staging environment for actual timing, not just the query plan, once a realistically-sized dataset exists — `EXPLAIN` alone on the current ~2K-row table won't reveal much, since everything is fast at this size regardless of indexing.

---

## Final Verdict

| Area | Current Status | Risk | Action |
|---|---|---|---|
| Task DocType design | One doc per task, standard Frappe pattern, appropriately denormalized | Low | KEEP — no change needed |
| Database | Sound InnoDB usage, no schema red flags | Low | No structural change |
| Indexes | Only 3 imperative indexes exist app-wide; several active query patterns unindexed | Medium (grows with row count) | Add the 4 recommended indexes — P1/P2 |
| API | Mostly well-batched; a handful of confirmed N+1 loops, mostly bounded by human-scale counts | Low-Medium | Fix root-chain-walk and recurring-dedup issues — P1/P2 |
| Permissions | Manual, consistent within this app's own API; not enforced for Desk/Report/direct access | Medium (architectural, not row-count-driven) | Acceptable for now; document the gap; consider `has_permission` hook if Desk/Report access to this data becomes a real usage pattern — P2/P3 |
| Redis/cache | Framework Redis in use automatically; app adds none; one anti-pattern (`clear_cache()` on every page load) | Low technically, but active waste | Remove the clear_cache misuse (P1); add team-membership caching (P2) |
| Scheduler | Idempotent, duplicate-safe; per-employee loops scale with headcount not task volume | Low now, watch headcount | Dedup holiday checks, consider queued email sends — P2 |
| Dashboard | Team dashboard well-batched; management dashboard scales with department count | Low-Medium | Batch the per-department loop — P2 |
| Storage | No red flags; Version-table growth unmeasured | Unknown — needs metrics | Measure via provided SQL — P2 |
| Long-term scalability | Architecture supports millions of rows once indexing gaps are closed | Low, contingent on P1 fixes landing | Re-run this audit's measurement steps at each capacity threshold |

### P0 — Critical
None identified. No production-breaking issue was found in this audit.

### P1 — Important (before significant growth)
- Add the 4 recommended indexes: `Daily Task(status, task_date)`, `Recurring Task Template(employee, is_active)`, `Employee Department Assignment(team_leader)`, `Daily Task(rolled_over_from, task_date)`.
- Remove/fix the unconditional `frappe.clear_cache()` call in `www/daily_checkin.py`.
- Fix `additional_work.html`'s per-item sequential submit loop to batch like `submit_morning_log`/`submit_eod_log` do.
- Fix Recurring Task Template's description-string dedup to match by a template FK instead.

### P2 — Scalability (as usage grows)
- Cache `_get_team_members` and HR manager email resolution.
- Batch `get_management_dashboard`'s per-department queries.
- Deduplicate scheduler holiday checks by holiday list; consider queued (non-`now=True`) email sends in scheduler loops.
- Add a lower date bound to `_safety_rollover`'s scan.
- Measure `tabVersion` growth relative to Daily Task.

### P3 — Future Optimization (do not implement without metrics)
- Archival/partitioning strategy — revisit only at the 500K-1M checkpoint, and only if indexing fixes prove insufficient.
- Wire up or remove the unused `Report Recipient` doctype.
- Normalize `Recurring Task Template.estimated_time` from Data to Float.
- Consider a reverse link from Daily Task Log to Daily Task if cross-reporting between them grows heavier.

---

### Answers to the 20 final questions

1. **Is the current architecture fundamentally sound?** Yes.
2. **Is one Frappe document per task a good design?** Yes — standard, appropriate for this domain.
3. **Is the current database design efficient?** Mostly — the schema is fine; indexing is incomplete for several active query patterns.
4. **Is it feasible for production use?** Yes — already in production use.
5. **Is it scalable?** Yes, to millions of rows, once the P1 index fixes land.
6. **At approximately what scale are changes likely to become necessary?** The P1 fixes are cheap enough to do now regardless of scale; without them, effects become noticeable somewhere in the 50K-100K row range for the specific unindexed filter patterns identified.
7. **What is most likely to become the first bottleneck?** Unindexed status/type-only filters on Daily Task (list view, potential reports), not the document-per-task model itself.
8. **Are the correct indexes present?** No — 4 gaps identified and specified above.
9. **Are APIs efficient?** Mostly yes; a few specific loops identified, all bounded by human-scale counts rather than table size.
10. **Are there N+1 queries?** Yes, several — cataloged in the API/N+1 sections; most are low-risk because they're bounded by per-request item counts, not total rows.
11. **Does the application explicitly use Redis cache?** No, effectively — one `get_cached_value` call; otherwise framework-level only.
12. **Should we add application-level Redis caching?** Selectively — team-membership resolution and HR manager email lookup, per the specific recommendations above. Not broadly.
13. **Does the scheduler become more expensive as data grows?** Its cost grows with active-employee count, not with Daily Task row count.
14. **Will historical records eventually need archival?** Possibly, at very large scale (500K-1M+) — not now, and not simply because records are old.
15. **Does Daily Task need redesigning?** No — optimize, don't redesign.
16. **What should we fix now?** The P1 list above.
17. **What can safely wait?** The P2 and P3 lists above.
18. **What should we monitor?** Row counts, table/index sizes, slow query log, dashboard latency, scheduler job duration — see [Monitoring Plan](#monitoring-plan).
19. **How can we benchmark the system properly?** Synthetic datasets at 10K-1M scale on a disposable staging site, per the [Benchmark Plan](#benchmark-plan) — not executed as part of this audit.
20. **Is there any reason to stop using the current architecture today?** No.
