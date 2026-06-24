"""
ST Attendance Tracker v2 — API
All whitelisted methods consumed by web pages.

Fixes:
  - Notifications sent to HR Manager role users + Team Leader (via reports_to)
  - No manual recipient configuration needed
  - reports_to field used for Team Leader identification
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
import json
from frappe.utils import today, now_datetime, getdate, add_days, get_datetime


# ── Employee helper ────────────────────────────────────────────────────────────

def _get_employee():
    emp = frappe.db.get_value(
        "Employee",
        {"user_id": frappe.session.user},
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


# ── Team leader check ──────────────────────────────────────────────────────────

def _is_team_leader(employee_name):
    """True if at least one active employee has reports_to = this person."""
    return bool(frappe.db.exists("Employee", {
        "reports_to": employee_name,
        "status": "Active",
    }))


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
        checkin.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(
            f"Checkin failed for {employee} ({log_type}): {e}",
            "ST Attendance Tracker"
        )


# ── Time normalisation helper ──────────────────────────────────────────────────

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


# ── Notification helper ────────────────────────────────────────────────────────


# ── Employee self-notification emails ─────────────────────────────────────────

def _send_employee_checkin_email(employee_name, employee_display_name,
                                  login_time, work_location, tasks, date):
    """
    Send check-in confirmation email to the employee themselves.
    Includes today's planned task list grouped by project.
    """
    try:
        emp_email = _get_employee_email(employee_name)
        if not emp_email:
            return

        login_hm = _to_ampm(login_time)
        task_table_html = _render_screenshot_task_table(tasks, is_checkout=False)

        html = f"""
        <div style="font-family:'Segoe UI', Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 12px;">
          <div style="font-size: 16px; font-weight: bold; color: #15803d; margin-bottom: 4px;">Login: {login_hm}</div>
          <div style="font-size: 18px; font-weight: bold; color: #1e3a8a; margin-bottom: 16px;">Today's Work To Do:</div>
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


def _send_employee_eod_email(employee_name, employee_display_name,
                              logout_time, net_hours, tasks, date):
    """
    Send EOD confirmation email to the employee themselves.
    Includes done tasks and carried-forward tasks.
    """
    try:
        emp_email = _get_employee_email(employee_name)
        if not emp_email:
            return

        done_tasks    = [t for t in tasks if t.get("status") == "Done"]
        pending_tasks = [t for t in tasks if t.get("status") in ["Pending", "In Progress"]]

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

        login_hm = _to_ampm(login_time) if login_time else "—"
        logout_hm = _to_ampm(logout_time)
        lunch_from_hm = _to_ampm(lunch_from) if lunch_from else None
        lunch_to_hm = _to_ampm(lunch_to) if lunch_to else None

        task_table_html = _render_screenshot_task_table(
            tasks, is_checkout=True, lunch_from=lunch_from_hm, lunch_to=lunch_to_hm
        )

        html = f"""
        <div style="font-family:'Segoe UI', Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 12px;">
          <div style="font-size: 16px; font-weight: bold; color: #15803d; margin-bottom: 4px;">Login: {login_hm}</div>
          <div style="font-size: 16px; font-weight: bold; color: #dc2626; margin-bottom: 4px;">Logout: {logout_hm}</div>
          <div style="font-size: 18px; font-weight: bold; color: #1e3a8a; margin-bottom: 16px;">Today's Work Summary:</div>
          <div>
            {task_table_html}
          </div>
        </div>
        """

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

    # Group tasks by project
    groups = {}
    for t in tasks:
        pname = (t.get("project_name") or "").strip()
        if not pname:
            pname = "General"
        groups.setdefault(pname, []).append(t)

    # Sort groups: General first, then alphabetically
    sorted_group_names = sorted(groups.keys(), key=lambda x: (x != "General", x.lower()))

    palettes = [
        {"bg": "#eff6ff", "border": "#bfdbfe", "text": "#1e40af"}, # Blue (General)
        {"bg": "#fdf2f8", "border": "#fbcfe8", "text": "#9d174d"}, # Pink
        {"bg": "#fffbeb", "border": "#fde68a", "text": "#92400e"}, # Amber
        {"bg": "#f0fdfa", "border": "#99f6e4", "text": "#115e59"}, # Teal
        {"bg": "#faf5ff", "border": "#e9d5ff", "text": "#6b21a8"}, # Purple
        {"bg": "#f5f3ff", "border": "#ddd6fe", "text": "#5b21b6"}, # Indigo
    ]

    html_rows = []

    header_html = """
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:16px;font-family:'Segoe UI', Arial, sans-serif;border:1px solid #cbd5e1;table-layout:fixed">
      <thead>
        <tr style="background-color:#1553a1;color:#ffffff;font-size:12.5px;font-weight:bold">
          <th style="padding:12px 10px;text-align:left;border:1px solid #cbd5e1;width:20%">Client</th>
          <th style="padding:12px 10px;text-align:left;border:1px solid #cbd5e1;width:50%">Task / Work</th>
          <th style="padding:12px 10px;text-align:center;border:1px solid #cbd5e1;width:15%">Time</th>
          <th style="padding:12px 10px;text-align:center;border:1px solid #cbd5e1;width:15%">Status</th>
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
            client_text = pname if task_idx == 0 else ""
            client_style = f"padding:10px 12px;font-size:13px;font-weight:bold;color:{palette['text']};background-color:{palette['bg']};border:1px solid {palette['border']};vertical-align:top;word-wrap:break-word;overflow:hidden;text-overflow:ellipsis" if task_idx == 0 else f"padding:10px 12px;background-color:{palette['bg']};border:1px solid {palette['border']};vertical-align:top"

            badges = []
            if t.get("rolled_over_from"):
                badges.append('<span style="background-color:#fffbeb;color:#b45309;font-size:10px;font-weight:600;padding:2px 6px;border-radius:4px;border:1px solid #fde68a;margin-left:6px;display:inline-block">Carried</span>')
            if t.get("task_type") == "Ad-hoc":
                badges.append('<span style="background-color:#faf5ff;color:#7c3aed;font-size:10px;font-weight:600;padding:2px 6px;border-radius:4px;border:1px solid #e9d5ff;margin-left:6px;display:inline-block">Ad-hoc</span>')
            
            badges_str = "".join(badges)
            task_style = f"padding:10px 12px;font-size:13px;color:#334155;background-color:{palette['bg']};border:1px solid {palette['border']};vertical-align:top;word-wrap:break-word"

            if is_checkout:
                time_val = t.get("actual_time") or t.get("estimated_time") or ""
            else:
                time_val = t.get("estimated_time") or ""
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
              <td style="{task_style}">{t.get("description") or ""}{badges_str}</td>
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


def _render_compact_task_rows(tasks, mode="checkin"):
    """
    Compact, scannable task list for HR/Team Leader emails.
    mode="checkin"  → shows all tasks for today, tags Carried tasks
    mode="checkout" → groups into Done / Ad-hoc / Pending sections
    Kept deliberately denser than the employee's own email since HR/TL
    receive one of these per employee per event, multiple times a day.
    """
    if not tasks:
        return '<p style="font-size:12px;color:#9ca3af;font-style:italic;margin:6px 0">No tasks recorded.</p>'

    def row(t):
        tag = ""
        if t.get("rolled_over_from"):
            tag = ' <span style="color:#92400e;font-size:10px;background:#fef3c7;padding:1px 6px;border-radius:8px">Carried</span>'
        elif t.get("task_type") == "Ad-hoc":
            tag = ' <span style="color:#7c3aed;font-size:10px;background:#f3e8ff;padding:1px 6px;border-radius:8px">Ad-hoc</span>'
        proj = f' <span style="color:#9ca3af;font-size:10px">[{t["project_name"]}]</span>' if t.get("project_name") else ""
        est  = f' <span style="color:#9ca3af;font-size:10px">Est:{t["estimated_time"]}</span>' if t.get("estimated_time") else ""
        act  = f' <span style="color:#2563eb;font-size:10px">Done in:{t["actual_time"]}</span>' if t.get("actual_time") else ""
        return f'<li style="margin-bottom:4px;font-size:12.5px;color:#374151">{t["description"]}{tag}{proj}{est}{act}</li>'

    if mode == "checkin":
        items = "".join(row(t) for t in tasks)
        return f'<ul style="margin:6px 0 0;padding-left:18px">{items}</ul>'

    # checkout mode: split into sections
    done    = [t for t in tasks if t.get("status") == "Done"]
    pending = [t for t in tasks if t.get("status") in ["Pending", "In Progress"]]

    out = ""
    if done:
        out += (
            '<div style="font-size:11px;font-weight:600;color:#15803d;margin:10px 0 3px">'
            f'Completed ({len(done)})</div>'
            f'<ul style="margin:0;padding-left:18px">{"".join(row(t) for t in done)}</ul>'
        )
    if pending:
        out += (
            '<div style="font-size:11px;font-weight:600;color:#d97706;margin:10px 0 3px">'
            f'Carried to tomorrow ({len(pending)})</div>'
            f'<ul style="margin:0;padding-left:18px">{"".join(row(t) for t in pending)}</ul>'
        )
    return out or '<p style="font-size:12px;color:#9ca3af;font-style:italic;margin:6px 0">No tasks recorded.</p>'


def _notify_hr_and_team_leader(employee_name, employee_display_name, event, details="", tasks=None):
    """
    Send email to:
    1. All users with HR Manager role (auto-fetched)
    2. The employee's Team Leader (via reports_to → user_id)

    tasks: optional list of Daily Task dicts for the relevant date.
           Rendered compactly below the summary line so HR/TL can see
           what the employee is working on without opening the system.
    """
    try:
        recipients = _get_hr_manager_emails()

        # Add Team Leader via reports_to
        reports_to = frappe.db.get_value("Employee", employee_name, "reports_to")
        if reports_to:
            tl_user = frappe.db.get_value("Employee", reports_to, "user_id")
            if tl_user and tl_user not in recipients:
                recipients.append(tl_user)

        if not recipients:
            return

        recipients = list(set(recipients))

        subject_map = {
            "checkin":  f"{employee_display_name} checked in",
            "checkout": f"{employee_display_name} submitted EOD",
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
        task_label = "Today's tasks" if event == "checkin" else "Task summary"

        html = f"""
        <div style="font-family:'Segoe UI', Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 12px;">
          <p style="font-size: 14px; color: #334155; margin: 0 0 16px; line-height: 1.5;">{details}</p>
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


# ── Rollover on EOD ────────────────────────────────────────────────────────────

def _rollover_pending_tasks(employee, date):
    # Lock the employee record to serialize EOD rollovers and prevent duplicate entries
    frappe.db.sql("select name from `tabEmployee` where name = %s for update", (employee,))

    next_date = _get_next_working_date(employee, date)

    pending = frappe.get_all("Daily Task", filters={
        "employee": employee,
        "task_date": date,
        "status": ["in", ["Pending", "In Progress"]],
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
        new_task.insert(ignore_permissions=True)
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

        lunch_mins = 0
        lf_hm = lt_hm = ""
        if lunch_from and lunch_to:
            lf_hm = _to_hhmm(lunch_from)
            lt_hm = _to_hhmm(lunch_to)
            if lf_hm and lt_hm:
                lf = get_datetime(base + lf_hm)
                lt = get_datetime(base + lt_hm)
                lunch_mins = int((lt - lf).total_seconds() / 60)
                # Invalid/reversed lunch entry — ignore rather than corrupt net hours
                if lunch_mins < 0 or lunch_mins > 240:
                    lunch_mins = 0

        net = total_mins - lunch_mins

        # Sanity ceiling — a workday can't sensibly exceed 18 hours.
        if net < 0 or net > 18 * 60:
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


@frappe.whitelist()
def validate_wfh_request():
    """
    Check if logged-in employee has a saved/submitted Attendance Request
    with reason = Work From Home for today.
    """
    employee = frappe.db.get_value(
        "Employee", {"user_id": frappe.session.user}, "name"
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

    exists = frappe.db.exists("Attendance Request", {
        "employee": employee,
        "from_date": ["<=", date],
        "to_date":   [">=", date],
        "reason":    "Work From Home",
        "docstatus": ["in", [0, 1]],
    })

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
            order_by="task_type asc, creation asc",
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

    return {
        "employee":       employee,
        "date":           date,
        "morning_done":   bool(morning_log),
        "eod_done":       bool(eod_log),
        "tasks":          tasks,
        "current_time":   now_datetime().strftime("%H:%M"),
        # FIX: use _to_hhmm so single-digit-hour timedeltas (e.g. 9:30:00)
        # don't produce a trailing colon after [:5] slicing
        "login_time":     _to_ampm(login_time_val),
        "is_team_leader": _is_team_leader(employee.name),
    }


# ── Morning submit ─────────────────────────────────────────────────────────────

@frappe.whitelist()
def submit_morning_log(new_tasks, login_time=None, carried_updates=None, work_location=None):
    """
    new_tasks:       JSON list of {description, estimated_time}
    login_time:      optional HH:MM override
    carried_updates: JSON list of {name, description, estimated_time} edits
    """
    employee = _get_employee()
    # Lock employee record to serialize check-in processing
    frappe.db.sql("select name from `tabEmployee` where name = %s for update", (employee.name,))

    date = today()

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
        update = {}
        if (c.get("description") or "").strip():
            update["description"] = c["description"].strip()
        if c.get("estimated_time") is not None:
            update["estimated_time"] = c.get("estimated_time", "")
        if update:
            frappe.db.set_value("Daily Task", task_name, update)

    # Insert new tasks
    for t in tasks:
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
        task_doc.insert(ignore_permissions=True)

    actual_login = login_time or now_datetime().strftime("%H:%M:%S")
    if len(str(actual_login)) == 5:
        actual_login = str(actual_login) + ":00"

    log = frappe.new_doc("Daily Task Log")
    log.employee       = employee.name
    log.date           = date
    log.log_type       = "Morning Check-In"
    log.login_time     = actual_login
    log.work_location  = work_location
    log.insert(ignore_permissions=True)
    log.submit()

    _make_checkin(employee.name, "IN", actual_login)
    frappe.db.commit()

    # Fetch today's full task list (planned + carried) once, reused below
    all_tasks = frappe.get_all("Daily Task",
        filters={"employee": employee.name, "task_date": date},
        fields=["name", "description", "status", "task_type",
                "estimated_time", "project_name", "rolled_over_from"],
        order_by="project_name asc, creation asc",
    )

    # Notification — includes today's tasks + carried-forward tasks
    late_flag = frappe.db.get_value("Daily Task Log", log.name, "is_late")
    late_text = " (Late check-in)" if late_flag else ""
    wfh_text = f" [{work_location}]" if work_location != "Office" else ""
    _notify_hr_and_team_leader(
        employee.name,
        employee.employee_name,
        "checkin",
        f"{employee.employee_name} checked in at {_to_hhmm(actual_login)}{late_text}{wfh_text} on {date}.",
        tasks=all_tasks,
    )

    # Send check-in confirmation to employee with task list
    _send_employee_checkin_email(
        employee.name,
        employee.employee_name,
        actual_login,
        work_location,
        all_tasks,
        date,
    )

    return {"success": True, "login_time": _to_ampm(actual_login)}


# ── EOD submit ─────────────────────────────────────────────────────────────────

@frappe.whitelist()
def submit_eod_log(lunch_from, lunch_to, logout_time, task_updates, adhoc_tasks):
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

    if not logout_time:
        frappe.throw("Logout time is required.")

    if frappe.db.exists("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "End of Day", "docstatus": 1,
    }):
        frappe.throw(f"You have already submitted End of Day for {date}.")

    updates = json.loads(task_updates) if isinstance(task_updates, str) else task_updates
    adhocs  = json.loads(adhoc_tasks)  if isinstance(adhoc_tasks, str)  else adhoc_tasks

    # Update existing task statuses + actual time
    for t in updates:
        name = t.get("name")
        if not name:
            continue
        update_fields = {
            "status":      t.get("status", "Pending"),
            "actual_time": t.get("actual_time", ""),
            "remarks":     t.get("remarks", ""),
        }
        # Allow description edit during EOD
        if t.get("description", "").strip():
            update_fields["description"] = t["description"].strip()
        frappe.db.set_value("Daily Task", name, update_fields)

        if t.get("status") == "Done":
            ancestor = frappe.db.get_value("Daily Task", name, "rolled_over_from")
            depth = 0
            while ancestor and depth < 365:
                # If an ancestor task was deleted, stop traversing to prevent issues
                if not frappe.db.exists("Daily Task", ancestor):
                    break
                frappe.db.set_value("Daily Task", ancestor, "status", "Done")
                ancestor = frappe.db.get_value("Daily Task", ancestor, "rolled_over_from")
                depth += 1

    # Insert ad-hoc tasks
    for t in adhocs:
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
        task_doc.insert(ignore_permissions=True)

    # Get login time for net hours calculation
    morning_log_name = frappe.db.get_value("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "Morning Check-In", "docstatus": 1,
    }, "name")
    login_time_raw = ""
    if morning_log_name:
        login_time_raw = frappe.db.get_value(
            "Daily Task Log", morning_log_name, "login_time"
        ) or ""

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
        order_by="project_name asc, creation asc",
    )

    _notify_hr_and_team_leader(
        employee.name,
        employee.employee_name,
        "checkout",
        f"{employee.employee_name} submitted EOD at {_to_hhmm(logout_time)}. "
        f"{done_count}/{total_count} tasks done.",
        tasks=all_tasks,
    )

    # Send EOD confirmation to employee with task summary
    _send_employee_eod_email(
        employee.name,
        employee.employee_name,
        logout_time,
        net_hours,
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

    if not _is_team_leader(employee.name):
        frappe.throw("Access denied. You are not a Team Leader.", frappe.PermissionError)

    team = frappe.get_all("Employee", filters={
        "reports_to": employee.name,
        "status": "Active",
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

    return {
        "departments": result,
        "date": date,
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
    order_by="task_type asc, creation asc")

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
    order_by="task_type asc, creation asc")

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
    task_employee = frappe.db.get_value("Daily Task", name, "employee")
    if task_employee != employee.name:
        frappe.throw("Not authorised to delete this task.", frappe.PermissionError)
    frappe.delete_doc("Daily Task", name, ignore_permissions=True, force=True)
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
        frappe.throw(f"Cannot reset check-in because End of Day has already been submitted for {date}.")

    # 2. Get Morning Check-In log name
    morning_log = frappe.db.get_value("Daily Task Log", {
        "employee": employee.name,
        "date": date,
        "log_type": "Morning Check-In",
    })
    if not morning_log:
        frappe.throw(f"You have not checked in on {date}.")

    # 3. Delete Morning Check-In log
    frappe.db.delete("Daily Task Log", {"name": morning_log})

    # 4. Delete Employee Checkin created by ST Daily Checkin today
    frappe.db.delete("Employee Checkin", {
        "employee": employee.name,
        "log_type": "IN",
        "device_id": "ST Daily Checkin",
        "time": ["between", [date + " 00:00:00", date + " 23:59:59"]],
    })

    # 5. Delete newly planned tasks created today (not rolled over)
    frappe.db.delete("Daily Task", {
        "employee": employee.name,
        "task_date": date,
        "task_type": "Planned",
        "rolled_over_from": ["is", "not set"],
    })

    # 6. Revert carried-forward tasks back to Pending status so they are ready to check in again
    frappe.db.set_value("Daily Task", {
        "employee": employee.name,
        "task_date": date,
        "rolled_over_from": ["is", "set"],
    }, "status", "Pending", update_modified=False)

    frappe.db.commit()
    return {"success": True}


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