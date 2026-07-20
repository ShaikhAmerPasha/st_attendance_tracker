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
- [ ] Confirmation email arrives at the employee's address with the task list.
- [ ] HR Manager(s) + the employee's Team Leader receive a "checked in" notification email (see §9).

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
- [ ] Clean up test Leave Application afterward if this was purely a test record.

## 3. Task management during the day

- [ ] Add a standalone task and a task inside a "project" group — both save and appear correctly grouped.
- [ ] Edit a carried-over task's description before checking in — saves correctly.
- [ ] Delete a carried task / delete an entire project group before EOD — removed correctly, and blocked with a clear error if attempted **after** EOD is already submitted for that date.
- [ ] Refresh the page mid-way through filling the **morning** form (before submitting) — your typed tasks should be restored from the draft-save (localStorage). Note: this draft-save does **not** exist for the EOD form — refreshing mid-EOD will lose unsaved edits; this is a known gap, not a bug to chase in this pass.

## 4. End of Day (EOD)

- [ ] From the checked-in state, mark each task's status (Pending/In Progress/Done) and enter "time taken" for any marked Done — trying to mark Done with no time entered should be blocked with a clear message.
- [ ] Enter lunch from/to, click **Check out & submit EOD**.
- [ ] Net Hours / Total Task Hours shown match manual expectation (login → logout minus lunch).
- [ ] Try to submit EOD **twice** the same day → second attempt should be blocked ("already submitted").
- [ ] Any task left Pending/In Progress at EOD should roll over to tomorrow's list automatically — verify by loading tomorrow's date (or check the "carried" tag / days-pending indicator once it appears).
- [ ] HR/Team Leader notification email fires with login/logout/net hours summary; employee gets their own EOD confirmation email.

## 5. Reset check-in

- [ ] From the checked-in (pre-EOD) state, click **Reset check-in**. Confirm the page reverts to the morning form, and a fresh check-in can be submitted afterward same day.

## 6. WFH / Attendance Request validation

- [ ] As an Office (or Hybrid-on-office-day) employee, pick **WFH** with no Attendance Request on file for today → should show a clear warning/redirect prompting them to submit one, and should **not** let check-in proceed silently.
- [ ] Submit + approve an Attendance Request (reason "Work From Home") covering today, reload, pick WFH again → now allowed through.

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

## 10. Email notifications — who gets what

- [ ] Confirm current recipients: check in Desk → Role → HR Manager → who currently holds this role, and separately note the checking-in employee's Team Leader. Every check-in/EOD notification should reach **all** of them, not just one.
- [ ] Scheduled reports (run these manually via Desk → Background Jobs, or wait for the cron): 10:30 AM missing-checkin reminder (to employees), 11:30 AM combined missing+late report (to HR), 10:00 PM EOD-missing report (to HR). Confirm employees on **any** approved leave (full or half-day) are correctly excluded from all three — and be aware half-day-leave employees are currently excluded entirely (not "expected for half the day"), which is today's intended-if-imperfect behavior.

## 11. Known non-bugs (don't file these as new issues)

- EOD form has no draft-save (morning form does) — known gap.
- Half-day/leave and WFH-via-Attendance-Request don't cross-validate — an employee can have both simultaneously with no conflict warning. Documented behavior, not a defect from this pass.
- Management Dashboard has no mobile layout (desktop-only) — known, unaddressed.
- `logout_time` field on the EOD form is read-only and server-synced — this is intentional, not an editable field you forgot to fill in.

---

## Sign-off checklist

Before moving to production, confirm every unchecked box above is checked for at least one employee of each `work_type` (Office/Remote/Hybrid), plus the half-day flow tested end-to-end at least once, plus the BOLA check in §7 confirmed blocked.
