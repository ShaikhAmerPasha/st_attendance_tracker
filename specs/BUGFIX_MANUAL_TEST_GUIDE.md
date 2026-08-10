# Bugfix Manual Test Guide — 2026-08-10

Fixes applied for the bugs found in the full codebase audit. Each section below is
one bug: what was wrong, what changed, and exact manual steps to confirm it on
`excel.localhost:8000` (Administrator/admin, or a real employee login).

Automated suite: `bench --site excel run-tests --app st_attendance_tracker` — ran
before and after these changes, same 4 pre-existing failures both times (see
"Pre-existing test failures" at the bottom). None of the fixes below touch those
code paths.

---

## 1 & 2. EDA-only Team Leaders locked out of Team Dashboard / task detail

**Was:** `/team-dashboard` access and the "Team" nav link on `/daily-checkin` both
checked only `Employee.reports_to`. A Team Leader defined solely via the
`Employee Department Assignment` table (no one reporting to them directly) was
redirected away and never saw the link, even though `get_team_dashboard` already
supported them. Same gap in `get_employee_task_detail` (drill-down into one
employee's tasks).

**Changed:** all three now use `_is_team_leader` / `_get_team_members`
(api.py) — checks `reports_to` OR EDA rows. Pure widening: anyone who passed the
old check still passes.

**Test:**
1. Pick (or create) an Employee, call them `EMP-A`, with **no one** reporting to
   them (`reports_to` field empty on all other employees).
2. On `EMP-A`'s Employee record, add a row to "Department Assignments" naming
   `EMP-A` as `team_leader` for some department, with another active employee
   (`EMP-B`) also assigned to that department.
3. Log in as `EMP-A`'s user. Open `/daily-checkin` — confirm the "Team" link now
   appears in the top nav, sidebar, and mobile nav (it did not before).
4. Click through to `/team-dashboard` — confirm the page loads (previously
   redirected straight back to `/daily-checkin`) and shows `EMP-B`.
5. From that dashboard, open `EMP-B`'s task detail for today — confirm it loads
   instead of "Access denied".
6. Regression check: log in as an existing, already-working Team Leader (one who
   leads via plain `reports_to`) — confirm their dashboard/nav link/task detail
   still work exactly as before.

## 3. Checkout email/response showed a different net_hours than the dashboard

**Was:** `submit_eod_log` computed `net_hours` once (api.py `_calc_net_hours`),
then `DailyTaskLog.validate()` silently recomputed and overwrote it with its own
formula when the log was inserted/submitted. The email sent to the employee and
the JSON response returned to the browser still used the first (discarded)
value — so in edge cases (lunch partly outside the shift, etc.) the number in
your inbox could differ from what Team/Management dashboard and My History show
for the same day.

**Changed:** one line after `log.submit()` re-reads the actual saved
`log.net_hours` and uses that for the response + both notification emails. No
calculation logic changed — the DB-stored value is untouched, just now
correctly echoed.

**Test:**
1. Check in as an employee (any login time).
2. Check out with a lunch break that's mostly normal — first confirm the happy
   path: net hours shown in the toast/response after checkout submit matches
   what `/my-history` shows for today once you reload.
3. Check your email (or Frappe Error Log / sent email queue if SMTP isn't wired
   up locally) — the checkout confirmation email's "Net Working Hours" should
   match the same number.
4. Edge case: check in, then check out with a lunch `from`/`to` pair that pokes
   outside the shift window (see test #4 below for how) — before this fix this
   was one of the cases where the two numbers could diverge; now both should
   read the same "0h Xm"-style value drawn from the saved log.

## 4. Lunch-outside-shift: soft warning then hard rejection

**Was:** the live preview (`recalc()`) showed a soft warning ("Lunch ignored")
and let you press "Check out & submit" when your lunch break fell outside your
shift window. The server (`DailyTaskLog._validate_lunch_hours`) then hard-rejects
that exact case with a `frappe.throw`, so the submit always failed anyway — just
after a confusing round trip.

**Changed:** `submitEOD()` now runs the same out-of-shift check *before* calling
the server, and blocks the submit with a clear toast instead of letting it go
through to a guaranteed server error.

**Test:**
1. Check in at, say, 09:00.
2. At EOD, set logout time to 17:00.
3. Turn on "Include lunch" and set lunch `from` = 07:00, `to` = 07:30 (before
   your shift even starts) — the pre-existing soft warning banner should still
   appear under the lunch fields (unchanged).
4. Click "Check out & submit" — confirm you now get an immediate toast error
   ("Lunch break must fall completely within your shift...") and the request
   never reaches the server (no network round trip, no server error toast).
5. Fix the lunch to something inside the shift (e.g. 13:00–13:30) and confirm
   checkout submits normally — regression check that valid lunches still work.
6. Regression check: reversed lunch (`to` before `from`) should still be
   blocked exactly as before ("Lunch end must be after lunch start").

## 5. Debug log spam on every checkout

**Was:** every single EOD submission wrote 2 unconditional rows to Frappe's
Error Log (title "ST NetHours Debug") — forever, for every employee, every day,
with no way to turn it off.

**Changed:** removed the two unconditional log calls. Kept the ones that only
fire on a real anomaly (empty `_to_hhmm` parse, the >24h/negative sanity check,
and the exception handler) — those still work exactly as before for actual
troubleshooting.

**Test:**
1. Note the current row count in Error Log (Desk → Error Log list, or
   `frappe.db.count("Error Log")`).
2. Do a normal check-in + check-out cycle for a test employee.
3. Confirm no new "ST NetHours Debug" rows appear in Error Log for that
   checkout.
4. Regression check: force an anomaly (e.g. temporarily set a nonsensical
   date so `_calc_net_hours` hits its exception path, or just trust the code
   read — the exception/sanity-check branches were untouched) — those should
   still log if something genuinely goes wrong.

## 7. Unescaped employee name in HR/Team-Leader email heading

**Was:** `_notify_hr_and_team_leader`'s HTML heading interpolated the subject
line (which includes the employee's display name) directly into the email HTML
without escaping — the only spot in these emails that skipped `html.escape`,
unlike the detail table and task table.

**Changed:** the heading now goes through `html.escape` (via a
non-shadowed `html_escape` import — the function already has a local variable
named `html` for the message body, so the top-level `html` module can't be
referenced by name inside it). The email `subject` header itself is untouched
(escaping a mail header would be wrong).

**Test:**
1. Do a normal check-in as any employee with HR Manager role users configured
   to receive notifications.
2. Confirm the HR/Team-Leader notification email still shows the employee's
   name correctly in the heading (e.g. "Jane Doe - Check-in - 10-08-2026") — no
   visible change for normal names.
3. (Optional, to prove the fix): temporarily rename a test Employee's
   `employee_name` to include `<b>test</b>`, trigger a check-in, and confirm
   the email heading now shows the literal text `<b>test</b>` instead of
   rendering it bold — then rename the employee back.

## 8. Free-text time parser dropped minutes without an "m" suffix

**Was:** typing `1h30` (no trailing `m`) into an Estimated/Time-taken field
parsed as exactly `1.0` hours — the `30` was silently discarded. Existed
identically in three places: `api.py::_parse_time_to_hours`,
`daily_task.py::_parse_time_to_hours` (Daily Task doctype), and the JS live
preview `parseTimeToHoursJs`.

**Changed:** all three now treat leftover digits after the `h` split (when
there's no `m` suffix) as minutes, so `1h30` → `1.5` everywhere. Every input
that already worked (`1h 30m`, `45m`, `1.5`, `2h`) is unaffected — verified by
tracing each case through the new branch by hand and via `py_compile` +
`node --check` on the edited files.

**Test:**
1. On `/daily-checkin`, add a task and type `1h30` into its "Est." field, tab
   out, then check the redisplayed value — should show `1h 30m`, not `1h`.
2. At EOD, type `1h30` into "Time taken for task completion" for a task, mark
   it Done, and watch the live "Total Task Hours" figure in the sticky
   progress bar — should include the 30 minutes.
3. Submit checkout and confirm `/my-history` shows the correct total for that
   day.
4. Regression check: repeat with `1h 30m`, `45m`, `90m`, `1.5`, and a bare `2` —
   all should parse exactly as they did before (1h30m, 45m, 1h30m, 1h30m, 2h).

---

## 11. Keyboard shortcuts for adding projects/tasks (new feature)

**Added** (`daily_checkin.html`, both the morning check-in form and the EOD/
checkout form):

- **Shift+P** — add a new project (only fires when you're *not* typing in a
  field, so it never interferes with typing a capital P anywhere).
  - Check-in page → same as clicking "Add project" (`addPG()`).
  - EOD page → same as clicking "Add project" in the ad-hoc area
    (`addEodProject()`).
- **Shift+T** — add a new task (same not-typing guard as above).
  - Check-in page → same as clicking "Add task" (`addT()`, standalone task).
  - EOD page → same as clicking "Add additional task" (`addAdhoc()`).
- **Tab**, while your cursor is in a task's description or estimated-time
  field (whether it's a new project's task, a new standalone task, or an
  ad-hoc/additional task) — adds another task line right there, same as
  Enter used to. This is a deliberate behavior change: Tab no longer moves
  focus to the next field in these specific boxes; it creates a new task
  instead.
- **Enter** (in one of those same task fields) or **Ctrl+Enter** (from
  anywhere on the page) — submits the whole form: check-in via
  `submitMorning()` or checkout via `submitEOD()`, whichever button is
  present on the page. Enter no longer adds a new task line — that's now
  Tab's job.

**Also fixed as part of this** — the exact bug you reported: typing in an
ad-hoc task's description that belongs to a *specific* project (added via
"Add additional task to this project") and pressing Tab (or, previously,
Enter) now adds another task to *that same project* instead of spawning an
unrelated new block with its own "Project/Header" field. The old code
couldn't tell the two apart because both used the same CSS class
(`adhoc-d`) — the new task-adding function now checks whether the field
lives inside a project group (`.pg`) and reuses that project's `addAdhocToProj`
if so.

**Scope note:** all of this is intentionally limited to task-entry fields
(description / estimated-time / ad-hoc header boxes). Every other field on
the page — login time, logout time, lunch times, project-name input,
remarks — keeps its native Tab/Enter behavior exactly as before, so normal
keyboard navigation elsewhere on the form is unaffected.

**Test:**
1. On `/daily-checkin` (before checking in), press **Shift+T** anywhere on
   the page (not while focused in a text field) — confirm a new standalone
   task box appears and its description field is focused, same as clicking
   "Add task".
2. Press **Shift+P** — confirm a new project block appears with its "Project
   name..." input focused, same as clicking "Add project".
3. Type a description into that new task, then press **Tab** — confirm
   another task line appears under the same project, and cursor lands in its
   description box (not on the estimated-time field of the row you just
   left).
4. With at least one task present, click into any task description field and
   press **Enter** — confirm it submits check-in (same as clicking "Check in
   & start day"), instead of adding another task row.
5. Repeat with **Ctrl+Enter** from a completely unrelated field (e.g. the
   login-time input) — confirm it also submits check-in.
6. Regression check: click into the "Project name..." input itself and press
   Tab/Enter — confirm nothing unusual happens (native browser behavior,
   unchanged).
7. Check out an employee who already has a project with tasks. Under that
   project, click "Add additional task to this project", type a description,
   and press **Tab** — confirm the new task lands under the *same* project
   (with its project badge), not as a separate new block with a blank
   "Project/Header" field. Press **Shift+P**/**Shift+T** on this page too —
   confirm they add an ad-hoc project / ad-hoc standalone task respectively.
8. Regression check: existing mouse-driven flows (clicking "Add project",
   "Add task", "Add additional task", "Check in", "Check out") all still work
   exactly as before — shortcuts are additive, not replacements for the
   buttons.

## Consciously NOT changed (would risk new bugs for unclear benefit)

- **`if_owner` permission vs. `employee` field mismatch** (Daily Task / Daily
  Task Log doctype permissions) — only bites if HR/Admin ever edits a task on
  an employee's behalf via the Desk UI directly (the app's own API always runs
  `ignore_permissions=True`, so this has zero effect on current usage). Fixing
  it means changing what Frappe's core `owner` field means for these
  doctypes, which affects audit-trail display — a product decision, not a
  drop-in fix.
- **Administrator excluded from real-time notifications but included in
  scheduled HR reports** — genuinely unclear which behavior is intended;
  changing either one changes who currently gets email. Left as-is pending a
  decision on intended behavior.
- **Leftover debug scripts** (`inspect_tasks.py`, `update_doctype.py`,
  `check_db.py`) — dead code, not wired into the app, no functional impact.
  Cleanup, not a bug fix; left alone.

## Pre-existing test failures (not caused by this change)

Verified with `git stash` / `git stash pop` — identical failures before and
after all fixes above:

- `test_4_6_net_hours_sanity_ceiling` — test expects >18h to return `""`, code's
  sanity ceiling is actually >24h. Pre-existing mismatch between test and code.
- 3 errors in the "5.x ad-hoc task" tests — pre-existing
  `"Time Taken for Task Completion is mandatory"` validation errors unrelated
  to anything touched here.

44 tests total, same 1 failure + 3 errors before and after.
