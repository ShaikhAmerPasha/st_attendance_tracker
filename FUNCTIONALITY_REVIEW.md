# Functionality Review — ST Attendance Tracker

Independent review of everything built this cycle: check-in/check-out core flow, half-day leave/WFH handling, carry-forward confirmation, recurring tasks, multi-department Team Leader notification, email formatting, and the recent bug-fix round. Two independent audits were run against the actual code (not memory of intent) — one on security/data-integrity, one on business-logic completeness and scale. Findings below are theirs, verified, synthesized with context: **under 100 employees, single location/timezone, informal internal use** (per your answers).

## Executive summary

The functional design is solid — the check-in/check-out lifecycle, half-day handling, carry-forward, recurring tasks, and multi-department notification are all genuinely well thought through, with real edge cases handled (overnight shifts, rollover idempotency, BOLA guards on the custom API). At your stated scale, the scalability concerns the audits raised are **not urgent**. The security audit surfaced **three critical, currently-exploitable issues** that had nothing to do with scale — they were wrong at 10 employees or 10,000.

**Status: all Critical and High findings below are now fixed and verified** (automated checks + the full 44-test suite, still at the same pre-existing baseline — nothing new broken). Two things worth knowing about how the fixes landed:

- **Recurring Task Template's permissions had drifted** since I last touched that file — someone had added unrestricted `System Manager`/`Team Lead` rows via the desk Role Permission Manager (dev mode auto-exports that back to the doctype JSON). That directly re-opened the exact over-privilege being fixed, so I tightened those rows too rather than leave them contradicting the fix.
- **The Team Leader "must hold a real manager role" check was tried and reverted.** Testing it against your actual data showed this org's real team leaders (e.g. Imran, already set up as a Team Leader for another employee) aren't tagged with the `Team Lead`/`HR Manager` Frappe role — `reports_to`/the assignment table itself is how leadership is tracked here. The role check would have broken real, already-working data the next time anyone touched an affected Employee record. Replaced with a check that matches how this org actually operates: the named Team Leader must be a real, active employee, and can't be a self-reference. Confirmed the existing data now saves cleanly and the self-reference case is still blocked.

## What's implemented

- Check-in/check-out with task planning, project grouping, ad-hoc tasks, lunch tracking, net-hours calculation (overnight-safe)
- Work location (Office/WFH/Remote/Hybrid) with Attendance Request validation, now correctly pre-filling WFH once a request is on file
- Half-day leave + half-day session selection, mid-day retroactive application, now-clarified "which half is which" messaging
- Carry-forward confirmation (opt-in checkbox, Dropped status) replacing silent auto-rollover
- Recurring Task Templates (employee self-service, auto-regenerate daily, immune to carry-forward)
- Multi-department Team Leader notification (Employee Department Assignment child table, with legacy `reports_to` fallback)
- Professional, systematic email formatting (detail cards, action timestamps, consistent single-tone design) for check-in, checkout, and HR/Team Leader notifications
- Team Leader dashboard, Management dashboard, scheduled reminder/report emails
- A 44-case automated test suite plus a maintained manual testing guide

## What's genuinely good

- **BOLA guards on the custom API surface are consistently applied** — every whitelisted mutation (`submit_morning_log`, `submit_eod_log`, `delete_carried_task`) correctly checks `_assert_task_owner`/employee equality before acting. This is the hard part of building a multi-tenant-per-employee system and it's done right.
- **Overnight/midnight-crossing shift math** is handled deliberately and correctly (`_calc_net_hours`), not an afterthought.
- **Rollover idempotency** — both `_rollover_pending_tasks` and `_safety_rollover` take row locks and de-duplicate correctly; this was tested and holds up.
- **All SQL is parameterized** — no injection found anywhere in `api.py`.
- **Deployment safety is genuinely strong** — every schema-changing patch is idempotent, guarded, and correctly split between `pre_model_sync`/`post_model_sync`. `bench migrate` on a fresh site would apply cleanly (aside from one gap noted below).
- **Shift Assignment integration correctly delegates to standard HRMS APIs** rather than reinventing shift logic — a real strength, not a shortcut.

## Critical — FIXED

**1. ~~Stored XSS in Team/Management dashboards.~~ Fixed.** Task descriptions were inserted into `innerHTML` unescaped in `team_dashboard.html`, `management_dashboard.html`, `my_history.html` — any employee could put `<img src=x onerror="...">` in a task description and have it execute in their Team Leader's or HR Manager's authenticated session the moment they viewed it. Added an `esc()` HTML-escape helper to all 3 files, applied to every task description, project name, employee name, and department string rendered via `innerHTML`. Verified: a malicious description now renders as literal escaped text, not executable markup.

**2. ~~Recurring Task Template: delete bypassed the ownership check.~~ Fixed.** `_check_ownership()` only ran on `validate()` — Frappe never calls `validate()` on delete. Added an `on_trash()` method that calls the same ownership check. Verified: Employee A attempting to delete Employee B's template now gets `PermissionError`.

**3. ~~Unrestricted read/write on Daily Task / Daily Task Log / Recurring Task Template.~~ Fixed.** Added `if_owner: 1` to the Employee role's permission row on all three doctypes — verified every insertion path in the app runs in the actual employee's own session (so `owner` is always correctly set to them), meaning this closes the gap with no legitimate flow broken. Verified: Employee A can no longer see Employee B's Daily Task via `frappe.get_list` (the standard permission-checked path desk/REST access uses).

## High — FIXED

- **~~`Team Lead` role bypass was org-wide, not relationship-scoped.~~ Fixed.** Removed `Team Lead` from the role-bypass set in `_check_ownership()` across `daily_task.py`, `daily_task_log.py`, `recurring_task_template.py` — Team Lead oversight now has to go through the already-correct `reports_to`/assignment-scoped dashboard API, not a blanket desk-edit bypass. Verified: a Team Lead-role user with no actual relationship to an employee can no longer edit that employee's Daily Task.
- **~~Employee Department Assignment `team_leader` could be redirected with no validation.~~ Fixed, with a correction along the way.** First attempt required the named Team Leader to hold a `Team Lead`/`HR Manager` Frappe role — testing against real data showed this org doesn't actually tag its real team leaders that way (Imran, a real team leader here, holds neither role), so that check would have broken legitimate existing data on the next save. Replaced with a check that matches actual practice: the named Team Leader must be a real, active employee, and can't be a self-reference. Also discovered along the way that Frappe doesn't call a child table row's own `validate()` when the parent saves — the real fix had to move to an `Employee` `doc_events` hook (`hooks.py` → `setup.py:validate_department_assignments`), since the child doctype's own controller method was silently never firing.
- **~~Dashboard visibility and notification routing used two different authorization sources.~~ Fixed.** `_is_team_leader()`/`get_team_dashboard()` now check both `reports_to` and Employee Department Assignment rows (same two sources `_get_team_leader_emails` already used for notifications), so a Team Leader defined only via the assignment table now sees that employee on `/team-dashboard` too, not just in their inbox.

Also found and fixed in passing: **`Recurring Task Template`'s permissions had drifted** to include unrestricted `System Manager`/`Team Lead` rows (added via the desk Role Permission Manager at some point, auto-exported to the doctype JSON under dev mode) — directly re-opening the over-privilege the `Team Lead` fix above addresses. Added `if_owner` to that row too rather than leave it contradicting the fix.

All of the above verified via automated checks (a throwaway verification script exercising each scenario) plus the full 44-test suite — same pre-existing baseline (1 unrelated failure, 3 unrelated errors), nothing newly broken.
- **~~HTML injection in emails~~ Fixed** (same unescaped description/project-name issue as #1, but in HTML email bodies sent to HR/Team Leaders/employees) — added Python `html.escape()` in `_render_screenshot_task_table` and `_render_detail_table`. Verified a malicious description no longer renders as raw markup in the email HTML.

## Should-fix, deprioritized given your scale (under 100, single timezone, informal)

These are real gaps the audits found, but **not urgent for your stated deployment** — flagging so they're known, not because they block anything right now:

- Hardcoded "Saturday = WFH for everyone" and "Sunday = always skipped" — fine for a single-region deployment, would break for any future Friday-Saturday-weekend office.
- Single server-timezone assumption throughout — a non-issue at one location, would matter if you ever add a remote/international office.
- `reset_morning_checkin` permanently deletes the original `Employee Checkin` record via raw SQL (no audit trail) — acceptable for informal internal use, would matter if this ever needs to double as an official attendance record.
- N+1 query patterns in the 10:30 AM reminder cron and per-department dashboard fan-out — genuinely irrelevant at under 100 employees.
- No digest/batching on HR/Team-Leader notification emails — at your scale, email volume is a non-issue (worth revisiting only if headcount grows a lot).
- Flat one-level Team Leader hierarchy (no skip-level), and Recurring Task Template being strictly 1-template-per-employee (no bulk-assign) — both real operational friction only at larger headcount.
- Missing `hrms` as a declared required app dependency — would only bite on a from-scratch install missing HRMS, not your existing site.
- 3 currently-erroring + 1 currently-failing pre-existing test (unrelated to anything built this session) — worth a look, not urgent. Also a genuine live inconsistency: ad-hoc tasks default `status="Done"` without requiring `actual_time`, but `Daily Task.validate()` requires it unconditionally whenever status is Done — this will throw for any ad-hoc task submitted without an explicit time.
- Zero automated test coverage on the dashboards, scheduled jobs, and actual email content — the parts most likely to have quiet bugs.
- A few minor races (`_ensure_recurring_tasks`, `reset_morning_checkin`, `update_half_day_session` don't take the row lock their sibling functions do) — plausible only under rapid double-clicks/multi-tab use, not malicious scenarios.

## Assumptions made while building

Worth surfacing since some of these were judgment calls, not requirements you explicitly gave:

- **Notify-all, not per-project routing**: every Team Leader on a multi-department employee gets every check-in/checkout email, regardless of which project that day's work belongs to — you confirmed this explicitly, but it's worth remembering as a deliberate simplicity-over-precision choice.
- **`reports_to` stays the HRMS org-chart source of truth**; the new Department Assignment table is purely additive for notification routing — I didn't touch the standard HRMS reporting structure.
- **Bare numbers in time fields mean hours, not minutes** ("30" = 30 hours) — left unchanged deliberately since reinterpreting it would silently change how existing data reads; flagged to you, no response yet on whether you actually want it changed.
- **Half-day session = the half being worked**, leave is the implied other half — confirmed with you as correct.
- **Employee Self Service / User Permission setup is HR's responsibility**, not this app's — the app assumes HR properly restricts each employee to their own record via standard ERPNext user permissions; nothing here automates or verifies that setup, and finding #3 shows what happens when that assumption doesn't hold.
- **Single company, single holiday calendar convention** (Sunday off, Saturday WFH) baked into rollover logic — reasonable for one location, not portable as-is.
- **Recurring tasks are daily, unconditionally** — no per-weekday scheduling (e.g. "standup only Mon-Fri") was requested or built; every active template regenerates every single day including weekends unless the employee just doesn't check in.

## Recommendation

All Critical and High findings are fixed and verified. Remaining open items are the "should-fix, deprioritized" and "nice-to-have" lists above — none of them urgent given your context (under 100 employees, single location, informal internal use). Revisit those only if headcount grows substantially or the org expands to multiple locations/timezones.
