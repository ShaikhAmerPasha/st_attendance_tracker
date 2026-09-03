"""
ST Attendance Tracker v2 — API
All whitelisted methods consumed by web pages.

Fixes:
  - Notifications sent to HR Manager role users + Team Leader(s)
  - No manual recipient configuration needed
  - Team Leader(s) resolved via Employee Department Assignment child table,
    falling back to reports_to for employees with no assignment rows
  - Root task guard prevents duplicate rollover
  - Safety rollover on morning page load
  - Checkin auto-heal if HR deletes record
  - Lunch from/to replacing single lunch_time
  - Login time editable by employee
  - Working hours auto-calculated
  - Carried tasks fully editable before check-in
  - Submit allowed with only carried tasks
  - [FIX] _to_hhmm() normalises timedelta/string before get_datetime() call —
    str(timedelta(seconds=34200)) = '9:30:00' and '9:30:00'[:5] = '9:30:'
    (trailing colon) which dateutil cannot parse, causing net_hours = ''.
  - [FIX] lunch_from/lunch_to display in right panel after EOD submit
    (moved from JS-only to server-rendered in get_context / template)
"""

import frappe
import datetime
import html
import re
from html import escape as html_escape
import json
from frappe.utils import today, now_datetime, getdate, add_days, cint
from frappe.desk.form.load import get_attachments
from st_attendance_tracker.time_utils import parse_duration_to_hours


# ── Employee helper ────────────────────────────────────────────────────────────

def _get_employee():
    emp = frappe.db.get_value(
        "Employee",
        {"user_id": frappe.session.user, "status": "Active"},
        ["name", "employee_name", "department", "reports_to", "designation"],
        as_dict=True,
    )
    if not emp:
        frappe.throw(
            "No Employee record is linked to your user account. "
            "Please contact HR.",
            frappe.PermissionError,
        )
    return emp


def has_permission_daily_work_log(doc, ptype=None, user=None):
    """Registered in hooks.py as Daily Work Log's has_permission hook.

    Gates direct document access (Desk, REST, and this module's own
    doc.insert()/save()/delete() calls) to the doc's own `employee`, not
    Frappe's built-in `if_owner` (creation-time `owner`) — the DocType's own
    permission row deliberately has if_owner unset so this hook is the sole
    per-document gate. `owner` diverges from `employee` whenever a record is
    created on someone's behalf (e.g. assign_task_via_agent, run by a service
    account), which under if_owner would lock the real employee out of their
    own record. HR Manager/System Manager/the task-assignment agent are
    granted here at the role level; dashboards read via frappe.get_all, which
    bypasses this hook entirely, so Team Leader visibility is unaffected.
    """
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    roles = frappe.get_roles(user)
    if {"System Manager", "HR Manager", "ST Task Assignment Agent"} & set(roles):
        return True
    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
    return bool(employee) and doc.employee == employee


def _task_owner_employee(task_name):
    """Resolve a Task Entry child row's owning employee via its parent
    Daily Work Log — child rows don't carry `employee` directly."""
    parent = frappe.db.get_value("Task Entry", task_name, "parent")
    return frappe.db.get_value("Daily Work Log", parent, "employee") if parent else None


def _assert_task_owner(task_name, employee_name):
    """Block cross-employee task mutation via db.set_value (BOLA guard)."""
    task_employee = _task_owner_employee(task_name)
    if task_employee != employee_name:
        frappe.throw("Not authorised to edit this task.", frappe.PermissionError)


def _assert_task_visible(task_name):
    """Allow the task's own employee, HR Manager, or that employee's Team Leader.

    Checks the HR Manager role first, same order as get_employee_task_detail —
    an HR Manager user need not have their own Employee record, so
    _get_employee() must not run unless the role check falls through.
    """
    if "HR Manager" in frappe.get_roles(frappe.session.user):
        return
    task_employee = _task_owner_employee(task_name)
    viewer_employee_name = _get_employee().name
    if task_employee == viewer_employee_name:
        return
    if task_employee in _get_team_members(viewer_employee_name):
        return
    frappe.throw("Not authorised to view this task.", frappe.PermissionError)


def _attach_task_files(tasks):
    """Batch-fetch File attachments for a list of task dicts, setting task['attachments']."""
    if not tasks:
        return
    files_by_task = {}
    for f in frappe.get_all("File",
        filters={"attached_to_doctype": "Task Entry", "attached_to_name": ["in", [t.name for t in tasks]]},
        fields=["name", "file_name", "file_url", "attached_to_name"],
    ):
        files_by_task.setdefault(f.attached_to_name, []).append(f)
    for t in tasks:
        t["attachments"] = files_by_task.get(t.name, [])


def _reparent_attachments(file_names, task_name):
    """Point orphan-uploaded Files (no doctype/docname yet) at the now-saved task.

    `file_names` is client-supplied. Only reparent a File that is (a) still
    genuinely unattached and (b) owned by the calling user — otherwise any
    employee could pass another employee's (or another doctype's) File
    docname here and silently steal that attachment onto their own task,
    since `frappe.db.set_value` bypasses the framework's own permission
    checks. A name that fails either check is just skipped, not thrown on —
    a stale/tampered id in one row shouldn't fail the whole submission.
    """
    if not file_names:
        return
    files = frappe.get_all("File",
        filters={"name": ["in", file_names]},
        fields=["name", "attached_to_doctype", "owner"],
    )
    for f in files:
        if f.attached_to_doctype or f.owner != frappe.session.user:
            continue
        frappe.db.set_value("File", f.name, {
            "attached_to_doctype": "Task Entry",
            "attached_to_name": task_name,
        })


# ── Daily Work Log helpers ──────────────────────────────────────────────────────
# One `Daily Work Log` document per (employee, date), holding check-in/checkout
# fields plus the day's `Task Entry` child rows — replaces the old
# `Daily Task Log` (submit/cancel, one doc per log_type) + standalone
# `Daily Task` (one doc per task) pair. See specs/daily-work-log-refactor.md.

def _get_work_log(employee, date):
    """Fetch the Daily Work Log doc for (employee, date), or None."""
    name = frappe.db.exists("Daily Work Log", {"employee": employee, "date": date})
    return frappe.get_doc("Daily Work Log", name) if name else None


def _get_or_new_work_log(employee, date):
    """Fetch the Daily Work Log for (employee, date), or an unsaved in-memory
    one if none exists yet — caller is responsible for saving it."""
    log = _get_work_log(employee, date)
    if log:
        return log
    log = frappe.new_doc("Daily Work Log")
    log.employee = employee
    log.date = date
    return log


def _save_work_log(log):
    # Permission-checked, not bypassed — has_permission_daily_work_log (hooks.py)
    # gates this to the doc's own employee (or HR/System Manager/the
    # task-assignment agent), so a caller acting on another employee's log
    # without one of those roles is stopped here rather than relying solely
    # on each controller method's own ownership checks.
    if log.is_new():
        log.insert()
    else:
        log.save()


def _next_sequence(work_log):
    """Next entry-order value for a new Task Entry row on this work log."""
    return max([row.sequence or 0 for row in work_log.tasks], default=0)


def _task_entry_dict(row, parent_date):
    """Flat dict view of a Task Entry child row (ORM row or plain dict from
    frappe.get_all), shaped like the old standalone Daily Task doctype so
    downstream code (dashboards, emails, my-history) needs no changes.
    `rolled_over_from` is synthesised as a truthy marker — every existing
    caller only ever checks it for truthiness, never reads the value. Real
    lineage is tracked via `series_id`/`origin_date` now (spec Section 4).
    """
    get = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))
    origin = get("origin_date")
    is_carried = bool(origin) and str(origin) != str(parent_date)
    return frappe._dict({
        "name": get("name"),
        "description": get("description"),
        "status": get("status"),
        "task_type": get("task_type"),
        "origin_date": origin,
        "rolled_over_from": "carried" if is_carried else None,
        "is_carried": is_carried,
        "remarks": get("remarks"),
        "estimated_time": get("estimated_time"),
        "actual_time": get("actual_time"),
        "project_name": get("project_name"),
        "series_id": get("series_id"),
    })


def _resolve_task_row(rows_by_name, task_name, work_log, employee_name, context):
    """Look up a Task Entry row the client is trying to edit, or fail with a
    refresh-and-retry error instead of a misleading "not authorised" one.

    The client only ever submits row names it rendered from an earlier
    server response, so a miss here isn't a permission violation — the row
    was deleted, rolled over into a new copy, or otherwise changed server-side
    between page load and submit (e.g. a stale tab held open across a
    same-day edit made from another tab/device, or a Team Leader/HR editing
    the same Daily Work Log via Desk). Logged so a real occurrence is
    diagnosable instead of a one-off support report with no trail.
    """
    row = rows_by_name.get(task_name)
    if not row:
        frappe.log_error(
            f"{context}: task '{task_name}' not found on {employee_name}'s "
            f"{work_log.date} Daily Work Log ({work_log.name}). "
            f"Rows currently on it: {[r.name for r in work_log.tasks]}",
            "ST Attendance Tracker — stale task reference"
        )
        frappe.throw(
            "This task could not be found — it may have been updated from "
            "another tab or device. Please refresh the page and try again.",
            frappe.ValidationError
        )
    return row


def _is_series_done(series_id):
    """True if any copy of this task's lineage has ever been marked Done."""
    return bool(series_id) and bool(frappe.db.exists("Task Entry", {
        "series_id": series_id, "status": "Done",
    }))


def _cascade_series_done(series_id, as_of_date):
    """Mark every past/current copy of a task's lineage Done, and delete every
    future carried-forward copy that hasn't been touched yet. Replaces the old
    rolled_over_from ancestor-walk + future-chain delete with two indexed
    queries instead of walking a chain. See spec Section 4.2."""
    if not series_id:
        return
    frappe.db.sql("""
        UPDATE `tabTask Entry` te
        INNER JOIN `tabDaily Work Log` dwl ON dwl.name = te.parent
        SET te.status = 'Done'
        WHERE te.series_id = %s AND dwl.date <= %s
    """, (series_id, as_of_date))
    future_rows = frappe.db.sql("""
        SELECT te.name FROM `tabTask Entry` te
        INNER JOIN `tabDaily Work Log` dwl ON dwl.name = te.parent
        WHERE te.series_id = %s AND dwl.date > %s
          AND te.status IN ('Pending', 'In Progress', 'Rolled Over')
    """, (series_id, as_of_date), as_dict=True)
    for row in future_rows:
        frappe.db.delete("Task Entry", {"name": row.name})


def _is_half_day_leave_today(employee_name, date):
    """True if employee has an approved half-day Leave Application covering `date`."""
    dt = getdate(date)
    exact = frappe.db.exists("Leave Application", {
        "employee": employee_name, "status": "Approved", "docstatus": 1,
        "half_day": 1, "half_day_date": dt,
    })
    if exact:
        return True
    return bool(frappe.db.exists("Leave Application", {
        "employee": employee_name, "status": "Approved", "docstatus": 1,
        "half_day": 1, "from_date": ["<=", dt], "to_date": [">=", dt],
    }))


def _resolve_half_day_session(employee_name, date, raw_value):
    """Validate + persist a half-day session choice. Informational only —
    never blocks check-in either way, just refuses to store nonsense."""
    value = (raw_value or "").strip()
    if value not in ("First Half", "Second Half"):
        return ""
    if not _is_half_day_leave_today(employee_name, date):
        return ""
    return value


# ── Team leader check ──────────────────────────────────────────────────────────

def _get_team_members(employee_name):
    """
    All active employees this person leads — via reports_to OR any Employee
    Department Assignment row naming them Team Leader. Keeps dashboard
    visibility aligned with who actually gets notified for that employee
    (_get_team_leader_emails draws from the same two sources) — previously
    the dashboard only checked reports_to, so a Team Leader defined solely
    via the Department Assignment table got emailed about an employee they
    couldn't actually see on /team-dashboard.

    Results are cached in Redis for 5 minutes to avoid repeating 2-3 DB
    queries on every call (this function is invoked multiple times per
    request from _assert_task_visible, get_page_state, get_team_dashboard,
    and both www/*.py context builders). Team membership changes
    infrequently — a few minutes of staleness is acceptable for read paths.
    """
    cache_key = f"st_att:team_members:{employee_name}"
    # expires=True: without it, a miss gets memoized as None in frappe.local's
    # per-request cache, and set_value()'s TTL below never clears that memo —
    # so a later call in the same request would keep returning the stale
    # None even after Redis has the real value (see _get_attendance_settings
    # for the full explanation; same bug, same fix).
    cached = frappe.cache().get_value(cache_key, expires=True)
    if cached is not None:
        return cached

    reports_to_names = frappe.get_all("Employee", filters={
        "reports_to": employee_name, "status": "Active",
    }, pluck="name")

    eda_parents = frappe.get_all("Employee Department Assignment",
        filters={"parenttype": "Employee", "team_leader": employee_name},
        pluck="parent")
    eda_names = frappe.get_all("Employee", filters={
        "name": ["in", eda_parents], "status": "Active",
    }, pluck="name") if eda_parents else []

    result = list(set(reports_to_names) | set(eda_names))
    frappe.cache().set_value(cache_key, result, expires_in_sec=300)
    return result


def _is_team_leader(employee_name):
    """True if this person leads at least one active employee (reports_to or EDA)."""
    return bool(_get_team_members(employee_name))


# ── ST Attendance Settings cache ────────────────────────────────────────────────

def _get_attendance_settings():
    """All ST Attendance Settings fields, cached — read on nearly every
    check-in/checkout/dashboard load but change only a handful of times
    a year. Invalidated explicitly on save (see clear_attendance_settings_cache),
    not a TTL guess, so HR sees changes immediately."""
    cache_key = "st_att:settings"
    # expires=True: without it, RedisWrapper.get_value() on a miss caches the
    # `None` into frappe.local's per-request memory cache (redis_wrapper.py),
    # and since set_value() with a TTL never clears that local entry, every
    # later get_value() call *in the same request* would keep returning the
    # stale None from local memory without ever checking Redis again — even
    # though Redis now holds the real value written just below. Only matters
    # right after a cache-cold miss; a warm cache is unaffected either way.
    cached = frappe.cache().get_value(cache_key, expires=True)
    if cached is not None:
        return cached
    settings = frappe.get_single("ST Attendance Settings").as_dict()
    frappe.cache().set_value(cache_key, settings, expires_in_sec=3600)
    return settings


def clear_attendance_settings_cache(doc=None, method=None):
    frappe.cache().delete_value("st_att:settings")


# ── HR Manager emails ──────────────────────────────────────────────────────────

def _get_hr_manager_emails():
    """Fetch emails of all enabled users with HR Manager role."""
    rows = frappe.db.sql("""
        SELECT DISTINCT u.email
        FROM `tabUser` u
        INNER JOIN `tabHas Role` hr ON hr.parent = u.name
        WHERE hr.role = 'HR Manager'
          AND u.enabled = 1
          AND u.email IS NOT NULL
          AND u.email != ''
          AND u.name != 'Administrator'
    """, as_dict=True)
    return [r.email for r in rows if r.email]


# ── Checkin helper ─────────────────────────────────────────────────────────────

def _make_checkin(employee, log_type, time_value=None):
    try:
        checkin = frappe.new_doc("Employee Checkin")
        checkin.employee = employee
        checkin.log_type = log_type
        if time_value:
            if isinstance(time_value, str):
                parts = (time_value + ":00").split(":")[:3]
                t = datetime.time(int(parts[0]), int(parts[1]), int(parts[2]))
            else:
                t = time_value
            checkin.time = datetime.datetime.combine(datetime.date.today(), t)
        else:
            checkin.time = now_datetime()
        checkin.device_id = "ST Daily Checkin"
        
        # Integrate with active HRMS Shift Assignment
        try:
            from hrms.hr.doctype.shift_assignment.shift_assignment import get_employee_shift
            shift = get_employee_shift(employee, checkin.time, consider_default_shift=True)
            if shift:
                checkin.shift = shift.shift_type.name
        except Exception:
            frappe.log_error(frappe.get_traceback(), "ST Attendance Tracker — checkin shift lookup failed")

        checkin.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(
            f"Checkin failed for {employee} ({log_type}): {e}",
            "ST Attendance Tracker"
        )


def _format_hours(val):
    if not val:
        return ""
    try:
        val_float = float(val)
        if val_float <= 0:
            return ""
        total_minutes = int(round(val_float * 60))
        h, m = divmod(total_minutes, 60)
        if h > 0 and m > 0:
            return f"{h}h {m}m"
        elif h > 0:
            return f"{h}h"
        elif m > 0:
            return f"{m}m"
        return ""
    except Exception:
        return str(val)


def _parse_time_to_hours(s):
    return parse_duration_to_hours(s)


def _to_hhmm(t):
    """
    Safely convert any time value to 'HH:MM' string.

    Handles:
      • datetime.timedelta  — returned by Frappe DB for Time fields.
        str(timedelta(seconds=34200)) = '9:30:00'.
        '9:30:00'[:5] = '9:30:' (trailing colon) which
        frappe.utils.get_datetime / dateutil CANNOT parse → exception →
        _calc_net_hours silently returns '' → net_hours stored as empty.
      • 'H:MM:SS' / 'HH:MM:SS' — strings with or without leading zero
      • 'HH:MM' — already-clean strings from JavaScript input[type=time]
      • AM/PM formats like '05:19 pm' or '5:19 PM'
      • None / '' — returns ''
    """
    if isinstance(t, datetime.timedelta):
        secs = int(t.total_seconds())
        return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}"
    s = str(t or "").strip().lower()
    if not s:
        return ""
    is_pm = "pm" in s
    is_am = "am" in s
    s = s.replace("pm", "").replace("am", "").strip()
    parts = s.split(":")
    if len(parts) >= 2:
        try:
            h = int(parts[0])
            m = int(parts[1])
            if is_pm and h < 12:
                h += 12
            elif is_am and h == 12:
                h = 0
            return f"{h:02d}:{m:02d}"
        except Exception:
            pass
    return ""


def _to_ampm(t):
    """
    Converts a time/timedelta/string to 'HH:MM AM/PM' format.
    """
    hhmm = _to_hhmm(t)
    if not hhmm:
        return ""
    try:
        parts = hhmm.split(":")
        h = int(parts[0])
        m = int(parts[1])
        ampm = "PM" if h >= 12 else "AM"
        h = h % 12
        if h == 0:
            h = 12
        return f"{h:02d}:{m:02d} {ampm}"
    except Exception:
        return hhmm


def _format_action_timestamp(dt):
    """
    Full 'DD Mon YYYY, HH:MM AM/PM' timestamp of the actual moment an
    action happened (server clock, captured when the request hit the
    server) — independent of the Login/Logout Time fields, which the
    employee can edit. Lets management reading the email later know
    exactly when check-in/check-out was really pressed.
    """
    return dt.strftime("%d %b %Y, %I:%M %p")


def _format_email_date(date):
    """dd-mm-yyyy for the 'Date'/'Shift Date' row in email detail cards —
    matches the format already used in every email subject line."""
    return frappe.utils.getdate(date).strftime("%d-%m-%Y")


# ── Notification helper ────────────────────────────────────────────────────────

# Shared, single-tone styling — every email uses the same font/width/accent color.
EMAIL_ACCENT_COLOR = "#1e3a5f"
EMAIL_WRAPPER_STYLE = (
    "font-family:'Segoe UI', Arial, sans-serif; max-width: 680px; "
    "margin: 0 auto; padding: 16px; color: #1f2937;"
)
EMAIL_HEADING_STYLE = (
    f"font-size: 18px; font-weight: bold; color: {EMAIL_ACCENT_COLOR}; margin-bottom: 12px;"
)
EMAIL_SUBHEADING_STYLE = (
    f"font-size: 16px; font-weight: bold; color: {EMAIL_ACCENT_COLOR}; "
    "margin: 20px 0 12px;"
)


def _render_detail_table(rows):
    """
    Render a clean label/value detail card. rows: list of (label, value)
    tuples; any row with a falsy value is skipped. Single consistent
    style for every row — no per-field colors.
    """
    visible_rows = [(label, value) for label, value in rows if value]
    if not visible_rows:
        return ""

    body = "".join(
        f"""
        <tr>
          <td style="padding:7px 12px;font-size:13px;color:#64748b;width:40%;
                     border-bottom:1px solid #e2e8f0;vertical-align:top">{html.escape(str(label))}</td>
          <td style="padding:7px 12px;font-size:13px;font-weight:600;color:#1f2937;
                     border-bottom:1px solid #e2e8f0;vertical-align:top">{html.escape(str(value))}</td>
        </tr>
        """
        for label, value in visible_rows
    )
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;
           border:1px solid #e2e8f0;border-radius:6px;overflow:hidden;margin-bottom:8px;
           font-family:'Segoe UI', Arial, sans-serif">
      <tbody>{body}</tbody>
    </table>
    """


# ── Employee self-notification emails ─────────────────────────────────────────

def _send_employee_checkin_email(employee_name, employee_display_name, login_time,
                                  checkin_action_time, work_location, half_day_session,
                                  is_late, tasks, date, department=None):
    """
    Send check-in confirmation email to the employee themselves.
    Includes a detail card (login time, work location, ...) and
    today's planned task list grouped by project.
    """
    try:
        emp_email = _get_employee_email(employee_name)
        if not emp_email:
            return

        login_hm = _to_ampm(login_time)
        detail_table_html = _render_detail_table([
            ("Employee", employee_display_name),
            ("Date", _format_email_date(date)),
            ("Login Time", login_hm),
            ("Checked-In At", _format_action_timestamp(checkin_action_time)),
            ("Work Location", work_location),
            ("Half-Day Session", half_day_session),
            ("Status", "Late Check-in" if is_late else None),
        ])
        task_table_html = _get_task_summary_html(tasks, department, is_checkout=False)

        html = f"""
        <div style="{EMAIL_WRAPPER_STYLE}">
          <div style="{EMAIL_HEADING_STYLE}">Check-In Confirmation</div>
          {detail_table_html}
          <div style="{EMAIL_SUBHEADING_STYLE}">Today's Work To Do</div>
          <div>
            {task_table_html}
          </div>
        </div>
        """

        sender, reply_to = _employee_mail_identity(employee_name, employee_display_name)
        frappe.sendmail(
            recipients=[emp_email],
            subject=f"{employee_display_name} - Check-in - {frappe.utils.getdate(date).strftime('%d-%m-%Y')}",
            message=html,
            now=False,
            sender=sender,
            reply_to=reply_to,
        )
    except Exception as e:
        frappe.log_error(
            f"Check-in email failed for {employee_name}: {e}",
            "ST Attendance Tracker"
        )


def _send_employee_eod_email(employee_name, employee_display_name, logout_time,
                              checkout_action_time, net_hours, work_location,
                              half_day_session, tasks, date,
                              is_late_checkout=False, submission_date=None, department=None):
    """
    Send EOD confirmation email to the employee themselves.
    Includes a detail card (hours, work location, ...), done tasks,
    and carried-forward tasks.
    """
    try:
        emp_email = _get_employee_email(employee_name)
        if not emp_email:
            return

        done_tasks = [t for t in tasks if t.get("status") == "Done"]

        # Fetch lunch and check-in times from the day's Daily Work Log
        work_log = _get_work_log(employee_name, date)
        lunch_from = work_log.lunch_from if work_log else None
        lunch_to = work_log.lunch_to if work_log else None
        login_time = work_log.login_time if work_log else None

        login_hm = _to_ampm(login_time) if login_time else None
        logout_hm = _to_ampm(logout_time)
        lunch_from_hm = _to_ampm(lunch_from) if lunch_from else None
        lunch_to_hm = _to_ampm(lunch_to) if lunch_to else None
        lunch_break = f"{lunch_from_hm} - {lunch_to_hm}" if lunch_from_hm and lunch_to_hm else None

        task_table_html = _get_task_summary_html(
            tasks, department, is_checkout=True, lunch_from=lunch_from_hm, lunch_to=lunch_to_hm
        )

        total_actual_hours = sum(float(t.get("actual_time") or 0.0) for t in tasks)
        working_hours_str = _format_hours(total_actual_hours) or "0h"

        detail_table_html = _render_detail_table([
            ("Employee", employee_display_name),
            ("Shift Date", _format_email_date(date)),
            ("Login Time", login_hm),
            ("Logout Time", logout_hm),
            ("Checked-Out At", _format_action_timestamp(checkout_action_time)),
            ("Lunch Break", lunch_break),
            ("Work Location", work_location),
            ("Net Working Hours", net_hours),
            ("Total Task Hours", working_hours_str),
            ("Half-Day Session", half_day_session),
            ("Tasks Completed", f"{len(done_tasks)}/{len(tasks)}"),
        ])

        late_note_html = ""
        work_summary_heading = "Today's Work Summary"
        if is_late_checkout:
            late_note_html = f"""<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:10px 14px;margin-bottom:14px;font-size:13px;color:#92400e;">
              Note: this checkout is for your shift on <strong>{date}</strong>, submitted on {submission_date} because checkout was missed at the time.
            </div>"""
            work_summary_heading = f"Work Summary for {frappe.utils.getdate(date).strftime('%d %b %Y')}"

        html = f"""
        <div style="{EMAIL_WRAPPER_STYLE}">
          <div style="{EMAIL_HEADING_STYLE}">Checkout Summary</div>
          {late_note_html}
          {detail_table_html}
          <div style="{EMAIL_SUBHEADING_STYLE}">{work_summary_heading}</div>
          <div>
            {task_table_html}
          </div>
        </div>"""

        sender, reply_to = _employee_mail_identity(employee_name, employee_display_name)
        frappe.sendmail(
            recipients=[emp_email],
            subject=f"{employee_display_name} - Check-out - {frappe.utils.getdate(date).strftime('%d-%m-%Y')}",
            message=html,
            now=False,
            sender=sender,
            reply_to=reply_to,
            attachments=_task_attachment_specs(tasks),
        )
    except Exception as e:
        frappe.log_error(
            f"EOD email failed for {employee_name}: {e}",
            "ST Attendance Tracker"
        )


def _get_employee_email(employee_name):
    """Get employee's best available email address."""
    emp = frappe.db.get_value("Employee", employee_name,
        ["prefered_email", "company_email", "personal_email", "user_id"],
        as_dict=True)
    if not emp:
        return None
    email = (
        emp.get("prefered_email") or
        emp.get("company_email") or
        emp.get("personal_email")
    )
    if not email and emp.get("user_id"):
        candidate = frappe.db.get_value("User", emp["user_id"], "email") or emp["user_id"]
        email = candidate if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", candidate or "") else None
    return email


def _employee_mail_identity(employee_name, employee_display_name):
    """(sender, reply_to) so a notification about this employee's check-in/
    checkout looks like it's from them, while still relaying through the
    site's one working outgoing account (avoids From/SPF-DKIM mismatch).
    Falls back to (None, None) if no default outgoing account is configured,
    so frappe.sendmail just uses its normal default sender."""
    account_email = frappe.db.get_value("Email Account", {"default_outgoing": 1}, "email_id")
    if not account_email:
        return None, None
    return frappe.utils.formataddr((employee_display_name, account_email)), _get_employee_email(employee_name)


def _task_attachment_specs(tasks):
    """frappe.sendmail(attachments=...) entries for every file across these
    tasks, so the email carries the actual files, not just a mention of them."""
    return [{"fid": f["name"]} for t in (tasks or []) for f in (t.get("attachments") or [])]


def _attachment_names_html(task):
    """Small '📎 filename, filename' line so the reader can tell which
    files (already MIME-attached to the email by the caller) belong to
    which task."""
    names = [html.escape(f["file_name"]) for f in (task.get("attachments") or [])]
    if not names:
        return ""
    return f'<div style="font-size:11.5px;color:#64748b;margin-top:4px">📎 {", ".join(names)}</div>'


def _render_screenshot_task_table(tasks, is_checkout=False, lunch_from=None, lunch_to=None):
    if not tasks:
        return '<p style="font-size:13px;color:#9ca3af;font-style:italic;margin:6px 0">No tasks added yet.</p>'

    # Group tasks by project, preserving the order projects were first entered in
    groups = {}
    for t in tasks:
        pname = (t.get("project_name") or "").strip()
        if not pname:
            pname = "General"
        groups.setdefault(pname, []).append(t)

    sorted_group_names = list(groups.keys())

    # Single accent color for all project names; alternating neutral shade
    # per group is the only visual separator — no rainbow.
    palettes = [
        {"bg": "#ffffff", "border": "#e2e8f0", "text": EMAIL_ACCENT_COLOR},
        {"bg": "#f8fafc", "border": "#e2e8f0", "text": EMAIL_ACCENT_COLOR},
    ]

    html_rows = []

    header_html = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:16px;font-family:'Segoe UI', Arial, sans-serif;border:1px solid #e2e8f0;table-layout:fixed">
      <thead>
        <tr style="background-color:{EMAIL_ACCENT_COLOR};color:#ffffff;font-size:12.5px;font-weight:bold">
          <th style="padding:12px 10px;text-align:left;border:1px solid #e2e8f0;width:20%">Client</th>
          <th style="padding:12px 10px;text-align:left;border:1px solid #e2e8f0;width:50%">Task / Work</th>
          <th style="padding:12px 10px;text-align:center;border:1px solid #e2e8f0;width:15%">Time</th>
          <th style="padding:12px 10px;text-align:center;border:1px solid #e2e8f0;width:15%">Status</th>
        </tr>
      </thead>
      <tbody>
    """
    html_rows.append(header_html)

    lunch_shown = False
    for group_idx, pname in enumerate(sorted_group_names):
        palette = palettes[group_idx % len(palettes)]
        ptasks = groups[pname]

        if group_idx > 0:
            # Dynamic placement of Lunch separator row
            if lunch_from and lunch_to and not lunch_shown:
                show_lunch = False
                if len(sorted_group_names) <= 2 and group_idx == 1:
                    show_lunch = True
                elif len(sorted_group_names) > 2 and group_idx == 2:
                    show_lunch = True

                if show_lunch:
                    lunch_html = f"""
                    <tr style="height:12px;line-height:12px"><td colspan="4" style="border:none;background-color:transparent;height:12px;padding:0;margin:0"></td></tr>
                    <tr>
                      <td colspan="4" style="border:none;background-color:transparent;font-size:13px;color:#4b5563;font-style:italic;padding:12px 4px;font-family:'Segoe UI', Arial, sans-serif">
                        lunch: {lunch_from} - {lunch_to}
                      </td>
                    </tr>
                    <tr style="height:12px;line-height:12px"><td colspan="4" style="border:none;background-color:transparent;height:12px;padding:0;margin:0"></td></tr>
                    """
                    html_rows.append(lunch_html)
                    lunch_shown = True

            if not (lunch_shown and group_idx == 2):
                sep_html = """
                <tr style="height:12px;line-height:12px">
                  <td colspan="4" style="border:none;background-color:transparent;height:12px;padding:0;margin:0"></td>
                </tr>
                """
                html_rows.append(sep_html)

        for task_idx, t in enumerate(ptasks):
            client_text = html.escape(pname) if task_idx == 0 else ""
            client_style = f"padding:10px 12px;font-size:13px;font-weight:bold;color:{palette['text']};background-color:{palette['bg']};border:1px solid {palette['border']};vertical-align:top;word-wrap:break-word;overflow:hidden;text-overflow:ellipsis" if task_idx == 0 else f"padding:10px 12px;background-color:{palette['bg']};border:1px solid {palette['border']};vertical-align:top"

            badges = []
            if t.get("rolled_over_from"):
                badges.append('<span style="background-color:#fffbeb;color:#b45309;font-size:10px;font-weight:600;padding:2px 6px;border-radius:4px;border:1px solid #fde68a;margin-left:6px;display:inline-block">Carried</span>')
            if t.get("task_type") == "Ad-hoc":
                badges.append('<span style="background-color:#faf5ff;color:#7c3aed;font-size:10px;font-weight:600;padding:2px 6px;border-radius:4px;border:1px solid #e9d5ff;margin-left:6px;display:inline-block">Additional</span>')
            
            badges_str = "".join(badges)
            task_style = f"padding:10px 12px;font-size:13px;color:#334155;background-color:{palette['bg']};border:1px solid {palette['border']};vertical-align:top;word-wrap:break-word"

            if is_checkout:
                raw_time = t.get("actual_time") or t.get("estimated_time") or ""
            else:
                raw_time = t.get("estimated_time") or ""
            time_val = _format_hours(raw_time)
            time_style = f"padding:10px 12px;font-size:12px;color:#475569;text-align:center;background-color:{palette['bg']};border:1px solid {palette['border']};vertical-align:middle"

            status_val = ""
            status_style_color = "#475569"
            if is_checkout:
                if t.get("status") == "Done":
                    status_val = "✔ Done"
                    status_style_color = "#047857"
                else:
                    status_val = "Pending"
                    status_style_color = "#b45309"
            
            status_style = f"padding:10px 12px;font-size:12px;font-weight:bold;text-align:center;color:{status_style_color};background-color:#fef8e7;border:1px solid {palette['border']};vertical-align:middle"

            remark_html = ""
            if is_checkout and t.get("remarks"):
                remark_html = f'<div style="font-size:11.5px;color:#64748b;font-style:italic;margin-top:4px">Remark: {html.escape(t.get("remarks"))}</div>'
            remark_html += _attachment_names_html(t)

            row_html = f"""
            <tr>
              <td style="{client_style}">{client_text}</td>
              <td style="{task_style}">{html.escape(t.get("description") or "")}{badges_str}{remark_html}</td>
              <td style="{time_style}">{time_val}</td>
              <td style="{status_style}">{status_val}</td>
            </tr>
            """
            html_rows.append(row_html)

    if lunch_from and lunch_to and not lunch_shown:
        lunch_html = f"""
        <tr style="height:12px;line-height:12px"><td colspan="4" style="border:none;background-color:transparent;height:12px;padding:0;margin:0"></td></tr>
        <tr>
          <td colspan="4" style="border:none;background-color:transparent;font-size:13px;color:#4b5563;font-style:italic;padding:12px 4px;font-family:'Segoe UI', Arial, sans-serif">
            lunch: {lunch_from} - {lunch_to}
          </td>
        </tr>
        """
        html_rows.append(lunch_html)

    html_rows.append("  </tbody>\n</table>")
    return "".join(html_rows)


def _render_grouped_task_summary(tasks, is_checkout=False, lunch_from=None, lunch_to=None):
    """Task list grouped by project, rendered as clean plain-text-style HTML.

    Designed for readability on mobile email clients: no absolute positioning,
    no colored badge spans, no tables. Projects are bold headings; tasks are
    dash-prefixed lines with metadata on a subtle second line.

    Department-level alternative to the tabular layout — selected via the
    "Task Summary Email Format = Grouped List" setting on Department.
    """
    if not tasks:
        return (
            '<p style="font-size:14px;color:#9ca3af;font-style:italic;'
            'margin:8px 0;font-family:\'Segoe UI\',Arial,sans-serif">'
            'No tasks added yet.</p>'
        )

    # ── Group tasks by project, preserving insertion order ──────────────
    groups = {}
    for t in tasks:
        pname = (t.get("project_name") or "").strip() or "General"
        groups.setdefault(pname, []).append(t)

    sorted_group_names = list(groups.keys())

    # Common inline styles — defined once, reused everywhere.
    FONT = "font-family:'Segoe UI',Arial,sans-serif"
    # Outer wrapper: generous line-height, fills parent.
    WRAP = f"{FONT};line-height:1.6;color:#1f2937"
    # Project heading: bold, slightly larger, thin bottom rule.
    PROJ_HEAD = (
        f"{FONT};font-size:15px;font-weight:700;color:#1e293b;"
        "padding:0 0 5px 0;margin:0 0 8px 0;"
        "border-bottom:1px solid #e2e8f0"
    )
    # Task description line (pre-wrap preserves spaces and newlines).
    TASK_LINE = f"{FONT};font-size:14px;color:#334155;margin:0;padding:0;white-space:pre-wrap"
    # Subtle metadata line (time, status, labels, remarks).
    META_LINE = f"{FONT};font-size:12px;color:#64748b;margin:2px 0 0 18px;padding:0"
    # Lunch break separator.
    LUNCH = f"{FONT};font-size:13px;color:#64748b;font-style:italic;margin:4px 0 14px 0;padding:0"

    # ── Build lunch HTML (inserted between project blocks) ─────────────
    lunch_html = ""
    if lunch_from and lunch_to:
        lunch_html = (
            f'<p style="{LUNCH}">Lunch: {html.escape(lunch_from)} – '
            f'{html.escape(lunch_to)}</p>'
        )

    # ── Render each project group ──────────────────────────────────────
    group_blocks = []
    for group_idx, pname in enumerate(sorted_group_names):
        lines = []
        for task_idx, t in enumerate(groups[pname]):
            # Replace \n with <br> to ensure email clients render newlines correctly.
            # white-space: pre-wrap will also handle any consecutive spaces.
            desc = html.escape(t.get("description") or "").replace('\n', '<br>')

            # ── Inline labels (plain text, parenthesised) ──────────────
            labels = []
            if t.get("task_type") == "Recurring":
                labels.append("Recurring")
            if t.get("rolled_over_from"):
                labels.append("Carried")
            if t.get("task_type") == "Ad-hoc":
                labels.append("Additional")
            label_text = f' ({", ".join(labels)})' if labels else ""

            # ── Status / time info ─────────────────────────────────────
            meta_parts = []
            if is_checkout:
                if t.get("status") == "Done":
                    meta_parts.append("✔ Done")
                else:
                    meta_parts.append("○ Pending")

                time_val = _format_hours(t.get("actual_time") or "")
                if time_val:
                    meta_parts.append(f"Actual: {time_val}")
                else:
                    est_val = _format_hours(t.get("estimated_time") or "")
                    if est_val:
                        meta_parts.append(f"Est: {est_val}")
            else:
                est_val = _format_hours(t.get("estimated_time") or "")
                if est_val:
                    meta_parts.append(f"Est: {est_val}")

            # ── Remark ─────────────────────────────────────────────────
            if is_checkout and t.get("remarks"):
                meta_parts.append(f"Remark: {html.escape(t.get('remarks'))}")

            # ── Attachments ────────────────────────────────────────────
            att_names = [html.escape(f["file_name"]) for f in (t.get("attachments") or [])]
            if att_names:
                meta_parts.append(f'📎 {", ".join(att_names)}')

            # ── Assemble the task block ────────────────────────────────
            # Task number + description on the primary line
            num = task_idx + 1
            task_html = (
                f'<p style="{TASK_LINE}">'
                f'&nbsp;&nbsp;{num}. {desc}{html.escape(label_text)}'
                f'</p>'
            )

            # Metadata on a second, lighter line (if any)
            if meta_parts:
                task_html += (
                    f'<p style="{META_LINE}">'
                    f'{" · ".join(meta_parts)}'
                    f'</p>'
                )

            lines.append(task_html)

        # ── Assemble the project block ─────────────────────────────────
        task_count = len(groups[pname])
        count_note = f' — {task_count} task{"s" if task_count != 1 else ""}'
        project_block = (
            f'<div style="margin:0 0 18px 0">'
            f'<p style="{PROJ_HEAD}">'
            f'{html.escape(pname)}'
            f'<span style="font-weight:400;font-size:12px;color:#94a3b8">'
            f'{count_note}</span></p>'
            f'{"".join(lines)}'
            f'</div>'
        )
        group_blocks.append(project_block)

        # Same placement rule as the tabular layout: lunch appears after the
        # first group for a 2-group day, after the second for 3+ groups.
        if lunch_html:
            show_lunch = (
                (len(sorted_group_names) <= 2 and group_idx == 0) or
                (len(sorted_group_names) > 2 and group_idx == 1)
            )
            if show_lunch:
                group_blocks.append(lunch_html)
                lunch_html = ""

    # Append lunch if it hasn't been placed yet (single-group edge case).
    if lunch_html:
        group_blocks.append(lunch_html)

    return f'<div style="{WRAP}">{"".join(group_blocks)}</div>'


def _get_task_summary_html(tasks, department, is_checkout=False, lunch_from=None, lunch_to=None):
    """Pick the task-summary renderer for this department, defaulting to
    the tabular layout when unset (covers departments that never opted in
    and any department without the custom field populated yet)."""
    fmt = frappe.db.get_value("Department", department, "task_summary_email_format") if department else None
    if fmt == "Grouped List":
        return _render_grouped_task_summary(tasks, is_checkout=is_checkout, lunch_from=lunch_from, lunch_to=lunch_to)
    return _render_screenshot_task_table(tasks, is_checkout=is_checkout, lunch_from=lunch_from, lunch_to=lunch_to)


def _get_team_leader_emails(employee_name):
    """
    Team Leader(s) to notify for this employee. Employees working across
    multiple departments (Employee Department Assignment child table) get
    every listed department's Team Leader notified. Employees with no
    assignment rows yet fall back to the legacy single reports_to manager.
    """
    team_leaders = frappe.get_all("Employee Department Assignment",
        filters={"parenttype": "Employee", "parent": employee_name},
        pluck="team_leader")

    if not team_leaders:
        reports_to = frappe.db.get_value("Employee", employee_name, "reports_to")
        team_leaders = [reports_to] if reports_to else []

    emails = []
    for tl in set(team_leaders):
        tl_user = frappe.db.get_value("Employee", tl, "user_id")
        if tl_user and tl_user not in emails:
            emails.append(tl_user)
    return emails


def _notify_hr_and_team_leader(employee_name, employee_display_name, event, detail_rows=None, tasks=None, department=None):
    """
    Send email to:
    1. All users with HR Manager role (auto-fetched)
    2. The employee's Team Leader(s) — see _get_team_leader_emails

    detail_rows: list of (label, value) tuples — same structured detail
                 card shown to the employee, so HR/TL see identical facts.
    tasks: optional list of Daily Task dicts for the relevant date.
    """
    try:
        recipients = _get_hr_manager_emails()

        for tl_user in _get_team_leader_emails(employee_name):
            if tl_user not in recipients:
                recipients.append(tl_user)

        if not recipients:
            return

        recipients = list(set(recipients))

        email_date = tasks[0].get("task_date") if (tasks and tasks[0].get("task_date")) else frappe.utils.today()
        email_date_label = frappe.utils.getdate(email_date).strftime('%d-%m-%Y')
        subject_map = {
            "checkin":  f"{employee_display_name} - Check-in - {email_date_label}",
            "checkout": f"{employee_display_name} - Check-out - {email_date_label}",
        }
        subject = subject_map.get(event, f"{employee_display_name} - Update - {email_date_label}")

        lunch_from_hm = None
        lunch_to_hm = None
        if event == "checkout":
            date = email_date
            work_log = _get_work_log(employee_name, date)
            if work_log:
                lunch_from_hm = _to_ampm(work_log.lunch_from) if work_log.lunch_from else None
                lunch_to_hm = _to_ampm(work_log.lunch_to) if work_log.lunch_to else None

        task_html = _get_task_summary_html(
            tasks or [],
            department,
            is_checkout=(event == "checkout"),
            lunch_from=lunch_from_hm,
            lunch_to=lunch_to_hm
        )
        task_label = "Today's Tasks" if event == "checkin" else "Task Summary"
        detail_table_html = _render_detail_table(detail_rows or [])

        # Checkout only, since that's the only flow with an attach control.
        attachment_specs = _task_attachment_specs(tasks) if event == "checkout" else []

        html = f"""
        <div style="{EMAIL_WRAPPER_STYLE}">
          <div style="{EMAIL_HEADING_STYLE}">{html_escape(subject)}</div>
          {detail_table_html}
          <div style="{EMAIL_SUBHEADING_STYLE}">{task_label}</div>
          <div>
            {task_html}
          </div>
        </div>
        """

        sender, reply_to = _employee_mail_identity(employee_name, employee_display_name)
        frappe.sendmail(
            recipients=recipients,
            subject=subject,
            message=html,
            now=False,
            sender=sender,
            reply_to=reply_to,
            attachments=attachment_specs,
        )
    except Exception as e:
        frappe.log_error(
            f"Notification failed for {employee_name} ({event}): {e}",
            "ST Attendance Tracker"
        )


# ── Next working date ──────────────────────────────────────────────────────────

def _get_next_working_date(employee, from_date):
    holiday_list = frappe.db.get_value("Employee", employee, "holiday_list")
    holidays = set()
    if holiday_list:
        rows = frappe.get_all(
            "Holiday", filters={"parent": holiday_list}, pluck="holiday_date"
        )
        holidays = {getdate(h) for h in rows}

    candidate = getdate(add_days(from_date, 1))
    while True:
        if candidate.weekday() == 6:
            candidate = getdate(add_days(candidate, 1))
            continue
        if candidate in holidays:
            candidate = getdate(add_days(candidate, 1))
            continue
        break
    return candidate


def _resolve_active_checkin_date(employee_name):
    """Which date's check-in is still 'open' (checked in, no EOD submitted
    yet) — the date the checkin/checkout page and EOD submission should act
    on. Only looks back to yesterday: an unresolved check-in older than
    that is abandoned, not something to silently resume into. Without this
    bound, a months-old forgotten checkout (or resetting today's check-in
    and thereby un-masking one) would surface as if it were the active
    shift, hiding today's own tasks behind a stale date — this is the same
    "don't fabricate a real span past one legitimate day" rule already
    applied in resolve_zero_diff_minutes (time_utils.py).
    """
    latest = frappe.db.get_value("Daily Work Log", {
        "employee": employee_name,
        "morning_submitted": 1,
        "date": [">=", add_days(today(), -1)],
    }, ["date", "eod_submitted"], order_by="date desc", as_dict=True)

    if not latest:
        return today()
    return latest.date if not latest.eod_submitted else today()


# ── Rollover on EOD ────────────────────────────────────────────────────────────

def _rollover_pending_tasks(employee, date):
    # Lock the employee record to serialize EOD rollovers and prevent duplicate entries
    frappe.db.sql("select name from `tabEmployee` where name = %s for update", (employee,))

    next_date = _get_next_working_date(employee, date)

    work_log = _get_work_log(employee, date)
    if not work_log:
        return 0

    pending = [row for row in work_log.tasks
               if row.status in ("Pending", "In Progress") and row.task_type != "Recurring"]
    if not pending:
        return 0

    next_log = _get_or_new_work_log(employee, next_date)
    existing_series = {row.series_id for row in next_log.tasks}

    # Source-row status changes below go through frappe.db.set_value, not
    # work_log.save() — this source day's Daily Work Log may already be
    # eod_submitted=1 (that's exactly what triggered this rollover call),
    # and re-saving a locked parent through the ORM would hit its own lock
    # guard. Task Entry has no controller validation of its own to skip, so
    # a direct field write is safe here — the same pattern the old
    # standalone-Daily-Task model used for this exact "housekeeping, not a
    # user edit" case.
    rolled = 0
    for row in pending:
        if _is_series_done(row.series_id):
            if row.status != "Done":
                frappe.db.set_value("Task Entry", row.name, "status", "Done")
            continue

        if row.series_id in existing_series or str(row.origin_date) == str(next_date):
            continue

        new_row = next_log.append("tasks", {})
        new_row.series_id = row.series_id
        new_row.origin_date = row.origin_date or date
        new_row.description = row.description
        new_row.task_type = "Planned"
        new_row.status = "Pending"
        new_row.estimated_time = row.estimated_time or ""
        new_row.project_name = row.project_name or ""
        new_row.remarks = f"[Carried from {row.origin_date or date}]"
        new_row.sequence = _next_sequence(next_log) + 1
        existing_series.add(row.series_id)
        frappe.db.set_value("Task Entry", row.name, "status", "Rolled Over")
        rolled += 1

    if rolled:
        _save_work_log(next_log)
        frappe.db.commit()
    return rolled


# ── Safety rollover on page load ───────────────────────────────────────────────

def _safety_rollover(employee, today_date):
    """
    Catches tasks missed because employee didn't submit EOD.
    Runs silently on morning page load before check-in.
    """
    # Lock the employee record to serialize page-load safety rollovers and prevent duplicate entries
    frappe.db.sql("select name from `tabEmployee` where name = %s for update", (employee,))

    stale_log_names = frappe.get_all("Daily Work Log", filters={
        "employee": employee,
        "date": ["<", today_date],
    }, pluck="name")
    if not stale_log_names:
        return

    today_log = _get_or_new_work_log(employee, today_date)
    existing_series = {row.series_id for row in today_log.tasks}
    appended = False

    # Source-row status changes go through frappe.db.set_value, not
    # source.save() — a stale day can itself already be eod_submitted=1,
    # and re-saving a locked parent through the ORM would hit its own lock
    # guard. See the matching comment in _rollover_pending_tasks.
    for log_name in stale_log_names:
        source = frappe.get_doc("Daily Work Log", log_name)
        for row in source.tasks:
            if row.status not in ("Pending", "In Progress") or row.task_type == "Recurring":
                continue

            if _is_series_done(row.series_id):
                if row.status != "Done":
                    frappe.db.set_value("Task Entry", row.name, "status", "Done")
                continue

            if row.series_id in existing_series or str(row.origin_date) == str(today_date):
                continue

            new_row = today_log.append("tasks", {})
            new_row.series_id = row.series_id
            new_row.origin_date = row.origin_date or source.date
            new_row.description = row.description
            new_row.task_type = "Planned"
            new_row.status = "Pending"
            new_row.estimated_time = row.estimated_time or ""
            new_row.project_name = row.project_name or ""
            new_row.remarks = f"[Auto-carried from {row.origin_date or source.date}]"
            new_row.sequence = _next_sequence(today_log) + 1
            existing_series.add(row.series_id)
            frappe.db.set_value("Task Entry", row.name, "status", "Rolled Over")
            appended = True

    if appended:
        _save_work_log(today_log)

    frappe.db.commit()


# ── Recurring tasks (e.g. daily scrum) ─────────────────────────────────────────

def _parse_recurring_days(raw):
    """Empty/blank -> every day (empty list, caller treats as no restriction).
    Mirrors ST Attendance Settings.hybrid_office_days' storage convention:
    Small Text, one day name per line, Monday..Saturday, no Sunday."""
    day_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5}
    if not raw:
        return []
    return [day_map[d] for d in (line.strip().lower() for line in raw.strip().split("\n")) if d in day_map]


def _ensure_recurring_tasks(employee, date):
    """
    For every active Recurring Task Template belonging to this employee,
    make sure a Daily Task for today already exists; insert one if not.
    Idempotent — safe to call on every page load / check-in attempt.
    Recurring-type tasks never enter the carry-forward chain (see the
    task_type exclusion in _rollover_pending_tasks) — a fresh one appears
    here again tomorrow regardless of whether today's was completed.
    A template only fires on the weekdays in its recurring_days; blank
    recurring_days means every day (backward compatible with templates
    created before this field existed).
    """
    templates = frappe.get_all("Recurring Task Template",
        filters={"employee": employee, "is_active": 1},
        fields=["name", "description", "project_name", "estimated_time", "recurring_days"])

    weekday_num = getdate(date).weekday()
    due = []
    for tpl in templates:
        days = _parse_recurring_days(tpl.recurring_days)
        if days and weekday_num not in days:
            continue
        due.append(tpl)

    work_log = _get_or_new_work_log(employee, date)
    existing_by_template = {row.recurring_template: row for row in work_log.tasks
                             if row.task_type == "Recurring" and row.recurring_template}

    changed = False
    for tpl in due:
        row = existing_by_template.get(tpl.name)
        if row:
            # Template may have been edited since this instance was created —
            # keep an untouched (still Pending) instance in sync rather than
            # leaving a stale copy of the old description/project/estimate.
            if row.status == "Pending" and (
                row.description != tpl.description
                or row.project_name != (tpl.project_name or "")
                or row.estimated_time != (tpl.estimated_time or "")
            ):
                row.description = tpl.description
                row.project_name = tpl.project_name or ""
                row.estimated_time = tpl.estimated_time or ""
                changed = True
            continue

        row = work_log.append("tasks", {})
        row.series_id = frappe.generate_hash(length=32)
        row.origin_date = date
        row.description = tpl.description
        row.task_type = "Recurring"
        row.status = "Pending"
        row.estimated_time = tpl.estimated_time or ""
        row.project_name = tpl.project_name or ""
        row.recurring_template = tpl.name
        row.sequence = _next_sequence(work_log) + 1
        changed = True

    # A template that's since been deleted, deactivated, or edited to no
    # longer recur on this weekday shouldn't leave a stale instance behind —
    # but only touch it if the employee hasn't started it yet.
    due_names = {tpl.name for tpl in due}
    for template_name, row in existing_by_template.items():
        if template_name not in due_names and row.status == "Pending":
            work_log.tasks.remove(row)
            changed = True

    if changed:
        _save_work_log(work_log)
        frappe.db.commit()


# ── Recurring task self-service (employee-facing CRUD) ─────────────────────────
# Ownership/validation is enforced by the doctype's own validate()/on_trash()
# (RecurringTaskTemplate._check_ownership) — these wrappers don't re-check it,
# and deliberately don't pass ignore_permissions so Frappe's own if_owner
# doctype permission (already granted to the Employee role) applies too.

@frappe.whitelist()
def get_recurring_tasks():
    """List the current employee's Recurring Task Templates for self-service management."""
    employee = _get_employee()
    rows = frappe.get_all("Recurring Task Template",
        filters={"employee": employee.name},
        fields=["name", "description", "project_name", "estimated_time", "is_active", "recurring_days"],
        order_by="description asc")
    for r in rows:
        raw = (r.recurring_days or "").strip()
        r["days"] = [d.strip() for d in raw.split("\n") if d.strip()] if raw else []
    return rows


@frappe.whitelist()
def save_recurring_task(name=None, description="", project_name="", estimated_time="", recurring_days=None, is_active=1):
    """Create or update (upsert by `name`) a self-service Recurring Task Template.
    `recurring_days` arrives as a JSON-encoded list of day names (or a plain list)."""
    employee = _get_employee()
    if not (description or "").strip():
        frappe.throw("Task description cannot be empty.")

    days = json.loads(recurring_days) if isinstance(recurring_days, str) else (recurring_days or [])

    if name:
        doc = frappe.get_doc("Recurring Task Template", name)
        if doc.employee != employee.name:
            frappe.throw("Not authorised to edit this task.", frappe.PermissionError)
    else:
        doc = frappe.new_doc("Recurring Task Template")
        doc.employee = employee.name

    doc.description = description.strip()
    doc.project_name = (project_name or "").strip()
    doc.estimated_time = estimated_time or ""
    doc.recurring_days = "\n".join(days)
    doc.is_active = 1 if str(is_active) in ("1", "true", "True") else 0
    doc.save()
    # Sync today's not-yet-started instance immediately — covers a
    # description/estimate edit, a day-change that drops today, or
    # deactivation — rather than waiting for the employee's next
    # /daily-checkin page load.
    _ensure_recurring_tasks(employee.name, today())
    return {"success": True, "name": doc.name}


@frappe.whitelist()
def delete_recurring_task(name):
    employee = _get_employee()
    task_employee = frappe.db.get_value("Recurring Task Template", name, "employee")
    if task_employee != employee.name:
        frappe.throw("Not authorised to delete this task.", frappe.PermissionError)
    # force=True: historical Task Entry rows keep a soft `recurring_template`
    # reference for provenance (spec: sync, not a hard dependency) — without
    # this, Frappe's link-check would block deleting any template that's
    # ever produced a task.
    frappe.delete_doc("Recurring Task Template", name, force=True)
    # Sync today's not-yet-started instance immediately rather than waiting
    # for the employee's next /daily-checkin page load.
    _ensure_recurring_tasks(employee.name, today())
    return {"success": True}


# ── Additional work self-service (employee-facing CRUD) ────────────────────────
# Independent of Daily Task Log — never reopens or recalculates a submitted
# EOD log's net_hours/working_hours. Ownership/validation is enforced by the
# doctype's own validate()/on_trash() (AdditionalWork._check_ownership) —
# these wrappers don't re-check it, and deliberately don't pass
# ignore_permissions so Frappe's own if_owner doctype permission applies too.

ADDITIONAL_WORK_PAGE_SIZE = 15


@frappe.whitelist()
def get_additional_work(page=0):
    """Paginated list of the current employee's Additional Work entries,
    newest work_date first."""
    employee = _get_employee()
    page = int(page or 0)
    rows = frappe.get_all("Additional Work",
        filters={"employee": employee.name},
        fields=["name", "work_date", "project_name", "hours_spent", "description", "remarks", "login_time", "logout_time", "status"],
        order_by="work_date desc, creation desc",
        start=page * ADDITIONAL_WORK_PAGE_SIZE,
        page_length=ADDITIONAL_WORK_PAGE_SIZE + 1)

    has_more = len(rows) > ADDITIONAL_WORK_PAGE_SIZE
    rows = rows[:ADDITIONAL_WORK_PAGE_SIZE]
    total_hours = sum(r.hours_spent or 0 for r in rows)
    return {"entries": rows, "total_hours": total_hours, "has_more": has_more}


@frappe.whitelist()
def save_additional_work(name=None, work_date=None, project_name="", hours_spent="", description="", remarks="", login_time="", logout_time="", status=""):
    """Create or update (upsert by `name`) a self-service Additional Work entry."""
    employee = _get_employee()
    if not (description or "").strip():
        frappe.throw("Description cannot be empty.")
    if not work_date:
        frappe.throw("Work date is required.")

    if name:
        doc = frappe.get_doc("Additional Work", name)
        if doc.employee != employee.name:
            frappe.throw("Not authorised to edit this entry.", frappe.PermissionError)
    else:
        doc = frappe.new_doc("Additional Work")
        doc.employee = employee.name

    doc.work_date = work_date
    doc.project_name = (project_name or "").strip()
    # Raw text ("1h 30m") passed through as-is — Additional Work's own
    # validate() parses hours_spent exactly once on save; pre-parsing here
    # too would feed it an already-numeric value, which parse_duration_to_hours
    # treats as bare minutes (its documented convention for unit-less input)
    # and divides by 60.
    doc.hours_spent = hours_spent or ""
    doc.description = description.strip()
    doc.remarks = (remarks or "").strip()
    doc.login_time = login_time or ""
    doc.logout_time = logout_time or ""
    doc.status = status or "Done"
    doc.save()
    return {"success": True, "name": doc.name}


@frappe.whitelist()
def delete_additional_work(name):
    employee = _get_employee()
    entry_employee = frappe.db.get_value("Additional Work", name, "employee")
    if entry_employee != employee.name:
        frappe.throw("Not authorised to delete this entry.", frappe.PermissionError)
    frappe.delete_doc("Additional Work", name)
    return {"success": True}


# ── WFH validation ─────────────────────────────────────────────────────────────

def _get_hybrid_office_days():
    """Reads hybrid office days from ST Attendance Settings. Fully dynamic."""
    day_map = {
        "monday": 0, "tuesday": 1, "wednesday": 2,
        "thursday": 3, "friday": 4, "saturday": 5,
    }
    try:
        raw = _get_attendance_settings().get("hybrid_office_days") or "Tuesday\nThursday"
        days = []
        for line in raw.strip().split("\n"):
            d = line.strip().lower()
            if d in day_map:
                days.append(day_map[d])
        return days if days else [1, 3]
    except Exception:
        frappe.log_error(frappe.get_traceback(), "ST Attendance Tracker — hybrid office days lookup failed")
        return [1, 3]


def _wfh_request_exists(employee_name, date):
    """True if a draft/submitted Attendance Request (reason=WFH) covers this date."""
    return bool(frappe.db.exists("Attendance Request", {
        "employee": employee_name,
        "from_date": ["<=", date],
        "to_date":   [">=", date],
        "reason":    "Work From Home",
        "docstatus": ["in", [0, 1]],
    }))


@frappe.whitelist()
def validate_wfh_request():
    """
    Check if logged-in employee has a saved/submitted Attendance Request
    with reason = Work From Home for today.
    """
    employee = frappe.db.get_value(
        "Employee", {"user_id": frappe.session.user, "status": "Active"}, "name"
    )
    if not employee:
        return {"valid": False, "message": "Employee record not found."}

    date = today()
    weekday_num = getdate(date).weekday()

    if weekday_num == 5:
        return {"valid": True, "message": "Saturday — WFH approved for all."}

    work_type = frappe.db.get_value("Employee", employee, "work_type") or "Office"
    if work_type == "Hybrid":
        hybrid_office_days = _get_hybrid_office_days()
        if weekday_num not in hybrid_office_days:
            return {"valid": True, "message": "Hybrid routine WFH day — no Attendance Request needed."}

    exists = _wfh_request_exists(employee, date)

    if exists:
        return {"valid": True, "message": "WFH request found.", "doc": exists}

    link = (
        f"/app/attendance-request/new-attendance-request-1?"
        f"employee={employee}&from_date={date}&to_date={date}&reason=Work From Home"
    )

    return {
        "valid": False,
        "message": (
            "You have not applied for Work From Home today. "
            "Please submit an Attendance Request with reason 'Work From Home' "
            "before checking in as WFH."
        ),
        "link": link,
    }


# ── Page state ─────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_page_state():
    employee = _get_employee()
    date = today()

    work_log = _get_work_log(employee.name, date)
    morning_done = bool(work_log and work_log.morning_submitted)

    # Safety rollover — only before check-in
    if not morning_done:
        _safety_rollover(employee.name, date)
        _ensure_recurring_tasks(employee.name, date)
        work_log = _get_work_log(employee.name, date)
        morning_done = bool(work_log and work_log.morning_submitted)

    # Auto-heal checkin if HR deleted it
    if morning_done:
        checkin_today = frappe.db.exists("Employee Checkin", {
            "employee": employee.name,
            "log_type": "IN",
            "device_id": "ST Daily Checkin",
            "time": ["between", [
                frappe.utils.get_datetime(date + " 00:00:00"),
                frappe.utils.get_datetime(date + " 23:59:59"),
            ]],
        })
        if not checkin_today:
            _make_checkin(employee.name, "IN", work_log.login_time)

    eod_done = bool(work_log and work_log.eod_submitted)

    tasks = []
    if work_log:
        tasks = [_task_entry_dict(row, date) for row in work_log.tasks]
        for t in tasks:
            t["days_pending"] = (
                (getdate(date) - getdate(t["origin_date"])).days if t["is_carried"] else 0
            )

    login_time_val = work_log.login_time if morning_done else ""

    # Fetch active employee shift & leave status
    shift_info = None
    try:
        from hrms.hr.doctype.shift_assignment.shift_assignment import get_employee_shift
        shift_details = get_employee_shift(employee.name, consider_default_shift=True)
        if shift_details:
            shift_info = {
                "name": shift_details.shift_type.name,
                "start_time": _to_hhmm(shift_details.shift_type.start_time),
                "end_time": _to_hhmm(shift_details.shift_type.end_time),
                "start_time_ampm": _to_ampm(shift_details.shift_type.start_time),
                "end_time_ampm": _to_ampm(shift_details.shift_type.end_time),
            }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "ST Attendance Tracker — shift lookup failed")

    leave_today = None
    try:
        leave_today = frappe.db.get_value("Leave Application", {
            "employee": employee.name,
            "from_date": ["<=", date],
            "to_date": [">=", date],
            "status": "Approved",
            "docstatus": 1
        }, "leave_type")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "ST Attendance Tracker — leave lookup failed")

    has_reset_today = bool(work_log and work_log.was_reset_today)

    return {
        "employee":       employee,
        "date":           date,
        "morning_done":   morning_done,
        "eod_done":       eod_done,
        "tasks":          tasks,
        "current_time":   now_datetime().strftime("%H:%M"),
        # FIX: use _to_ampm so single-digit-hour timedeltas (e.g. 9:30:00)
        # don't produce a trailing colon after [:5] slicing
        "login_time":     _to_ampm(login_time_val),
        "is_team_leader": _is_team_leader(employee.name),
        "shift_info":     shift_info,
        "leave_today":    leave_today,
        "has_reset_today": has_reset_today,
    }


# ── Notification jobs (run off the request path) ────────────────────────────────
# Building these emails means base64-encoding every attached image/zip/PDF into
# an Email Queue record — with a multi-megabyte checkout attachment that alone
# can take several seconds, which used to happen twice (HR/TL email + employee
# email) inside the same synchronous check-in/checkout HTTP request. Both
# submit_morning_log and submit_eod_log now enqueue these instead of calling
# them directly, so the employee's checkout finishes as soon as their data is
# saved — the emails still go out, just from a background worker a moment
# later. enqueue_after_commit=True guarantees the worker never reads the
# just-saved Daily Work Log before this request's transaction is durable.

def _send_checkin_notifications(employee_name, date, checkin_action_time):
    employee = frappe.db.get_value("Employee", employee_name,
        ["name", "employee_name", "department"], as_dict=True)
    work_log = _get_work_log(employee_name, date)
    if not employee or not work_log:
        return

    all_tasks = [_task_entry_dict(row, date) for row in work_log.tasks]
    detail_rows = [
        ("Employee", employee.employee_name),
        ("Date", _format_email_date(date)),
        ("Login Time", _to_ampm(work_log.login_time)),
        ("Checked-In At", _format_action_timestamp(checkin_action_time)),
        ("Work Location", work_log.work_location),
        ("Half-Day Session", work_log.half_day_session),
        ("Status", "Late Check-in" if work_log.is_late else None),
    ]
    _notify_hr_and_team_leader(
        employee.name, employee.employee_name, "checkin",
        detail_rows, tasks=all_tasks, department=employee.department,
    )
    _send_employee_checkin_email(
        employee.name, employee.employee_name, work_log.login_time, checkin_action_time,
        work_log.work_location, work_log.half_day_session, work_log.is_late, all_tasks, date,
        department=employee.department,
    )


def _send_eod_notifications(employee_name, date, checkout_action_time, is_late_checkout):
    employee = frappe.db.get_value("Employee", employee_name,
        ["name", "employee_name", "department"], as_dict=True)
    work_log = _get_work_log(employee_name, date)
    if not employee or not work_log:
        return

    all_tasks = [_task_entry_dict(row, date) for row in work_log.tasks]
    _attach_task_files(all_tasks)

    done_count = sum(1 for t in all_tasks if t.status == "Done")
    total_count = len(all_tasks)
    total_actual_hours = sum(float(t.get("actual_time") or 0.0) for t in all_tasks)
    working_hours_str = _format_hours(total_actual_hours) or "0h"

    detail_rows = [
        ("Employee", employee.employee_name),
        ("Date", _format_email_date(date)),
        ("Login Time", _to_ampm(work_log.login_time) if work_log.login_time else None),
        ("Logout Time", _to_ampm(work_log.logout_time)),
        ("Checked-Out At", _format_action_timestamp(checkout_action_time)),
        ("Work Location", work_log.work_location),
        ("Net Working Hours", work_log.net_hours),
        ("Total Task Hours", working_hours_str),
        ("Half-Day Session", work_log.half_day_session),
        ("Tasks Completed", f"{done_count}/{total_count}"),
    ]
    _notify_hr_and_team_leader(
        employee.name, employee.employee_name, "checkout",
        detail_rows, tasks=all_tasks, department=employee.department,
    )
    _send_employee_eod_email(
        employee.name, employee.employee_name, work_log.logout_time, checkout_action_time,
        work_log.net_hours, work_log.work_location, work_log.half_day_session, all_tasks, date,
        is_late_checkout=is_late_checkout, submission_date=today(),
        department=employee.department,
    )


# ── Morning submit ─────────────────────────────────────────────────────────────

@frappe.whitelist()
def submit_morning_log(new_tasks, login_time=None, carried_updates=None, work_location=None, half_day_session=None):
    """
    new_tasks:        JSON list of {description, estimated_time}
    login_time:       optional HH:MM override
    carried_updates:  JSON list of {name, description, estimated_time} edits
    half_day_session: "First Half"/"Second Half", only kept if employee is
                      actually on approved half-day leave today
    """
    checkin_action_time = now_datetime()
    employee = _get_employee()
    # Lock employee record to serialize check-in processing
    frappe.db.sql("select name from `tabEmployee` where name = %s for update", (employee.name,))

    date = today()

    # Safety rollover to catch and carry forward any pending tasks from previous days
    _safety_rollover(employee.name, date)
    _ensure_recurring_tasks(employee.name, date)

    work_log = _get_or_new_work_log(employee.name, date)
    if work_log.morning_submitted:
        frappe.throw("You have already checked in today.")

    # ── WFH Validation ──────────────────────────────────────────────────
    work_location = (work_location or "Office").strip()
    weekday_num = getdate(date).weekday()
    is_saturday = weekday_num == 5

    work_type = frappe.db.get_value("Employee", employee.name, "work_type") or "Office"
    is_hybrid_routine_day = False
    if work_type == "Hybrid" and not is_saturday:
        hybrid_office_days = _get_hybrid_office_days()
        is_hybrid_routine_day = weekday_num not in hybrid_office_days

    if work_location == "WFH" and not is_saturday and not is_hybrid_routine_day:
        wfh_exists = frappe.db.exists("Attendance Request", {
            "employee": employee.name,
            "from_date": ["<=", date],
            "to_date":   [">=", date],
            "reason":    "Work From Home",
            "docstatus": ["in", [0, 1]],
        })
        if not wfh_exists:
            frappe.throw(
                "You have not applied for Work From Home today. "
                "Please submit an Attendance Request before checking in as WFH.",
                title="WFH Not Applied"
            )

    tasks   = json.loads(new_tasks)       if isinstance(new_tasks, str)       else (new_tasks or [])
    carried = json.loads(carried_updates) if isinstance(carried_updates, str) else (carried_updates or [])

    # Count total tasks (new + already in DB from rollover)
    carried_count = len(work_log.tasks)

    if not tasks and carried_count == 0:
        frappe.throw("Please add at least one planned task before checking in.")

    # Update carried task descriptions/estimates if employee edited them
    rows_by_name = {row.name: row for row in work_log.tasks if row.name}
    for c in carried:
        task_name = c.get("name")
        if not task_name:
            continue
        row = _resolve_task_row(rows_by_name, task_name, work_log, employee.name, "submit_morning_log")
        if (c.get("description") or "").strip():
            row.description = c["description"].strip()
        if c.get("estimated_time") is not None:
            # Raw text ("2h 15m") is passed through as-is — Daily Work Log's
            # own validate()/_prepare_tasks() parses every row's
            # estimated_time/actual_time exactly once on save. Parsing here
            # too would feed that second pass an already-numeric value,
            # which parse_duration_to_hours treats as bare minutes (its
            # documented convention for unit-less input) and divides by 60.
            row.estimated_time = c.get("estimated_time", "")
        if c.get("project_name") is not None:
            row.project_name = c.get("project_name", "").strip()

    # Insert new tasks, preserving the order the employee entered them in
    sequence_base = _next_sequence(work_log)
    new_rows_with_attachments = []
    for i, t in enumerate(tasks):
        desc = (t.get("description") or "").strip()
        if not desc:
            continue
        row = work_log.append("tasks", {})
        row.series_id = frappe.generate_hash(length=32)
        row.origin_date = date
        row.description = desc
        row.task_type = "Planned"
        row.status = "Pending"
        row.estimated_time = t.get("estimated_time", "")
        row.project_name = (t.get("project_name") or "").strip()
        row.sequence = sequence_base + i + 1
        new_rows_with_attachments.append((row, t.get("attachment_names")))

    actual_login = login_time or now_datetime().strftime("%H:%M:%S")
    if re.fullmatch(r"\d{2}:\d{2}", str(actual_login)):
        actual_login = str(actual_login) + ":00"

    work_log.login_time = actual_login
    work_log.work_location = work_location
    work_log.half_day_session = _resolve_half_day_session(employee.name, date, half_day_session)
    work_log.morning_submitted = 1
    _save_work_log(work_log)

    for row, attachment_names in new_rows_with_attachments:
        _reparent_attachments(attachment_names, row.name)

    _make_checkin(employee.name, "IN", actual_login)
    frappe.db.commit()

    frappe.enqueue(
        _send_checkin_notifications,
        queue="short",
        enqueue_after_commit=True,
        employee_name=employee.name,
        date=date,
        checkin_action_time=checkin_action_time,
    )

    return {"success": True, "login_time": _to_ampm(actual_login)}


# ── EOD submit ─────────────────────────────────────────────────────────────────

@frappe.whitelist()
def submit_eod_log(lunch_from, lunch_to, logout_time, task_updates, adhoc_tasks):
    checkout_action_time = now_datetime()
    employee = _get_employee()
    # Lock employee record to serialize checkout/EOD processing
    frappe.db.sql("select name from `tabEmployee` where name = %s for update", (employee.name,))

    date = _resolve_active_checkin_date(employee.name)
    is_late_checkout = str(date) != str(today())

    if not isinstance(date, str):
        date = frappe.utils.getdate(date).strftime("%Y-%m-%d")

    if not frappe.flags.in_test:
        logout_time = now_datetime().strftime("%H:%M:%S")
    elif not logout_time:
        frappe.throw("Logout time is required.")

    work_log = _get_or_new_work_log(employee.name, date)
    if work_log.eod_submitted:
        frappe.throw(f"You have already checked out for {date}.")

    updates = json.loads(task_updates) if isinstance(task_updates, str) else task_updates
    adhocs  = json.loads(adhoc_tasks)  if isinstance(adhoc_tasks, str)  else adhoc_tasks

    # Update existing task statuses + actual time
    rows_by_name = {row.name: row for row in work_log.tasks if row.name}
    newly_done_series = []
    for t in updates:
        name = t.get("name")
        if not name:
            continue
        row = _resolve_task_row(rows_by_name, name, work_log, employee.name, "submit_eod_log")
        status = t.get("status", "Pending")
        actual_time = t.get("actual_time", "")
        if status == "Done" and not _parse_time_to_hours(actual_time):
            frappe.throw(f"Please provide time taken for task completion for the completed task: '{t.get('description') or name}'")
        # Entering time taken without touching the status dropdown means
        # they've started it — reflect that instead of leaving it Pending.
        if status == "Pending" and _parse_time_to_hours(actual_time):
            status = "In Progress"
        # Employee explicitly declined to carry an unfinished task forward —
        # drop it instead of letting _rollover_pending_tasks pick it up.
        if status in ("Pending", "In Progress") and not t.get("carry_forward", True):
            status = "Dropped"

        row.status = status
        # Raw text passed through as-is — see the matching comment above on
        # the carried-task estimated_time assignment: Daily Work Log's own
        # validate() parses every row's actual_time exactly once on save,
        # and pre-parsing here too would double it down to bare minutes.
        row.actual_time = actual_time
        row.remarks = t.get("remarks", "")
        # Allow description edit during EOD
        if (t.get("description") or "").strip():
            row.description = t["description"].strip()

        if status == "Done":
            newly_done_series.append(row.series_id)

    # Insert ad-hoc tasks, preserving the order the employee entered them in
    sequence_base = _next_sequence(work_log)
    new_rows_with_attachments = []
    for i, t in enumerate(adhocs):
        desc = (t.get("description") or "").strip()
        if not desc:
            continue
        row = work_log.append("tasks", {})
        row.series_id = frappe.generate_hash(length=32)
        row.origin_date = date
        row.description = desc
        row.task_type = "Ad-hoc"
        row.status = t.get("status", "Done")
        row.estimated_time = t.get("estimated_time", "")
        row.actual_time = t.get("actual_time", "")
        row.remarks = t.get("remarks", "")
        row.project_name = (t.get("project_name") or "").strip()
        row.sequence = sequence_base + i + 1
        new_rows_with_attachments.append((row, t.get("attachment_names")))

    login_time_raw = work_log.login_time or ""
    half_day_session = work_log.half_day_session or ""
    work_location = work_log.work_location or ""

    # net_hours is (re)computed by DailyWorkLog.validate() on save — no need
    # to pre-compute it here (the old two-doc model did, but its result was
    # always immediately overwritten by the log doc's own validate() anyway).
    work_log.lunch_from = lunch_from or ""
    work_log.lunch_to = lunch_to or ""
    work_log.logout_time = logout_time
    work_log.eod_submitted = 1

    _save_work_log(work_log)
    net_hours = work_log.net_hours or ""

    for row, attachment_names in new_rows_with_attachments:
        _reparent_attachments(attachment_names, row.name)

    for series_id in newly_done_series:
        _cascade_series_done(series_id, date)

    _make_checkin(employee.name, "OUT", logout_time)
    pending_count = _rollover_pending_tasks(employee.name, date)
    frappe.db.commit()

    frappe.enqueue(
        _send_eod_notifications,
        queue="short",
        enqueue_after_commit=True,
        employee_name=employee.name,
        date=date,
        checkout_action_time=checkout_action_time,
        is_late_checkout=is_late_checkout,
    )

    return {
        "success":       True,
        "pending_count": pending_count,
        "net_hours":     net_hours,
        "logout_time":   _to_ampm(logout_time),
    }


# ── Team dashboard ─────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_team_dashboard(date=None):
    """Team Leader sees only their direct reports (reports_to = current employee)."""
    employee = _get_employee()
    date = date or today()

    team_names = _get_team_members(employee.name)
    if not team_names:
        frappe.throw("Access denied. You are not a Team Leader.", frappe.PermissionError)

    team = frappe.get_all("Employee", filters={
        "name": ["in", team_names],
    }, fields=["name", "employee_name", "department", "designation", "user_id"])

    return _build_team_data(team, date)


# ── Management dashboard ───────────────────────────────────────────────────────

@frappe.whitelist()
def get_management_dashboard(date=None):
    """HR Manager sees all departments with all employees."""
    if not ("HR Manager" in frappe.get_roles(frappe.session.user)):
        frappe.throw("Access denied. HR Manager role required.", frappe.PermissionError)

    date = date or today()

    departments = frappe.get_all(
        "Department", filters={"is_group": 0}, pluck="name", order_by="name asc"
    )

    result = []
    for dept in departments:
        employees = frappe.get_all("Employee", filters={
            "department": dept, "status": "Active",
        }, fields=["name", "employee_name", "department", "designation", "user_id"])

        if not employees:
            continue

        dept_data = _build_team_data(employees, date)
        dept_data["department"] = dept
        result.append(dept_data)

    all_employees = frappe.get_all("Employee",
        filters={"status": "Active"},
        fields=["name", "employee_name", "department"],
    )
    checked_in = frappe.get_all("Daily Work Log", filters={
        "date": date, "morning_submitted": 1,
    }, pluck="employee")
    eod_done = frappe.get_all("Daily Work Log", filters={
        "date": date, "eod_submitted": 1,
    }, pluck="employee")
    on_leave = frappe.get_all("Leave Application", filters={
        "from_date": ["<=", date], "to_date": [">=", date],
        "status": "Approved", "docstatus": 1,
    }, pluck="employee")

    # Calculate rankings based on completed EOD logs for this date
    def _parse_time_to_minutes(time_str):
        if not time_str:
            return 0
        s = str(time_str).strip().lower()
        if 'h' in s or 'm' in s:
            h = m = 0
            try:
                if 'h' in s:
                    parts = s.split('h')
                    h = int(float(parts[0].strip()))
                    s = parts[1]
                if 'm' in s:
                    parts = s.split('m')
                    m = int(float(parts[0].strip()))
                return h * 60 + m
            except Exception:
                pass
        hhmm = _to_hhmm(time_str)
        if not hhmm:
            return 0
        try:
            parts = hhmm.split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            return 0

    eod_logs = frappe.db.get_all("Daily Work Log", filters={
        "date": date,
        "eod_submitted": 1,
    }, fields=["employee", "employee_name", "net_hours", "logout_time"])

    checkin_logs = frappe.db.get_all("Daily Work Log", filters={
        "date": date,
        "morning_submitted": 1,
    }, fields=["employee", "login_time"])

    login_map = {c.employee: c.login_time for c in checkin_logs}

    half_day_leaves = frappe.db.get_all("Leave Application", filters={
        "from_date": ["<=", date], "to_date": [">=", date],
        "status": "Approved", "half_day": 1, "docstatus": 1
    }, pluck="employee")

    rankings = []
    for log in eod_logs:
        net_min = _parse_time_to_minutes(log.net_hours)
        login_val = login_map.get(log.employee)
        rankings.append({
            "employee": log.employee,
            "employee_name": log.employee_name,
            "net_hours": log.net_hours or "00:00",
            "net_minutes": net_min,
            "login_time": _to_ampm(login_val) if login_val else "—",
            "logout_time": _to_ampm(log.logout_time) if log.logout_time else "—",
            "is_half_day": log.employee in half_day_leaves
        })

    # Sort rankings descending by net minutes
    rankings.sort(key=lambda x: x["net_minutes"], reverse=True)

    standard_workday_hours = _get_attendance_settings().get("standard_workday_hours") or 8

    return {
        "departments": result,
        "date": date,
        "rankings": rankings,
        "standard_workday_minutes": int(standard_workday_hours * 60),
        "summary": {
            "total":      len(all_employees),
            "checked_in": len(set(checked_in)),
            "eod_done":   len(set(eod_done)),
            "on_leave":   len(set(on_leave)),
            "missing":    len([e for e in all_employees
                               if e.name not in checked_in
                               and e.name not in on_leave]),
        }
    }


@frappe.whitelist()
def get_employee_task_detail(employee_name, date=None):
    """Full task detail for one employee. HR Manager or their Team Leader."""
    if not ("HR Manager" in frappe.get_roles(frappe.session.user)):
        current_emp = _get_employee()
        if employee_name not in _get_team_members(current_emp.name):
            frappe.throw("Access denied.", frappe.PermissionError)

    date = date or today()

    emp = frappe.db.get_value("Employee", employee_name,
        ["name", "employee_name", "department", "designation"], as_dict=True)

    work_log = _get_work_log(employee_name, date)

    morning_log = None
    eod_log = None
    tasks = []
    if work_log:
        if work_log.morning_submitted:
            morning_log = frappe._dict({
                "name": work_log.name, "login_time": work_log.login_time, "is_late": work_log.is_late,
            })
        if work_log.eod_submitted:
            eod_log = frappe._dict({
                "name": work_log.name, "logout_time": work_log.logout_time,
                "lunch_from": work_log.lunch_from, "lunch_to": work_log.lunch_to,
                "net_hours": work_log.net_hours,
            })
        tasks = [_task_entry_dict(row, date) for row in work_log.tasks]
        _attach_task_files(tasks)

    return {
        "employee":    emp,
        "date":        date,
        "morning_log": morning_log,
        "eod_log":     eod_log,
        "tasks":       tasks,
    }


# ── Employee history ───────────────────────────────────────────────────────────

@frappe.whitelist()
def get_my_history(page=0):
    """Past attendance summary for logged-in employee. 15 records per page."""
    employee = _get_employee()
    page   = int(page)
    limit  = 15
    offset = page * limit

    logs = frappe.get_all("Daily Work Log", filters={
        "employee": employee.name,
        "morning_submitted": 1,
        "date": ["<=", today()],
    }, fields=["date", "login_time", "logout_time", "net_hours",
               "lunch_from", "lunch_to", "is_late", "eod_submitted"],
        order_by="date desc", start=offset, page_length=limit)

    for log in logs:
        log["eod_done"] = 1 if log.get("eod_submitted") else 0

    dates = [log.date for log in logs]
    counts_by_date = {}
    if dates:
        rows = frappe.db.sql("""
            SELECT dwl.date as task_date,
                COUNT(*) as total,
                SUM(CASE WHEN te.status = 'Done' THEN 1 ELSE 0 END) as done
            FROM `tabTask Entry` te
            INNER JOIN `tabDaily Work Log` dwl ON dwl.name = te.parent
            WHERE dwl.employee = %(emp)s AND dwl.date IN %(dates)s
            GROUP BY dwl.date
        """, {"emp": employee.name, "dates": dates}, as_dict=True)
        counts_by_date = {row.task_date: row for row in rows}

    for log in logs:
        counts = counts_by_date.get(log.date)
        log["total_tasks"] = counts.total if counts else 0
        log["done_tasks"]  = counts.done  if counts else 0

    return {
        "logs":     logs,
        "employee": employee,
        "has_more": len(logs) == limit,
    }


@frappe.whitelist()
def get_history_day_detail(date):
    """Full task list for a specific past date."""
    employee = _get_employee()

    work_log = _get_work_log(employee.name, date)

    tasks = []
    morning_log = None
    eod_log = None
    if work_log:
        tasks = [_task_entry_dict(row, date) for row in work_log.tasks]
        if work_log.morning_submitted:
            morning_log = frappe._dict({"login_time": work_log.login_time, "is_late": work_log.is_late})
        if work_log.eod_submitted:
            eod_log = frappe._dict({
                "logout_time": work_log.logout_time, "lunch_from": work_log.lunch_from,
                "lunch_to": work_log.lunch_to, "net_hours": work_log.net_hours,
            })

    return {
        "date":        date,
        "tasks":       tasks,
        "morning_log": morning_log,
        "eod_log":     eod_log,
    }


@frappe.whitelist()
def delete_carried_task(name):
    """Delete a carried task. Employee can only delete their own tasks."""
    employee = _get_employee()
    parent = frappe.db.get_value("Task Entry", name, "parent")
    if not parent:
        frappe.throw("Not authorised to delete this task.", frappe.PermissionError)

    work_log = frappe.get_doc("Daily Work Log", parent)
    if work_log.employee != employee.name:
        frappe.throw("Not authorised to delete this task.", frappe.PermissionError)
    if work_log.eod_submitted:
        frappe.throw("Cannot delete tasks after checkout is submitted.", frappe.ValidationError)

    row = next((r for r in work_log.tasks if r.name == name), None)
    if not row:
        frappe.throw("Not authorised to delete this task.", frappe.PermissionError)

    # Clean up the lineage to prevent safety/EOD rollover from resurrecting it
    frappe.db.sql("""
        UPDATE `tabTask Entry`
        SET status = 'Rolled Over'
        WHERE series_id = %s AND status IN ('Pending', 'In Progress')
    """, (row.series_id,))

    work_log.tasks.remove(row)
    _save_work_log(work_log)
    frappe.db.commit()
    return {"success": True}


@frappe.whitelist(methods=["POST"])
def upload_task_attachment(task_name):
    """Attach an uploaded file to an existing task. Employee can only attach
    to their own tasks.

    Bypasses Frappe's generic upload_file endpoint on purpose (confirmed via
    a live 403 during checkout, not assumed): that endpoint's
    check_write_permission() loads Task Entry standalone via
    frappe.get_doc(doctype, name), then calls doc.check_permission(), which
    delegates a child doctype's permission check to its parent via
    has_child_permission(). That delegation reads
    `getattr(child_doc, "parent_doc", child_doc.parent)` to find the parent
    — but every Document/BaseDocument has a `parent_doc` *property*
    (base_document.py) that exists (and returns None) on a standalone-loaded
    child, so getattr never falls through to the intended `.parent` name
    string. The parent doc permission check then runs with no document
    context at all, so an if_owner-gated permission (ours) can never
    resolve to "yes, this employee owns it" — write is always denied. This
    is a core Frappe limitation for if_owner-restricted child doctypes
    accessed this way, not something fixable via DocType permission config.
    `frappe.client.set_value` (used for inline description/remarks edits)
    is unaffected — it resolves the parent doc explicitly before calling
    save(), never hitting this code path.
    """
    employee = _get_employee()
    _assert_task_owner(task_name, employee.name)

    file_obj = frappe.request.files.get("file")
    if not file_obj:
        frappe.throw("No file uploaded.")

    doc = frappe.get_doc({
        "doctype": "File",
        "attached_to_doctype": "Task Entry",
        "attached_to_name": task_name,
        "folder": "Home",
        "file_name": file_obj.filename,
        "is_private": cint(frappe.form_dict.get("is_private", 1)),
        "content": file_obj.stream.read(),
    })
    doc.insert(ignore_permissions=True)
    return doc


@frappe.whitelist()
def get_task_attachments(task_name):
    """List files attached to a task. Visible to the owner, their Team Leader, or HR Manager."""
    _assert_task_visible(task_name)
    return get_attachments("Task Entry", task_name)


@frappe.whitelist()
def view_task_attachment(file_name, task_name):
    """Stream a task's attached file. Visible to the owner, their Team Leader, or HR Manager.

    Bypasses Frappe's private-file route on purpose: that route resolves
    access via the Task Entry doctype's permission table (empty — child
    doctypes delegate to their parent), which has no row for Team Lead
    (and can't be given one without granting every Team Lead blanket read
    access to every employee's tasks — has_permission hooks can only deny,
    never grant, beyond that table). This endpoint does the same precise
    "is this employee my report" check _assert_task_visible already does
    elsewhere, then serves the file directly.
    """
    _assert_task_visible(task_name)
    if frappe.db.get_value("File", file_name, "attached_to_name") != task_name:
        frappe.throw("Not authorised to view this file.", frappe.PermissionError)
    file_doc = frappe.get_doc("File", file_name)
    frappe.local.response.filename = file_doc.file_name
    frappe.local.response.filecontent = file_doc.get_content()
    frappe.local.response.type = "download"


@frappe.whitelist(methods=["POST"])
def delete_task_attachment(file_name, task_name):
    """Delete a task's attached file. Employee can only delete their own task's files."""
    employee = _get_employee()
    _assert_task_owner(task_name, employee.name)
    if frappe.db.get_value("File", file_name, "attached_to_name") != task_name:
        frappe.throw("Not authorised to delete this file.", frappe.PermissionError)
    frappe.delete_doc("File", file_name, ignore_permissions=True)
    return {"success": True}


@frappe.whitelist()
def delete_carried_project(project_name, task_date):
    """Delete all tasks belonging to a project for a specific date."""
    employee = _get_employee()

    work_log = _get_work_log(employee.name, task_date)
    if not work_log:
        return {"success": True}
    if work_log.eod_submitted:
        frappe.throw("Cannot delete project tasks after checkout is submitted.", frappe.ValidationError)

    matching = [r for r in work_log.tasks if (r.project_name or "") == project_name]
    if not matching:
        return {"success": True}

    for row in matching:
        # Clean up the lineage to prevent safety/EOD rollover from resurrecting it
        frappe.db.sql("""
            UPDATE `tabTask Entry`
            SET status = 'Rolled Over'
            WHERE series_id = %s AND status IN ('Pending', 'In Progress')
        """, (row.series_id,))
        work_log.tasks.remove(row)

    _save_work_log(work_log)
    frappe.db.commit()
    return {"success": True}


@frappe.whitelist()
def reset_morning_checkin():
    """Reset morning check-in: revert today's Daily Work Log to its
    pre-checkin state and preserve its Task Entry rows as Pending for
    re-check-in (replaces cancelling a separate Morning Check-In doc under
    the old submit/cancel model — there's only one doc per day now)."""
    employee = _get_employee()

    date = _resolve_active_checkin_date(employee.name)
    if not isinstance(date, str):
        date = frappe.utils.getdate(date).strftime("%Y-%m-%d")

    work_log = _get_work_log(employee.name, date)
    if not work_log or not work_log.morning_submitted:
        frappe.throw(f"You have not checked in on {date}.")
    if work_log.eod_submitted:
        frappe.throw(f"Cannot reset check-in because checkout has already been submitted for {date}.")

    # Delete Employee Checkin created by ST Daily Checkin today
    frappe.db.delete("Employee Checkin", {
        "employee": employee.name,
        "log_type": "IN",
        "device_id": "ST Daily Checkin",
        "time": ["between", [date + " 00:00:00", date + " 23:59:59"]],
    })

    # Revert tasks back to Pending so they are preserved for re-check-in.
    # 'Rolled Over' tasks are excluded: they already have a forward copy on
    # a later date, so reviving them here would leave two live copies.
    for row in work_log.tasks:
        if row.status not in ("Pending", "Rolled Over"):
            row.status = "Pending"

    work_log.morning_submitted = 0
    work_log.login_time = None
    work_log.work_location = None
    work_log.half_day_session = None
    work_log.is_late = 0
    work_log.was_reset_today = 1

    _save_work_log(work_log)
    frappe.db.commit()
    return {"success": True}


@frappe.whitelist()
def update_half_day_session(session):
    """
    Retroactively apply a half-day session to an already-submitted Morning
    Check-In — for an employee who checked in normally, then got a half-day
    leave approved later the same day. Reuses the same eligibility check
    as check-in time (_resolve_half_day_session); the page reload after this
    re-derives shift truncation and recommended logout time from the newly
    stored value.
    """
    employee = _get_employee()
    date = today()

    work_log = _get_work_log(employee.name, date)
    if not work_log or not work_log.morning_submitted:
        frappe.throw("You have not checked in today.")
    if work_log.eod_submitted:
        frappe.throw("Cannot change half-day session after checkout is submitted.")

    resolved = _resolve_half_day_session(employee.name, date, session)
    if not resolved:
        frappe.throw("You do not have an approved half-day leave for today.")

    work_log.half_day_session = resolved
    _save_work_log(work_log)
    frappe.db.commit()
    return {"success": True, "half_day_session": resolved}


# ── Shared builder ─────────────────────────────────────────────────────────────

def _build_team_data(employees, date):
    """Build attendance + task data for a list of employees on a given date."""
    if not employees:
        return {"employees": [], "summary": {}}

    emp_names = [e.name for e in employees]

    work_logs = frappe.get_all("Daily Work Log", filters={
        "employee": ["in", emp_names], "date": date,
    }, fields=["name", "employee", "login_time", "is_late", "logout_time",
               "net_hours", "morning_submitted", "eod_submitted"])
    work_log_by_emp = {w.employee: w for w in work_logs}
    parent_to_emp = {w.name: w.employee for w in work_logs}

    on_leave = set(frappe.get_all("Leave Application", filters={
        "employee": ["in", emp_names],
        "from_date": ["<=", date],
        "to_date":   [">=", date],
        "status":    "Approved",
        "docstatus": 1,
    }, pluck="employee"))

    all_tasks = []
    if parent_to_emp:
        raw_tasks = frappe.get_all("Task Entry", filters={
            "parent": ["in", list(parent_to_emp.keys())],
        }, fields=["name", "parent", "description", "status", "project_name",
                   "task_type", "estimated_time", "actual_time", "origin_date"])
        for t in raw_tasks:
            entry = _task_entry_dict(t, date)
            entry["employee"] = parent_to_emp.get(t.parent)
            all_tasks.append(entry)
    _attach_task_files(all_tasks)

    tasks_by_emp = {}
    for t in all_tasks:
        tasks_by_emp.setdefault(t["employee"], []).append(t)

    result_employees = []
    for emp in employees:
        work_log = work_log_by_emp.get(emp.name)
        morning = work_log if work_log and work_log.morning_submitted else None
        eod     = work_log if work_log and work_log.eod_submitted else None
        tasks   = tasks_by_emp.get(emp.name, [])
        done    = sum(1 for t in tasks if t["status"] == "Done")

        # Real attendance data always takes priority over a leave record —
        # an employee on approved half-day leave who checks in for the other
        # half must still show their actual check-in/checkout, not "On leave".
        if eod:
            status = "eod_done"
        elif morning:
            status = "late" if morning.is_late else "checked_in"
        elif emp.name in on_leave:
            status = "leave"
        else:
            status = "missing"

        result_employees.append({
            "name":          emp.name,
            "employee_name": emp.employee_name,
            "department":    emp.department,
            "designation":   emp.get("designation", ""),
            "status":        status,
            # FIX: _to_hhmm prevents trailing-colon from single-digit-hour timedeltas
            "login_time":    _to_ampm(morning.login_time)  if morning and morning.login_time  else "",
            "logout_time":   _to_ampm(eod.logout_time)     if eod     and eod.logout_time     else "",
            "net_hours":     eod.net_hours                  if eod                              else "",
            "is_late":       bool(morning.is_late)          if morning                          else False,
            "tasks":         tasks,
            "done_tasks":    done,
            "total_tasks":   len(tasks),
        })

    checked_in_count = sum(
        1 for e in result_employees
        if e["status"] in ["checked_in", "late", "eod_done"]
    )

    return {
        "employees": result_employees,
        "date": date,
        "summary": {
            "total":      len(employees),
            "checked_in": checked_in_count,
            "missing":    sum(1 for e in result_employees if e["status"] == "missing"),
            "on_leave":   sum(1 for e in result_employees if e["status"] == "leave"),
            "late":       sum(1 for e in result_employees if e["status"] == "late"),
            "eod_done":   sum(1 for w in work_logs if w.eod_submitted),
        }
    }


@frappe.whitelist()
def assign_task_via_agent(assignee_employee, description, assigned_by_employee, project_name=None, estimated_time=None):
    """
    Intended for the Hermes Agent service user only.
    Allows an AI agent to assign a task to an employee's Daily Work Log on behalf of a team leader (via WhatsApp).
    Always re-verifies that `assignee_employee` is actually a team member of `assigned_by_employee` before assigning,
    as the caller agent is acting as a proxy.
    """
    if "ST Task Assignment Agent" not in frappe.get_roles(frappe.session.user):
        frappe.throw("Not authorised to use the agent task assignment API.", frappe.PermissionError)

    # Re-verify team membership independently of the caller's role, as the agent
    # acts on behalf of the `assigned_by_employee` WhatsApp sender.
    team_members = _get_team_members(assigned_by_employee)
    if assignee_employee not in team_members:
        frappe.throw(f"{assignee_employee} is not a direct report of {assigned_by_employee}.", frappe.PermissionError)

    description = (description or "").strip()
    if not description:
        frappe.throw("Task description cannot be empty.", frappe.ValidationError)

    date = today()
    log = _get_or_new_work_log(assignee_employee, date)
    
    log.append("tasks", {
        "task_type": "Ad-hoc",
        "status": "Pending",
        "description": description,
        "project_name": project_name,
        # Raw text passed through as-is — Daily Work Log's own validate()/
        # _prepare_tasks() parses every row's estimated_time exactly once on
        # save; pre-parsing here too would double it down to bare minutes
        # (see the matching comments on the other task_updates/carried
        # assignments above).
        "estimated_time": estimated_time or "",
        "series_id": frappe.generate_hash(length=32),
        "origin_date": date,
        "sequence": _next_sequence(log) + 1
    })
    
    _save_work_log(log)

    emp_data = frappe.db.get_value(
        "Employee", assignee_employee,
        ["employee_name", "cell_number"],
        as_dict=True,
    ) or {}

    return {
        "work_log": log.name,
        "employee": assignee_employee,
        "employee_name": emp_data.get("employee_name") or "",
        "cell_number": emp_data.get("cell_number") or None,
        "date": date,
    }