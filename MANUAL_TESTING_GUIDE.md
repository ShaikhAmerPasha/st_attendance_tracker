# Manual Testing Guide — ST Attendance Tracker

Complete walkthrough for QA-ing the app before production. Site: `excel.localhost:8000` (Administrator/admin per CLAUDE.md).

## Setup

**Testing as different employees without their passwords:** log in as Administrator → Desk → Users list → open the target user → menu (top-right "...") → **Impersonate**. This logs you in as that user instantly. Use this to test Office/Remote/Hybrid employees without needing each password.

**Test employees you need (create if missing, via Desk → HR → Employee):**
| Role | work_type | Purpose |
|---|---|---|
| Employee A | Office | Standard flow, half-day tests |
| Employee B | Remote | Remote-only flow (no location choice) |
| Employee C | Hybrid | Office-day vs WFH-routine-day logic |
| A Team Leader | any | someone with `reports_to` pointing at Employee A/B/C |
| An HR Manager user | — | Management Dashboard access |

Each test employee needs: a `user_id` linked to a real login, `status = Active`, and a submitted **Shift Assignment** (HR → Shift Assignment, docstatus submitted, `status = Active`, `start_date` ≤ today, no `end_date` or a future one).

---

## 1. Morning Check-In — core flow

URL: `/daily-checkin`

- [ ] **Office employee, first login of the day:** page loads, "Active Shift" card shows the assigned shift's real start/end time (not blank, not "No shift assigned" — unless genuinely no Shift Assignment exists, in which case that text is correct).
- [ ] Work-location dropdown shows **Office / WFH** and is editable.
- [ ] Add 2-3 planned tasks (description + estimated time), click **Check in**.
- [ ] After reload: "Attendance Status" card shows "Checked In" with the login time; login time in the confirmation toast matches (should read like `09:00 AM`, never a garbled value like `9:0:` or similar).
- [ ] Try to check in **again** the same day → should be blocked ("already checked in" type message), not silently create a second log.
- [ ] Confirmation email arrives at the employee's address with the task list — see §10 for what the email itself should look like now (detail card, "Checked-In At" timestamp).
- [ ] HR Manager(s) + the employee's Team Leader(s) receive a "checked in" notification email (see §10, §13 for multi-department employees).

- [ ] **Remote employee:** work-location card shows **Remote only**, dropdown is disabled/read-only, no WFH validation triggered.
- [ ] **Hybrid employee on a configured office day** (check `ST Attendance Settings → Hybrid Office Days`, default Tue/Thu): dropdown offers Office/WFH but picking WFH requires an approved **Attendance Request** (see §6).
- [ ] **Hybrid employee on a non-office day:** dropdown defaults to WFH with a "no Attendance Request needed" note, Office still selectable.

- [ ] **Saturday (any employee):** work location forced to WFH, read-only, no validation.

## 2. Half-Day Session (new feature)

Setup: give an Office-type test employee an **Approved** Leave Application for today with `Half Day` checked (HR → Leave Application → tick "Half Day", set the date, submit, then approve).

- [ ] Reload `/daily-checkin` as that employee (or Impersonate them) — the pre-checkin form now shows an extra **"Half-day session"** dropdown (First Half / Second Half) next to the work-location dropdown. It must **not** appear for employees without half-day leave today.
- [ ] Note the Active Shift card's full window (e.g. "9:00 AM - 5:00 PM").
- [ ] Change the Half-day session dropdown from First Half → Second Half **without submitting yet** — the Active Shift card's time range should update live to the *second* half of the shift (e.g. "1:00 PM - 5:00 PM"). Switch back to First Half — should revert to the *first* half (e.g. "9:00 AM - 1:00 PM"). This must happen instantly, no page reload.
- [ ] Add a task, check in with **Second Half** selected.
- [ ] After reload: Active Shift card now permanently shows the truncated Second Half window (not the full shift) — this value is now read from the saved record, not the live preview.
- [ ] The mid-day "recommended logout time" hint should reflect the truncated shift's real duration (e.g. login + ~4h for a standard 8h shift split in half), not always a flat 4 hours.
- [ ] Submit EOD (see §4) — the HR/Team-Leader notification email should mention "Half-Day: Second Half" (or whichever was picked).
- [ ] **Regression check:** an employee with *no* Shift Assignment at all, who also has half-day leave — the selector should still appear (for record-keeping) but the Active Shift card should keep showing "No shift assigned" (not error/crash), and the recommended logout time should fall back to the old flat half/full-day estimate.
- [ ] **Fixed bug — which half is WFH/Office now shown clearly:** with half-day leave active, pick Work Location = WFH and Half-day session = Second Half (in either order) — a note should appear below both dropdowns reading something like *"Second Half: checking in as WFH. First Half is your approved leave."*, updating live as you change either dropdown (no page reload). After checking in, the same sentence should appear near the recommended-logout banner, now reading from the saved values. If you check in *before* half-day leave gets approved (see §14), the mid-day "Apply Half-Day" banner should say your pending half will be "recorded as `<your check-in location>`" — not a bare prompt with no location context.
- [ ] Clean up test Leave Application afterward if this was purely a test record.

## 3. Task management during the day

- [ ] Add a standalone task and a task inside a "project" group — both save and appear correctly grouped.
- [ ] Edit a carried-over task's description before checking in — saves correctly.
- [ ] Delete a carried task / delete an entire project group before EOD — removed correctly, and blocked with a clear error if attempted **after** EOD is already submitted for that date.
- [ ] Refresh the page mid-way through filling the **morning** form (before submitting) — your typed tasks should be restored from the draft-save (localStorage). Note: this draft-save does **not** exist for the EOD form — refreshing mid-EOD will lose unsaved edits; this is a known gap, not a bug to chase in this pass.

## 4. End of Day / Checkout

User-facing wording changed: the button and email/badge text now say "Checkout" instead of "EOD" — this guide still uses "EOD" informally as shorthand in prose (matches the internal `log_type = "End of Day"` value and `submit_eod_log` function name, which are unchanged), but nothing in the actual UI reads "EOD" anymore.

- [ ] From the checked-in state, mark each task's status (Pending/In Progress/Done) and enter "time taken" for any marked Done — trying to mark Done with no time entered should be blocked with a clear message.
- [ ] **New behavior**: leave a task's status dropdown untouched (still "Pending") but type something into "Time taken for task completion" (e.g. `30m`) → after submitting checkout, that task's status should be **In Progress**, not left as Pending — entering time is treated as an implicit "I started this." A task left with no time entered at all stays Pending as before.
- [ ] Enter lunch from/to, click **Check out & submit** (was "Check out & submit EOD").
- [ ] Net Hours / Total Task Hours shown match manual expectation (login → logout minus lunch).
- [ ] Try to submit checkout **twice** the same day → second attempt should be blocked ("already checked out").
- [ ] Any task left Pending/In Progress at checkout, with its **"Carry forward to tomorrow"** checkbox left checked (default), rolls over to tomorrow's list automatically — verify by loading tomorrow's date (or check the "carried" tag / days-pending indicator once it appears). See §11 for the opt-out (uncheck → Dropped) path.
- [ ] Any active Recurring Task Template for this employee should appear in its own **"Recurring"** section at the top of the task list (above project groups/standalone tasks, not interspersed among them) — tagged **"Recurs daily"**, not "Carried" — with no manual re-typing needed. See §12.
- [ ] HR/Team Leader notification email fires with login/logout/net hours summary; employee gets their own checkout confirmation email. See §10 for the email's expected format.

## 5. Reset check-in

- [ ] From the checked-in (pre-EOD) state, click **Reset check-in**. Confirm the page reverts to the morning form, and a fresh check-in can be submitted afterward same day.
- [ ] **Regression check (fixed bug):** after resetting, do **not** add any new tasks — your original tasks should still be visible in the "Carried from previous days" panel (reverted to Pending). Correct the login time and click **Check in & start day** again → must succeed, must **not** show "Please add at least one planned task" (this was a client-side counting bug; the tasks were always safely preserved in the database, the page just miscounted them client-side before submitting).

## 6. WFH / Attendance Request validation

- [ ] As an Office (or Hybrid-on-office-day) employee, pick **WFH** with no Attendance Request on file for today → should show a clear warning/redirect prompting them to submit one, and should **not** let check-in proceed silently.
- [ ] Submit + approve an Attendance Request (reason "Work From Home") covering today, reload `/daily-checkin` → **fixed bug:** the work-location dropdown should now default/pre-select to **WFH** automatically (with a note "Your WFH request is on file"), not "Office". Confirm check-in with WFH now goes through without hitting the "WFH Not Applied" popup again.

## 7. Cross-employee protection (BOLA — fixed this session, worth confirming)

- [ ] Log in as Employee A, note one of their task IDs (visible in page source / dev tools network tab as `name` on a Daily Task row).
- [ ] Log in as Employee B (a *different* employee), and attempt to call `submit_morning_log` or `submit_eod_log`'s carried-task-update path referencing Employee A's task id (via browser dev console `frappe.call(...)` is the easiest way to test this directly). Expect a **"Not authorised to edit this task"** permission error, not a silent success.
- [ ] Confirm Employee B genuinely cannot see or modify Employee A's Daily Task Log via the desk UI either (as a plain Employee-role user, not System Manager).

## 8. Team Leader Dashboard

URL: `/team-dashboard` — visible only to someone who is a `reports_to` target for at least one active employee.

- [ ] Log in as a Team Leader → dashboard loads showing **only their own direct reports**, not the whole company.
- [ ] Checked-in / late / on-leave / EOD-done statuses per report match what you set up in §1-4.
- [ ] Click into an individual report's task detail — drill-down loads correctly.
- [ ] Log in as a non-team-leader employee → `/team-dashboard` should not show any reports (or redirect), confirming scoping.

## 9. Management Dashboard

URL: `/management-dashboard` — HR Manager role only.

- [ ] Log in as a plain Employee → attempting to visit this URL should redirect away (not show data).
- [ ] Log in as HR Manager → department rollups, company-wide checked-in/late/on-leave counts, and the leaderboard (with rank badges, overtime flag, half-day flag) all populate.
- [ ] Verify counts add up sanity-check style: checked-in + missing + on-leave ≈ total active employees for the day.
- [ ] Note: no CSV export exists — if you need one for a report, it must be pulled manually via Desk report view for now.

## 10. Email notifications — format and who gets what

- [ ] Confirm current recipients: check in Desk → Role → HR Manager → who currently holds this role, and separately note the checking-in employee's Team Leader(s) (§13 if they work in multiple departments). Every check-in/EOD notification should reach **all** of them, not just one.
- [ ] Open a check-in email (Desk → Email Queue, newest row, or the real inbox) and confirm it shows a clean **detail card** — label/value rows, single navy accent color throughout (no rainbow per-field colors) — with: Employee, Date, Login Time, **Checked-In At** (full date+time, the real server moment the check-in button was pressed — distinct from Login Time, which the employee can edit), Work Location, Half-Day Session (if applicable), Status (Late Check-in, if applicable). Task table below it keeps its existing Client/Task/Time/Status columns, grouped by project, with a single accent color (no per-project rainbow) and alternating light-gray row shading.
- [ ] Open the matching EOD email — same detail-card style, with Employee, Date, Login Time, Logout Time, **Checked-Out At**, Lunch Break, Work Location, Net Working Hours, Total Task Hours, Half-Day Session, Tasks Completed.
- [ ] Confirm the HR/Team Leader notification email shows the **same structured detail card** (not a free-text paragraph) — same fields as above.
- [ ] Scheduled reports (run these manually via Desk → Background Jobs, or wait for the cron): 10:30 AM missing-checkin reminder (to employees), 11:30 AM combined missing+late report (to HR), 10:00 PM EOD-missing report (to HR). Confirm employees on **any** approved leave (full or half-day) are correctly excluded from all three — and be aware half-day-leave employees are currently excluded entirely (not "expected for half the day"), which is today's intended-if-imperfect behavior.

## 11. Carry-Forward Confirmation (new)

Employees now choose per-task whether an unfinished task carries to tomorrow, instead of it happening silently.

- [ ] Check in with 2+ planned tasks. At EOD, leave one Pending/In Progress — confirm it shows a checked **"Carry forward to tomorrow"** checkbox. Marking a task Done hides its checkbox (irrelevant once done).
- [ ] **Uncheck** the box on one Pending task, submit EOD.
- [ ] Desk → Daily Task list → filter by this employee/today's date → the unchecked task's status is now **Dropped** (not Pending, not Rolled Over).
- [ ] Load the check-in page for the next working day (or Impersonate + advance via a fresh check-in tomorrow) → confirm the Dropped task does **not** reappear.
- [ ] Leave a different task Pending with the box left **checked** (default) → confirm it still rolls over to tomorrow exactly as before (regression check — default behavior unchanged).
- [ ] This confirmation step only applies at EOD submit. The separate "safety rollover" that catches a fully-missed day (employee never submitted EOD at all) stays silent/automatic by design — not a bug if you see a task auto-carry without ever seeing a checkbox for it in that scenario.

## 12. Recurring Tasks (new)

Employee self-service templates (e.g. daily scrum) that auto-populate every day without manual re-entry or carry-forward.

- [ ] Desk → search "Recurring Task Template" → New → set Employee (yourself, or the employee you're testing as), Description = "Daily Scrum Standup", Estimated Time = `15m` (free text, same format as Daily Task's time fields — not a raw decimal), Active checked → Save. (Employees can create/manage their own via the standard list/form — HR Manager can see/manage all.)
- [ ] Load `/daily-checkin` for that employee **before** checking in → the task appears in its own **"Recurring"** section at the very top of the task list (above any carried/project tasks), tagged **"Recurs daily"** (not "Carried", not needing to be typed in), with the Estimated column correctly showing `15m`.
- [ ] Check in — the recurring task counts toward "at least one task" and shows in its own "Recurring" section at the top of the working-day task list too (still above project groups/standalone tasks).
- [ ] At checkout, leave it Pending/In Progress (don't mark Done) → confirm it shows the **"Recurs daily"** badge instead of a carry-forward checkbox, and submit.
- [ ] Load the next working day's check-in page → confirm a **fresh** "Daily Scrum Standup" task appears again automatically (not linked to yesterday's via any carried-from chain — it's a brand new instance from the template every day, regardless of whether yesterday's was completed).
- [ ] Set the template's Active checkbox off **while today's copy is still Pending** → confirm today's copy disappears from `/daily-checkin` immediately too (not just future days). If you'd already marked it Done, deactivating should leave that record alone.
- [ ] As a different Employee-role user, confirm you cannot see/edit another employee's Recurring Task Template (ownership guard, same BOLA pattern as §7).

## 13. Multi-Department Team Leaders (new)

For employees who work across more than one department (e.g. Web + Digital Marketing) and need every relevant Team Leader notified.

- [ ] Desk → open the test Employee → find the **"Department Assignments"** grid (right after the core Department field) → add 2 rows: (Department = Web, Team Leader = Person A) and (Department = Digital Marketing, Team Leader = Person B) → Save.
- [ ] Have that employee check in → confirm **both** Person A and Person B receive the notification email (in addition to HR Manager role users, unchanged).
- [ ] Same at check-out/EOD — both Team Leaders get that email too, every time, regardless of which project the day's tasks were logged under (this is intentional — no per-task department routing).
- [ ] **Regression check:** an employee with **no** Department Assignment rows still notifies their existing single `reports_to` manager exactly as before (legacy fallback) — the core `reports_to` field and org-chart are untouched by this feature.
- [ ] **Fixed bug:** log in (or Impersonate) as the *employee themselves* whose Department Assignment row's Team Leader is a person they wouldn't normally have permission to view — open their own Employee record (Desk → Employee → their name, or via Impersonate then visiting their own profile). Must load cleanly, **not** throw "Insufficient Permission" / "not allowed to access this Employee Department Assignment record". This was blocking self-view entirely before the fix.

## 14. Half-Day Applied Mid-Day (fixed bug)

Scenario: employee checks in normally (no half-day flagged yet), then gets a half-day Leave Application approved for today *after* they've already checked in.

- [ ] Check in as an Office employee with a Shift Assignment, no half-day leave yet.
- [ ] An hour or so later (or just immediately for testing), submit + approve a half-day Leave Application for today (HR → Leave Application → tick "Half Day" → submit → approve).
- [ ] Reload `/daily-checkin` (still checked in, not reset) → a new amber banner should appear: "Your half-day leave is approved but not yet applied to today's check-in", with a First Half/Second Half selector and an **Apply Half-Day** button.
- [ ] Pick a half, click **Apply Half-Day** → page reloads.
- [ ] Confirm: the banner is now gone, the Active Shift card shows the truncated half-shift window, and the recommended logout time hint reflects the shorter (half-day) duration — not the full shift anymore.
- [ ] **Regression check:** an employee who selected their half-day session at check-in time (the original pre-checkin flow) should never see this banner at all (it only appears when the field is still unset).
- [ ] Try applying twice, or after EOD is already submitted for the day → second attempt / post-EOD attempt should be blocked with a clear error, not silently succeed.

## 15. Task Time Entry Format (fixed bug)

- [ ] Add a task with Estimated time `45m`, mark it Done at EOD with Time Taken `1h 30m`.
- [ ] Reload the page → both fields must redisplay as `45m` and `1h 30m` — **not** as a raw decimal like `0.75` or `1.5`.
- [ ] Placeholder text on all Est./Actual time boxes should now read "e.g. 1h 30m, 45m" (was inconsistent/unclear before).
- [ ] **Known, intentional, unchanged:** typing a bare number with no unit (e.g. `30`) is still interpreted as **30 hours**, not 30 minutes — this was NOT changed, since reinterpreting bare numbers as minutes would silently change how all existing historical data is read. Always include a unit (`m` or `h`) when entering minutes. Flag to the team if you actually want bare numbers to default to minutes instead — that's a deliberate follow-up decision, not an oversight.

## 16. Open issues — resolved

- ~~Task data appears lost after a WFH-not-applied popup → Attendance Request → "Go to Check-In Page" round trip.~~ **Closed, confirmed non-issue** — no data loss occurs; the draft-save is never cleared on navigation. Confirmed by direct testing: the original tab still has the typed data (the popup's Attendance Request link opens in a new tab, which is what made it look like data vanished).
- ~~Recurring Task Template list — no visible Add/New button.~~ **Fixed — real root cause found.** The doctype JSON had `"in_create": 1` set. Per Frappe's own permission-building logic (`frappe/utils/user.py`), that flag diverts a doctype into a separate `in_create` list (the navbar's global "+ Create New" dropdown) *instead of* the `can_create` list that the List View's own "+ Add" button is driven by — confirmed directly by inspecting `UserPermissions.build_permissions()` output, which showed the doctype in `in_create` but absent from `can_create` for every user, including Administrator (explaining why it wasn't role-specific). Removed the flag, migrated. **Note:** since this array is computed once per login session, anyone who was already logged in needs a fresh login or hard refresh to see the button — it won't appear on an old, already-open session.

## 17. Known non-bugs (don't file these as new issues)

- EOD form has no draft-save (morning form does) — known gap.
- Half-day/leave and WFH-via-Attendance-Request don't cross-validate — an employee can have both simultaneously with no conflict warning. Documented behavior, not a defect from this pass.
- Management Dashboard has no mobile layout (desktop-only) — known, unaddressed.
- `logout_time` field on the EOD form is read-only and server-synced — this is intentional, not an editable field you forgot to fill in.
- The page-load "safety rollover" (§4, §11) for a fully-missed EOD day stays silent/automatic on purpose — no carry-forward checkbox exists for that path, only for an active EOD submission.
- Both Team Leaders on a multi-department employee (§13) get every check-in/EOD email unconditionally — there's no per-task department routing, by design.
- Typing a bare number (e.g. `30`) in an Est./Actual time box is still interpreted as **hours**, not minutes (§15) — intentional, unchanged, since reinterpreting it would silently alter how existing data is read. Always include a unit.

---

## Sign-off checklist

Before moving to production, confirm every unchecked box above is checked for at least one employee of each `work_type` (Office/Remote/Hybrid), plus the half-day flow tested end-to-end at least once, the BOLA check in §7 confirmed blocked, the carry-forward drop path in §11, the recurring-task auto-creation in §12, the multi-department dual-notification and self-view fix in §13, the mid-day half-day application in §14, and the time-format redisplay in §15. §16's two open issues are tracked separately and don't block sign-off on everything else.
