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
import json
from frappe.utils import today, now_datetime, getdate, add_days, get_datetime


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


def _assert_task_owner(task_name, employee_name):
    """Block cross-employee task mutation via db.set_value (BOLA guard)."""
    task_employee = frappe.db.get_value("Daily Task", task_name, "employee")
    if task_employee != employee_name:
        frappe.throw("Not authorised to edit this task.", frappe.PermissionError)


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
    """
    reports_to_names = frappe.get_all("Employee", filters={
        "reports_to": employee_name, "status": "Active",
    }, pluck="name")

    eda_parents = frappe.get_all("Employee Department Assignment",
        filters={"parenttype": "Employee", "team_leader": employee_name},
        pluck="parent")
    eda_names = frappe.get_all("Employee", filters={
        "name": ["in", eda_parents], "status": "Active",
    }, pluck="name") if eda_parents else []

    return list(set(reports_to_names) | set(eda_names))


def _is_team_leader(employee_name):
    """True if this person leads at least one active employee (reports_to or EDA)."""
    return bool(_get_team_members(employee_name))


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
            pass

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
        h = int(val_float)
        m = int(round((val_float - h) * 60))
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
    if not s:
        return 0.0
    s = str(s).strip().lower()
    try:
        return float(s)
    except ValueError:
        pass

    h = 0.0
    m = 0.0

    for term in ['hours', 'hour', 'hrs', 'hr']:
        s = s.replace(term, 'h')
    for term in ['minutes', 'minute', 'mins', 'min', 'm']:
        s = s.replace(term, 'm')

    if 'h' in s:
        parts = s.split('h')
        try:
            h = float(parts[0].strip())
        except ValueError:
            pass
        s = parts[1]
    if 'm' in s:
        parts = s.split('m')
        try:
            m = float(parts[0].strip())
        except ValueError:
            pass

    return h + (m / 60.0)


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
                                  is_late, tasks, date):
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
            ("Date", date),
            ("Login Time", login_hm),
            ("Checked-In At", _format_action_timestamp(checkin_action_time)),
            ("Work Location", work_location),
            ("Half-Day Session", half_day_session),
            ("Status", "Late Check-in" if is_late else None),
        ])
        task_table_html = _render_screenshot_task_table(tasks, is_checkout=False)

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

        frappe.sendmail(
            recipients=[emp_email],
            subject=f"Checked in at {login_hm} — Your plan for {date}",
            message=html,
            now=False,
        )
    except Exception as e:
        frappe.log_error(
            f"Check-in email failed for {employee_name}: {e}",
            "ST Attendance Tracker"
        )


def _send_employee_eod_email(employee_name, employee_display_name, logout_time,
                              checkout_action_time, net_hours, work_location,
                              half_day_session, tasks, date):
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

        # Fetch lunch and check-in times from Daily Task Log
        db_vals = frappe.db.get_value("Daily Task Log", {
            "employee": employee_name,
            "date": date,
            "log_type": "End of Day",
            "docstatus": 1
        }, ["lunch_from", "lunch_to"])
        lunch_from, lunch_to = db_vals if db_vals else (None, None)

        login_time = frappe.db.get_value("Daily Task Log", {
            "employee": employee_name,
            "date": date,
            "log_type": "Morning Check-In",
            "docstatus": 1
        }, "login_time")

        login_hm = _to_ampm(login_time) if login_time else None
        logout_hm = _to_ampm(logout_time)
        lunch_from_hm = _to_ampm(lunch_from) if lunch_from else None
        lunch_to_hm = _to_ampm(lunch_to) if lunch_to else None
        lunch_break = f"{lunch_from_hm} - {lunch_to_hm}" if lunch_from_hm and lunch_to_hm else None

        task_table_html = _render_screenshot_task_table(
            tasks, is_checkout=True, lunch_from=lunch_from_hm, lunch_to=lunch_to_hm
        )

        total_actual_hours = sum(float(t.get("actual_time") or 0.0) for t in tasks)
        working_hours_str = _format_hours(total_actual_hours) or "0h"

        detail_table_html = _render_detail_table([
            ("Employee", employee_display_name),
            ("Date", date),
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

        html = f"""
        <div style="{EMAIL_WRAPPER_STYLE}">
          <div style="{EMAIL_HEADING_STYLE}">Checkout Summary</div>
          {detail_table_html}
          <div style="{EMAIL_SUBHEADING_STYLE}">Today's Work Summary</div>
          <div>
            {task_table_html}
          </div>
        </div>"""

        frappe.sendmail(
            recipients=[emp_email],
            subject=f"Day complete — {len(done_tasks)}/{len(tasks)} tasks done | {date}",
            message=html,
            now=False,
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
        email = frappe.db.get_value("User", emp["user_id"], "email") or emp["user_id"]
    return email


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
                badges.append('<span style="background-color:#faf5ff;color:#7c3aed;font-size:10px;font-weight:600;padding:2px 6px;border-radius:4px;border:1px solid #e9d5ff;margin-left:6px;display:inline-block">Ad-hoc</span>')
            
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

            row_html = f"""
            <tr>
              <td style="{client_style}">{client_text}</td>
              <td style="{task_style}">{html.escape(t.get("description") or "")}{badges_str}</td>
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


def _notify_hr_and_team_leader(employee_name, employee_display_name, event, detail_rows=None, tasks=None):
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

        subject_map = {
            "checkin":  f"{employee_display_name} checked in",
            "checkout": f"{employee_display_name} checked out",
        }
        subject = subject_map.get(event, f"{employee_display_name} — attendance update")

        lunch_from_hm = None
        lunch_to_hm = None
        if event == "checkout":
            date = tasks[0].get("task_date") if (tasks and tasks[0].get("task_date")) else frappe.utils.today()
            db_vals = frappe.db.get_value("Daily Task Log", {
                "employee": employee_name,
                "date": date,
                "log_type": "End of Day",
                "docstatus": 1
            }, ["lunch_from", "lunch_to"])
            if db_vals:
                lunch_from_hm = _to_ampm(db_vals[0]) if db_vals[0] else None
                lunch_to_hm = _to_ampm(db_vals[1]) if db_vals[1] else None

        task_html = _render_screenshot_task_table(
            tasks or [],
            is_checkout=(event == "checkout"),
            lunch_from=lunch_from_hm,
            lunch_to=lunch_to_hm
        )
        task_label = "Today's Tasks" if event == "checkin" else "Task Summary"
        detail_table_html = _render_detail_table(detail_rows or [])

        html = f"""
        <div style="{EMAIL_WRAPPER_STYLE}">
          <div style="{EMAIL_HEADING_STYLE}">{subject}</div>
          {detail_table_html}
          <div style="{EMAIL_SUBHEADING_STYLE}">{task_label}</div>
          <div>
            {task_html}
          </div>
        </div>
        """

        frappe.sendmail(
            recipients=recipients,
            subject=subject,
            message=html,
            now=False,
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


# ── Root task tracer ───────────────────────────────────────────────────────────

def _get_root_task(task_name):
    """
    Walk up the rolled_over_from chain to find the original task.
    Prevents task pending 10 days from being duplicated 10 times.
    """
    current = task_name
    depth = 0
    while depth < 365:
        parent = frappe.db.get_value("Daily Task", current, "rolled_over_from")
        if not parent:
            return current
        # Check if the parent task still exists in the database.
        # If it has been deleted, we treat 'current' as the root/original task.
        if not frappe.db.exists("Daily Task", parent):
            return current
        current = parent
        depth += 1
    return current


def _next_sequence(employee, task_date):
    """Next entry-order value for a new Daily Task on this employee/date."""
    return (frappe.db.get_value(
        "Daily Task", {"employee": employee, "task_date": task_date}, "max(sequence)"
    ) or 0)


# ── Rollover on EOD ────────────────────────────────────────────────────────────

def _rollover_pending_tasks(employee, date):
    # Lock the employee record to serialize EOD rollovers and prevent duplicate entries
    frappe.db.sql("select name from `tabEmployee` where name = %s for update", (employee,))

    next_date = _get_next_working_date(employee, date)

    pending = frappe.get_all("Daily Task", filters={
        "employee": employee,
        "task_date": date,
        "status": ["in", ["Pending", "In Progress"]],
        "task_type": ["!=", "Recurring"],
    }, fields=["name", "description", "task_type", "origin_date",
               "remarks", "rolled_over_from", "estimated_time", "project_name"])

    rolled = 0
    for task in pending:
        root = _get_root_task(task.name)
        root_status = frappe.db.get_value("Daily Task", root, "status")
        if root_status == "Done":
            frappe.db.set_value("Daily Task", task.name, "status", "Done")
            continue

        already = frappe.db.exists("Daily Task", {
            "employee": employee,
            "task_date": next_date,
            "rolled_over_from": root,
        })
        root_date = frappe.db.get_value("Daily Task", root, "task_date")
        if already or str(root_date) == str(next_date):
            continue

        new_task = frappe.new_doc("Daily Task")
        new_task.employee      = employee
        new_task.task_date     = next_date
        new_task.description   = task.description
        new_task.task_type     = "Planned"
        new_task.status        = "Pending"
        new_task.origin_date   = task.origin_date or date
        new_task.rolled_over_from = root
        new_task.estimated_time   = task.estimated_time or ""
        new_task.project_name     = task.project_name or ""
        new_task.remarks = f"[Carried from {task.origin_date or date}]"
        new_task.sequence = _next_sequence(employee, next_date) + 1
        new_task.insert(ignore_permissions=True)
        frappe.db.set_value("Daily Task", task.name, "status", "Rolled Over")
        rolled += 1

    if rolled:
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

    missed = frappe.get_all("Daily Task", filters={
        "employee": employee,
        "task_date": ["<", today_date],
        "status": ["in", ["Pending", "In Progress"]],
    }, fields=["name", "description", "task_type", "origin_date",
               "remarks", "task_date", "estimated_time", "project_name"])

    for task in missed:
        root = _get_root_task(task.name)
        root_status = frappe.db.get_value("Daily Task", root, "status")
        if root_status == "Done":
            frappe.db.set_value("Daily Task", task.name, "status", "Done")
            continue

        already = frappe.db.exists("Daily Task", {
            "employee": employee,
            "task_date": today_date,
            "rolled_over_from": root,
        })
        root_date = frappe.db.get_value("Daily Task", root, "task_date")
        if already or str(root_date) == str(today_date):
            continue

        new_task = frappe.new_doc("Daily Task")
        new_task.employee      = employee
        new_task.task_date     = today_date
        new_task.description   = task.description
        new_task.task_type     = "Planned"
        new_task.status        = "Pending"
        new_task.origin_date   = task.origin_date or task.task_date
        new_task.rolled_over_from = root
        new_task.estimated_time   = task.estimated_time or ""
        new_task.project_name    = task.project_name or ""
        new_task.remarks = f"[Auto-carried from {task.origin_date or task.task_date}]"
        new_task.sequence = _next_sequence(employee, today_date) + 1
        new_task.insert(ignore_permissions=True)
        frappe.db.set_value("Daily Task", task.name, "status", "Rolled Over")

    frappe.db.commit()


# ── Recurring tasks (e.g. daily scrum) ─────────────────────────────────────────

def _ensure_recurring_tasks(employee, date):
    """
    For every active Recurring Task Template belonging to this employee,
    make sure a Daily Task for today already exists; insert one if not.
    Idempotent — safe to call on every page load / check-in attempt.
    Recurring-type tasks never enter the carry-forward chain (see the
    task_type exclusion in _rollover_pending_tasks) — a fresh one appears
    here again tomorrow regardless of whether today's was completed.
    """
    templates = frappe.get_all("Recurring Task Template",
        filters={"employee": employee, "is_active": 1},
        fields=["description", "project_name", "estimated_time"])

    for tpl in templates:
        exists = frappe.db.exists("Daily Task", {
            "employee": employee,
            "task_date": date,
            "task_type": "Recurring",
            "description": tpl.description,
        })
        if exists:
            continue

        new_task = frappe.new_doc("Daily Task")
        new_task.employee      = employee
        new_task.task_date     = date
        new_task.description   = tpl.description
        new_task.task_type     = "Recurring"
        new_task.status        = "Pending"
        new_task.origin_date   = date
        new_task.estimated_time = tpl.estimated_time or ""
        new_task.project_name   = tpl.project_name or ""
        new_task.sequence = _next_sequence(employee, date) + 1
        new_task.insert(ignore_permissions=True)

    frappe.db.commit()


# ── Working hours calculation ──────────────────────────────────────────────────

def _calc_net_hours(login_time, logout_time, lunch_from, lunch_to, date_str):
    """
    Calculates net working hours = (logout - login) - lunch duration.

    All time arguments are normalised through _to_hhmm() before being
    passed to get_datetime(). This fixes the core bug where Frappe's DB
    returns Time fields as datetime.timedelta objects, and
    str(timedelta(seconds=34200)) = '9:30:00' whose first 5 chars are
    '9:30:' (trailing colon) — an unparseable string that silently raised
    an exception and caused net_hours to be stored as ''.
    """
    # Diagnostic log — captures raw inputs so any future discrepancy
    # can be confirmed in Frappe Error Log (filter title "ST NetHours Debug")
    frappe.log_error(
        f"raw inputs → login={login_time!r}({type(login_time).__name__}) "
        f"logout={logout_time!r} lunch={lunch_from!r}-{lunch_to!r} date={date_str}",
        "ST NetHours Debug"
    )
    try:
        login_hm  = _to_hhmm(login_time)
        logout_hm = _to_hhmm(logout_time)

        if not login_hm or not logout_hm:
            frappe.log_error(
                f"_to_hhmm returned empty: login_hm={login_hm!r} logout_hm={logout_hm!r}",
                "ST NetHours Debug"
            )
            return ""

        base = str(date_str) + " "
        login_dt  = get_datetime(base + login_hm)
        logout_dt = get_datetime(base + logout_hm)
        total_mins = int((logout_dt - login_dt).total_seconds() / 60)
        # Handle overnight/night-shift (logout < login crosses midnight)
        if total_mins < 0:
            total_mins += 24 * 60
        elif total_mins == 0:
            # If the calendar date of checkout is the same as the check-in date, consider it 0
            if today() == str(date_str):
                total_mins = 0
            else:
                total_mins = 24 * 60 # 24-hour workday

        lunch_mins = 0
        lf_hm = lt_hm = ""
        if lunch_from and lunch_to:
            lf_hm = _to_hhmm(lunch_from)
            lt_hm = _to_hhmm(lunch_to)
            if lf_hm and lt_hm:
                # Convert all to minutes to check offsets (same as daily_task_log.py validation)
                def time_to_mins(t_str):
                    parts = t_str.split(":")
                    return int(parts[0]) * 60 + int(parts[1])

                login_mins = time_to_mins(login_hm)
                logout_mins = time_to_mins(logout_hm)
                lf_mins = time_to_mins(lf_hm)
                lt_mins = time_to_mins(lt_hm)

                shift_len = (logout_mins - login_mins) if logout_mins >= login_mins \
                            else (logout_mins + 24 * 60 - login_mins)

                lf_abs = (lf_mins - login_mins) if lf_mins >= login_mins \
                         else (lf_mins + 24 * 60 - login_mins)
                lt_abs = (lt_mins - login_mins) if lt_mins >= login_mins \
                         else (lt_mins + 24 * 60 - login_mins)

                if lt_abs < lf_abs:
                    lt_abs += 24 * 60

                lunch_duration = lt_abs - lf_abs

                # Only deduct lunch if it is valid and falls completely within the work shift
                if 0 < lunch_duration and lf_abs >= 0 and lt_abs <= shift_len:
                    lunch_mins = lunch_duration

        net = total_mins - lunch_mins

        # Sanity ceiling — a workday can't exceed 24 hours.
        if net < 0 or net > 24 * 60:
            frappe.log_error(
                f"Suspicious net hours calc: login={login_hm} logout={logout_hm} "
                f"lunch_from={lf_hm} lunch_to={lt_hm} date={date_str} "
                f"total_mins={total_mins} lunch_mins={lunch_mins} net={net}",
                "ST Attendance Tracker — net hours sanity check"
            )
            return ""

        result = f"{net // 60}h {net % 60}m"
        frappe.log_error(
            f"result → login={login_hm} logout={logout_hm} "
            f"total={total_mins} lunch={lunch_mins} net={net} → {result}",
            "ST NetHours Debug"
        )
        return result
    except Exception as e:
        frappe.log_error(
            f"exception: {e} | login={login_time!r} logout={logout_time!r} "
            f"lunch={lunch_from!r}-{lunch_to!r}",
            "ST NetHours Debug"
        )
        return ""


# ── WFH validation ─────────────────────────────────────────────────────────────

def _get_hybrid_office_days():
    """Reads hybrid office days from ST Attendance Settings. Fully dynamic."""
    day_map = {
        "monday": 0, "tuesday": 1, "wednesday": 2,
        "thursday": 3, "friday": 4, "saturday": 5,
    }
    try:
        raw = frappe.db.get_single_value(
            "ST Attendance Settings", "hybrid_office_days"
        ) or "Tuesday\nThursday"
        days = []
        for line in raw.strip().split("\n"):
            d = line.strip().lower()
            if d in day_map:
                days.append(day_map[d])
        return days if days else [1, 3]
    except Exception:
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

    morning_log = frappe.db.exists("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "Morning Check-In", "docstatus": 1,
    })

    # Safety rollover — only before check-in
    if not morning_log:
        _safety_rollover(employee.name, date)
        _ensure_recurring_tasks(employee.name, date)

    # Auto-heal checkin if HR deleted it
    if morning_log:
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
            login_time = frappe.db.get_value(
                "Daily Task Log", morning_log, "login_time"
            )
            _make_checkin(employee.name, "IN", login_time)

    eod_log = frappe.db.exists("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "End of Day", "docstatus": 1,
    })

    tasks = []
    if morning_log:
        tasks = frappe.get_all("Daily Task",
            filters={"employee": employee.name, "task_date": date},
            fields=["name", "description", "status", "task_type",
                    "origin_date", "rolled_over_from", "remarks",
                    "estimated_time", "actual_time"],
            order_by="sequence asc",
        )
        for task in tasks:
            task["is_carried"] = bool(task.get("rolled_over_from"))
            if task.get("rolled_over_from") and task.get("origin_date"):
                task["days_pending"] = (
                    getdate(date) - getdate(task["origin_date"])
                ).days
            else:
                task["days_pending"] = 0

    login_time_val = ""
    if morning_log:
        login_time_val = frappe.db.get_value(
            "Daily Task Log", morning_log, "login_time"
        ) or ""

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
        pass

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
        pass

    has_reset_today = bool(frappe.db.exists("Daily Task Log", {
        "employee": employee.name,
        "date": date,
        "log_type": "Morning Check-In",
        "docstatus": 2
    }))

    return {
        "employee":       employee,
        "date":           date,
        "morning_done":   bool(morning_log),
        "eod_done":       bool(eod_log),
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

    if frappe.db.exists("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "Morning Check-In", "docstatus": 1,
    }):
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
    carried_count = frappe.db.count("Daily Task", {
        "employee": employee.name,
        "task_date": date,
    })

    if not tasks and carried_count == 0:
        frappe.throw("Please add at least one planned task before checking in.")

    # Update carried task descriptions/estimates if employee edited them
    for c in carried:
        task_name = c.get("name")
        if not task_name:
            continue
        _assert_task_owner(task_name, employee.name)
        update = {}
        if (c.get("description") or "").strip():
            update["description"] = c["description"].strip()
        if c.get("estimated_time") is not None:
            update["estimated_time"] = _parse_time_to_hours(c.get("estimated_time", ""))
        if c.get("project_name") is not None:
            update["project_name"] = c.get("project_name", "").strip()
        if update:
            frappe.db.set_value("Daily Task", task_name, update)

    # Insert new tasks, preserving the order the employee entered them in
    sequence_base = _next_sequence(employee.name, date)
    for i, t in enumerate(tasks):
        desc = (t.get("description") or "").strip()
        if not desc:
            continue
        task_doc = frappe.new_doc("Daily Task")
        task_doc.employee       = employee.name
        task_doc.task_date      = date
        task_doc.description    = desc
        task_doc.task_type      = "Planned"
        task_doc.status         = "Pending"
        task_doc.origin_date    = date
        task_doc.estimated_time = t.get("estimated_time", "")
        task_doc.project_name = (t.get("project_name") or "").strip()
        task_doc.sequence = sequence_base + i + 1
        task_doc.insert(ignore_permissions=True)

    actual_login = login_time or now_datetime().strftime("%H:%M:%S")
    if len(str(actual_login)) == 5:
        actual_login = str(actual_login) + ":00"

    log = frappe.new_doc("Daily Task Log")
    log.employee          = employee.name
    log.date              = date
    log.log_type          = "Morning Check-In"
    log.login_time        = actual_login
    log.work_location     = work_location
    log.half_day_session  = _resolve_half_day_session(employee.name, date, half_day_session)
    log.insert(ignore_permissions=True)
    log.flags.ignore_permissions = True
    log.submit()

    _make_checkin(employee.name, "IN", actual_login)
    frappe.db.commit()

    # Fetch today's full task list (planned + carried) once, reused below
    all_tasks = frappe.get_all("Daily Task",
        filters={"employee": employee.name, "task_date": date},
        fields=["name", "description", "status", "task_type",
                "estimated_time", "project_name", "rolled_over_from"],
        order_by="sequence asc",
    )

    # Notification — includes today's tasks + carried-forward tasks
    late_flag = frappe.db.get_value("Daily Task Log", log.name, "is_late")
    resolved_half_day = log.half_day_session
    detail_rows = [
        ("Employee", employee.employee_name),
        ("Date", date),
        ("Login Time", _to_ampm(actual_login)),
        ("Checked-In At", _format_action_timestamp(checkin_action_time)),
        ("Work Location", work_location),
        ("Half-Day Session", resolved_half_day),
        ("Status", "Late Check-in" if late_flag else None),
    ]
    _notify_hr_and_team_leader(
        employee.name,
        employee.employee_name,
        "checkin",
        detail_rows,
        tasks=all_tasks,
    )

    # Send check-in confirmation to employee with task list
    _send_employee_checkin_email(
        employee.name,
        employee.employee_name,
        actual_login,
        checkin_action_time,
        work_location,
        resolved_half_day,
        late_flag,
        all_tasks,
        date,
    )

    return {"success": True, "login_time": _to_ampm(actual_login)}


# ── EOD submit ─────────────────────────────────────────────────────────────────

@frappe.whitelist()
def submit_eod_log(lunch_from, lunch_to, logout_time, task_updates, adhoc_tasks):
    checkout_action_time = now_datetime()
    employee = _get_employee()
    # Lock employee record to serialize checkout/EOD processing
    frappe.db.sql("select name from `tabEmployee` where name = %s for update", (employee.name,))
    
    # Resolve active date: latest check-in without a checkout, else today
    latest_checkin = frappe.db.get_value("Daily Task Log", {
        "employee": employee.name,
        "log_type": "Morning Check-In",
        "docstatus": 1
    }, "date", order_by="date desc")

    if latest_checkin:
        has_eod = frappe.db.exists("Daily Task Log", {
            "employee": employee.name,
            "date": latest_checkin,
            "log_type": "End of Day",
            "docstatus": 1
        })
        if not has_eod:
            date = latest_checkin
        else:
            date = today()
    else:
        date = today()

    if not isinstance(date, str):
        date = frappe.utils.getdate(date).strftime("%Y-%m-%d")

    if not frappe.flags.in_test:
        logout_time = now_datetime().strftime("%H:%M:%S")
    elif not logout_time:
        frappe.throw("Logout time is required.")

    if frappe.db.exists("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "End of Day", "docstatus": 1,
    }):
        frappe.throw(f"You have already checked out for {date}.")

    updates = json.loads(task_updates) if isinstance(task_updates, str) else task_updates
    adhocs  = json.loads(adhoc_tasks)  if isinstance(adhoc_tasks, str)  else adhoc_tasks

    # Update existing task statuses + actual time
    for t in updates:
        name = t.get("name")
        if not name:
            continue
        _assert_task_owner(name, employee.name)
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
        update_fields = {
            "status":      status,
            "actual_time": _parse_time_to_hours(actual_time),
            "remarks":     t.get("remarks", ""),
        }
        # Allow description edit during EOD
        if t.get("description", "").strip():
            update_fields["description"] = t["description"].strip()
        frappe.db.set_value("Daily Task", name, update_fields)

        if t.get("status") == "Done":
            root = _get_root_task(name)
            if root:
                # Find all future tasks rolled over from this root and delete them
                future_tasks = frappe.get_all("Daily Task", filters={
                    "rolled_over_from": root,
                    "task_date": [">", date],
                    "status": ["in", ["Pending", "In Progress", "Rolled Over"]]
                }, fields=["name"])
                for ft in future_tasks:
                    frappe.delete_doc("Daily Task", ft.name, ignore_permissions=True, force=True)

            ancestor = frappe.db.get_value("Daily Task", name, "rolled_over_from")
            depth = 0
            while ancestor and depth < 365:
                # If an ancestor task was deleted, stop traversing to prevent issues
                if not frappe.db.exists("Daily Task", ancestor):
                    break
                frappe.db.set_value("Daily Task", ancestor, "status", "Done")
                ancestor = frappe.db.get_value("Daily Task", ancestor, "rolled_over_from")
                depth += 1

    # Insert ad-hoc tasks, preserving the order the employee entered them in
    sequence_base = _next_sequence(employee.name, date)
    for i, t in enumerate(adhocs):
        desc = (t.get("description") or "").strip()
        if not desc:
            continue
        task_doc = frappe.new_doc("Daily Task")
        task_doc.employee    = employee.name
        task_doc.task_date   = date
        task_doc.description = desc
        task_doc.task_type   = "Ad-hoc"
        task_doc.status      = t.get("status", "Done")
        task_doc.origin_date = date
        task_doc.estimated_time = t.get("estimated_time", "")
        task_doc.actual_time = t.get("actual_time", "")
        task_doc.remarks     = t.get("remarks", "")
        task_doc.project_name = (t.get("project_name") or "").strip()
        task_doc.sequence = sequence_base + i + 1
        task_doc.insert(ignore_permissions=True)

    # Get login time for net hours calculation
    morning_log_name = frappe.db.get_value("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "Morning Check-In", "docstatus": 1,
    }, "name")
    login_time_raw = ""
    half_day_session = ""
    work_location = ""
    if morning_log_name:
        morning_log = frappe.db.get_value(
            "Daily Task Log", morning_log_name,
            ["login_time", "half_day_session", "work_location"], as_dict=True
        )
        login_time_raw = morning_log.login_time or ""
        half_day_session = morning_log.half_day_session or ""
        work_location = morning_log.work_location or ""

    # FIX: normalise all time values through _to_hhmm before calc
    # Frappe DB returns Time fields as datetime.timedelta; JS sends 'HH:MM' strings.
    # _calc_net_hours now handles both, but normalising here is belt-and-suspenders.
    net_hours = _calc_net_hours(
        login_time_raw, logout_time, lunch_from, lunch_to, date
    ) if login_time_raw else ""

    log = frappe.new_doc("Daily Task Log")
    log.employee    = employee.name
    log.date        = date
    log.log_type    = "End of Day"
    log.login_time  = login_time_raw
    log.lunch_from  = lunch_from  or ""
    log.lunch_to    = lunch_to    or ""
    log.logout_time = logout_time
    log.net_hours   = net_hours
    log.insert(ignore_permissions=True)
    log.flags.ignore_permissions = True
    log.submit()

    _make_checkin(employee.name, "OUT", logout_time)
    pending_count = _rollover_pending_tasks(employee.name, date)
    frappe.db.commit()

    done_count = frappe.db.count("Daily Task", {
        "employee": employee.name, "task_date": date, "status": "Done"
    })
    total_count = frappe.db.count("Daily Task", {
        "employee": employee.name, "task_date": date,
    })

    # Fetch full task list once — used by both HR/TL notification and employee email
    all_tasks = frappe.get_all("Daily Task",
        filters={"employee": employee.name, "task_date": date},
        fields=["name", "description", "status", "task_type",
                "estimated_time", "actual_time", "project_name", "rolled_over_from"],
        order_by="sequence asc",
    )

    total_actual_hours = sum(float(t.get("actual_time") or 0.0) for t in all_tasks)
    working_hours_str = _format_hours(total_actual_hours) or "0h"

    detail_rows = [
        ("Employee", employee.employee_name),
        ("Date", date),
        ("Login Time", _to_ampm(login_time_raw) if login_time_raw else None),
        ("Logout Time", _to_ampm(logout_time)),
        ("Checked-Out At", _format_action_timestamp(checkout_action_time)),
        ("Work Location", work_location),
        ("Net Working Hours", net_hours),
        ("Total Task Hours", working_hours_str),
        ("Half-Day Session", half_day_session),
        ("Tasks Completed", f"{done_count}/{total_count}"),
    ]
    _notify_hr_and_team_leader(
        employee.name,
        employee.employee_name,
        "checkout",
        detail_rows,
        tasks=all_tasks,
    )

    # Send EOD confirmation to employee with task summary
    _send_employee_eod_email(
        employee.name,
        employee.employee_name,
        logout_time,
        checkout_action_time,
        net_hours,
        work_location,
        half_day_session,
        all_tasks,
        date,
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
        "Department", filters={"is_group": 0}, pluck="name"
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
    checked_in = frappe.get_all("Daily Task Log", filters={
        "date": date, "log_type": "Morning Check-In", "docstatus": 1,
    }, pluck="employee")
    eod_done = frappe.get_all("Daily Task Log", filters={
        "date": date, "log_type": "End of Day", "docstatus": 1,
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

    eod_logs = frappe.db.get_all("Daily Task Log", filters={
        "date": date,
        "log_type": "End of Day",
        "docstatus": 1
    }, fields=["employee", "employee_name", "net_hours", "logout_time"])

    checkin_logs = frappe.db.get_all("Daily Task Log", filters={
        "date": date,
        "log_type": "Morning Check-In",
        "docstatus": 1
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

    return {
        "departments": result,
        "date": date,
        "rankings": rankings,
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
        reports_to_me = frappe.db.exists("Employee", {
            "name": employee_name,
            "reports_to": current_emp.name,
        })
        if not reports_to_me:
            frappe.throw("Access denied.", frappe.PermissionError)

    date = date or today()

    emp = frappe.db.get_value("Employee", employee_name,
        ["name", "employee_name", "department", "designation"], as_dict=True)

    morning_log = frappe.db.get_value("Daily Task Log", {
        "employee": employee_name, "date": date,
        "log_type": "Morning Check-In", "docstatus": 1,
    }, ["login_time", "is_late", "name"], as_dict=True)

    eod_log = frappe.db.get_value("Daily Task Log", {
        "employee": employee_name, "date": date,
        "log_type": "End of Day", "docstatus": 1,
    }, ["logout_time", "lunch_from", "lunch_to", "net_hours", "name"], as_dict=True)

    tasks = frappe.get_all("Daily Task", filters={
        "employee": employee_name, "task_date": date,
    }, fields=["name", "description", "status", "task_type",
               "estimated_time", "actual_time", "rolled_over_from", "origin_date"],
    order_by="sequence asc")

    for task in tasks:
        task["is_carried"] = bool(task.get("rolled_over_from"))

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

    logs = frappe.db.sql("""
        SELECT
            l.date,
            MAX(CASE WHEN l.log_type = 'Morning Check-In' THEN l.login_time  END) as login_time,
            MAX(CASE WHEN l.log_type = 'End of Day'       THEN l.logout_time END) as logout_time,
            MAX(CASE WHEN l.log_type = 'End of Day'       THEN l.net_hours   END) as net_hours,
            MAX(CASE WHEN l.log_type = 'End of Day'       THEN l.lunch_from  END) as lunch_from,
            MAX(CASE WHEN l.log_type = 'End of Day'       THEN l.lunch_to    END) as lunch_to,
            MAX(CASE WHEN l.log_type = 'Morning Check-In' THEN l.is_late     END) as is_late,
            SUM(CASE WHEN l.log_type = 'End of Day'       THEN 1 ELSE 0      END) as eod_done
        FROM `tabDaily Task Log` l
        WHERE l.employee = %(employee)s
          AND l.docstatus = 1
          AND l.date <= %(today)s
        GROUP BY l.date
        ORDER BY l.date DESC
        LIMIT %(limit)s OFFSET %(offset)s
    """, {"employee": employee.name, "today": today(),
          "limit": limit, "offset": offset}, as_dict=True)

    for log in logs:
        counts = frappe.db.sql("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'Done' THEN 1 ELSE 0 END) as done
            FROM `tabDaily Task`
            WHERE employee = %(emp)s AND task_date = %(date)s
        """, {"emp": employee.name, "date": log.date}, as_dict=True)

        log["total_tasks"] = counts[0].total if counts else 0
        log["done_tasks"]  = counts[0].done  if counts else 0

    return {
        "logs":     logs,
        "employee": employee,
        "has_more": len(logs) == limit,
    }


@frappe.whitelist()
def get_history_day_detail(date):
    """Full task list for a specific past date."""
    employee = _get_employee()

    tasks = frappe.get_all("Daily Task", filters={
        "employee": employee.name, "task_date": date,
    }, fields=["name", "description", "status", "task_type",
               "estimated_time", "actual_time", "rolled_over_from",
               "origin_date", "remarks", "project_name"],
    order_by="sequence asc")

    for task in tasks:
        task["is_carried"] = bool(task.get("rolled_over_from"))

    morning_log = frappe.db.get_value("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "Morning Check-In", "docstatus": 1,
    }, ["login_time", "is_late"], as_dict=True)

    eod_log = frappe.db.get_value("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "End of Day", "docstatus": 1,
    }, ["logout_time", "lunch_from", "lunch_to", "net_hours"], as_dict=True)

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
    task_employee, task_date = frappe.db.get_value("Daily Task", name, ["employee", "task_date"])
    if task_employee != employee.name:
        frappe.throw("Not authorised to delete this task.", frappe.PermissionError)

    if frappe.db.exists("Daily Task Log", {
        "employee": employee.name,
        "date": task_date,
        "log_type": "End of Day",
        "docstatus": 1
    }):
        frappe.throw("Cannot delete tasks after checkout is submitted.", frappe.ValidationError)

    # Clean up ancestors to prevent safety/EOD rollover from resurrecting the task
    root = _get_root_task(name)
    if root:
        frappe.db.sql("""
            UPDATE `tabDaily Task`
            SET status = 'Rolled Over'
            WHERE employee = %s
              AND (name = %s OR rolled_over_from = %s)
              AND status IN ('Pending', 'In Progress')
        """, (employee.name, root, root))

    frappe.delete_doc("Daily Task", name, ignore_permissions=True, force=True)
    frappe.db.commit()
    return {"success": True}


@frappe.whitelist()
def delete_carried_project(project_name, task_date):
    """Delete all tasks belonging to a project for a specific date."""
    employee = _get_employee()

    if frappe.db.exists("Daily Task Log", {
        "employee": employee.name,
        "date": task_date,
        "log_type": "End of Day",
        "docstatus": 1
    }):
        frappe.throw("Cannot delete project tasks after checkout is submitted.", frappe.ValidationError)

    # Find all tasks for this employee, date, and project name
    tasks = frappe.get_all("Daily Task", filters={
        "employee": employee.name,
        "task_date": task_date,
        "project_name": project_name
    }, fields=["name"])

    for t in tasks:
        # Clean up ancestors to prevent safety/EOD rollover from resurrecting the task
        root = _get_root_task(t.name)
        if root:
            frappe.db.sql("""
                UPDATE `tabDaily Task`
                SET status = 'Rolled Over'
                WHERE employee = %s
                  AND (name = %s OR rolled_over_from = %s)
                  AND status IN ('Pending', 'In Progress')
            """, (employee.name, root, root))
        frappe.delete_doc("Daily Task", t.name, ignore_permissions=True, force=True)

    frappe.db.commit()
    return {"success": True}



@frappe.whitelist()
def reset_morning_checkin():
    """Reset morning check-in by deleting check-in logs and newly created tasks for today."""
    employee = _get_employee()
    
    # Resolve active date: latest check-in without a checkout, else today
    latest_checkin = frappe.db.get_value("Daily Task Log", {
        "employee": employee.name,
        "log_type": "Morning Check-In",
        "docstatus": 1
    }, "date", order_by="date desc")

    if latest_checkin:
        has_eod = frappe.db.exists("Daily Task Log", {
            "employee": employee.name,
            "date": latest_checkin,
            "log_type": "End of Day",
            "docstatus": 1
        })
        if not has_eod:
            date = latest_checkin
        else:
            date = today()
    else:
        date = today()

    if not isinstance(date, str):
        date = frappe.utils.getdate(date).strftime("%Y-%m-%d")

    # 1. Check if EOD is already submitted
    eod_exists = frappe.db.exists("Daily Task Log", {
        "employee": employee.name,
        "date": date,
        "log_type": "End of Day",
        "docstatus": 1,
    })
    if eod_exists:
        frappe.throw(f"Cannot reset check-in because checkout has already been submitted for {date}.")


    # 2. Get Morning Check-In log name
    morning_log = frappe.db.get_value("Daily Task Log", {
        "employee": employee.name,
        "date": date,
        "log_type": "Morning Check-In",
        "docstatus": 1,
    })
    if not morning_log:
        frappe.throw(f"You have not checked in on {date}.")

    # 3. Cancel Morning Check-In log (sets docstatus = 2)
    morning_doc = frappe.get_doc("Daily Task Log", morning_log)
    morning_doc.flags.ignore_permissions = True
    if morning_doc.docstatus == 1:
        morning_doc.cancel()

    # 4. Delete Employee Checkin created by ST Daily Checkin today
    frappe.db.delete("Employee Checkin", {
        "employee": employee.name,
        "log_type": "IN",
        "device_id": "ST Daily Checkin",
        "time": ["between", [date + " 00:00:00", date + " 23:59:59"]],
    })

    # 5. Revert ALL tasks back to Pending so they are preserved for re-check-in.
    #    Previously this deleted planned tasks, causing user work loss.
    frappe.db.sql("""
        UPDATE `tabDaily Task`
        SET status = 'Pending'
        WHERE employee = %s AND task_date = %s AND status != 'Pending'
    """, (employee.name, date))

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

    if frappe.db.exists("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "End of Day", "docstatus": 1,
    }):
        frappe.throw("Cannot change half-day session after checkout is submitted.")

    morning_log = frappe.db.get_value("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "Morning Check-In", "docstatus": 1,
    }, "name")
    if not morning_log:
        frappe.throw("You have not checked in today.")

    resolved = _resolve_half_day_session(employee.name, date, session)
    if not resolved:
        frappe.throw("You do not have an approved half-day leave for today.")

    frappe.db.set_value("Daily Task Log", morning_log, "half_day_session", resolved)
    frappe.db.commit()
    return {"success": True, "half_day_session": resolved}


# ── Shared builder ─────────────────────────────────────────────────────────────

def _build_team_data(employees, date):
    """Build attendance + task data for a list of employees on a given date."""
    if not employees:
        return {"employees": [], "summary": {}}

    emp_names = [e.name for e in employees]

    morning_logs = frappe.get_all("Daily Task Log", filters={
        "employee": ["in", emp_names],
        "date": date,
        "log_type": "Morning Check-In",
        "docstatus": 1,
    }, fields=["employee", "login_time", "is_late"])
    checked_in_map = {l.employee: l for l in morning_logs}

    eod_logs = frappe.get_all("Daily Task Log", filters={
        "employee": ["in", emp_names],
        "date": date,
        "log_type": "End of Day",
        "docstatus": 1,
    }, fields=["employee", "logout_time", "net_hours"])
    eod_map = {l.employee: l for l in eod_logs}

    on_leave = set(frappe.get_all("Leave Application", filters={
        "employee": ["in", emp_names],
        "from_date": ["<=", date],
        "to_date":   [">=", date],
        "status":    "Approved",
        "docstatus": 1,
    }, pluck="employee"))

    all_tasks = frappe.get_all("Daily Task", filters={
        "employee": ["in", emp_names],
        "task_date": date,
    }, fields=["employee", "name", "description", "status",
               "task_type", "estimated_time", "actual_time", "rolled_over_from"])

    tasks_by_emp = {}
    for t in all_tasks:
        tasks_by_emp.setdefault(t.employee, []).append(t)

    result_employees = []
    for emp in employees:
        morning = checked_in_map.get(emp.name)
        eod     = eod_map.get(emp.name)
        tasks   = tasks_by_emp.get(emp.name, [])
        done    = sum(1 for t in tasks if t.status == "Done")

        if emp.name in on_leave:
            status = "leave"
        elif eod:
            status = "eod_done"
        elif morning:
            status = "late" if morning.is_late else "checked_in"
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
            "on_leave":   len(on_leave),
            "late":       sum(1 for e in result_employees if e["status"] == "late"),
            "eod_done":   len(eod_map),
        }
    }