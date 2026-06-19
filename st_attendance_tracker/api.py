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

        late_flag = frappe.db.get_value("Daily Task Log",
            {"employee": employee_name, "date": date,
             "log_type": "Morning Check-In", "docstatus": 1}, "is_late")
        late_badge = (
            '<span style="background:#fef9c3;color:#854d0e;font-size:11px;'
            'padding:2px 8px;border-radius:20px;margin-left:8px">Late</span>'
            if late_flag else ""
        )
        wl_badge = ""
        if work_location and work_location != "Office":
            wl_badge = (
                f'<span style="background:#f3f4f6;color:#374151;font-size:11px;'
                f'padding:2px 8px;border-radius:20px;margin-left:8px">{work_location}</span>'
            )

        # Build task list HTML grouped by project
        projects = {}
        standalone = []
        for t in tasks:
            pname = (t.get("project_name") or "").strip()
            if pname:
                projects.setdefault(pname, []).append(t)
            else:
                standalone.append(t)

        task_html = ""
        proj_icons = ["📁", "📂", "🗂️", "📋"]

        for i, (pname, ptasks) in enumerate(projects.items()):
            icon = proj_icons[i % len(proj_icons)]
            rows = ""
            for t in ptasks:
                est = f'<span style="color:#9ca3af;font-size:11px;margin-left:8px">Est: {t["estimated_time"]}</span>' if t.get("estimated_time") else ""
                carried = '<span style="background:#fef3c7;color:#92400e;font-size:10px;padding:1px 6px;border-radius:10px;margin-left:6px">Carried</span>' if t.get("rolled_over_from") else ""
                rows += f'''
                <tr>
                  <td style="padding:7px 10px;border-bottom:1px solid #f9fafb;font-size:13px;color:#374151">
                    {t["description"]}{carried}{est}
                  </td>
                </tr>'''
            task_html += f'''
            <div style="margin-bottom:14px">
              <div style="font-size:12px;font-weight:600;color:#111;margin-bottom:6px">{icon} {pname}</div>
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border-collapse:collapse;background:#f9fafb;border-radius:6px;overflow:hidden">
                {rows}
              </table>
            </div>'''

        for t in standalone:
            est = f'<span style="color:#9ca3af;font-size:11px;margin-left:8px">Est: {t["estimated_time"]}</span>' if t.get("estimated_time") else ""
            carried = '<span style="background:#fef3c7;color:#92400e;font-size:10px;padding:1px 6px;border-radius:10px;margin-left:6px">Carried</span>' if t.get("rolled_over_from") else ""
            task_html += f'''
            <div style="padding:7px 10px;background:#f9fafb;border-radius:6px;
                        font-size:13px;color:#374151;margin-bottom:6px">
              📌 {t["description"]}{carried}{est}
            </div>'''

        total_tasks = len(tasks)
        carried_count = sum(1 for t in tasks if t.get("rolled_over_from"))
        est_total = sum(
            int(t["estimated_time"].replace("hr","").replace("h","").strip())
            for t in tasks
            if t.get("estimated_time") and t["estimated_time"].replace("hr","").replace("h","").strip().isdigit()
        )

        # no_tasks_msg = '<p style="font-size:13px;color:#9ca3af;font-style:italic">No tasks added yet.</p>' if not tasks else ""
        no_tasks_msg = '<p style="font-size:13px;color:#9ca3af;font-style:italic">No tasks added yet.</p>' if not tasks else ""

        carried_span = (
            '<span>🔄 <strong style="color:#92400e">' + str(carried_count) + '</strong> carried</span>'
        ) if carried_count else ""
        est_span = (
            '<span>⏱ <strong style="color:#111">' + str(est_total) + 'h</strong> estimated</span>'
        ) if est_total else ""


        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
          <div style="background:#EE1C29;padding:18px 22px;border-radius:6px 6px 0 0">
            <h2 style="color:#fff;margin:0;font-size:17px">
              ✅ You're checked in {late_badge}{wl_badge}
            </h2>
            <p style="color:rgba(255,255,255,.75);margin:5px 0 0;font-size:12px">
              StandardTouch e-Solutions · {date}
            </p>
          </div>
          <div style="background:#fff;padding:18px 22px;border:1px solid #e5e7eb;border-top:none">
            <p style="font-size:14px;color:#111;margin:0 0 6px">
              Hi <strong>{employee_display_name}</strong>,
            </p>
            <p style="font-size:13px;color:#6b7280;margin:0 0 18px;line-height:1.5">
              Your check-in has been recorded at <strong>{str(login_time)[:5]}</strong>.
              Here's your plan for today:
            </p>

            <div style="border-top:2px solid #f3f4f6;padding-top:14px;margin-bottom:14px">
              {task_html or no_tasks_msg}
            </div>

            # <div style="background:#f9fafb;border-radius:8px;padding:10px 14px;
            #             display:flex;gap:16px;font-size:12px;color:#6b7280">
            #   <span>📊 <strong style="color:#111">{total_tasks}</strong> tasks planned</span>
            #   {"<span>🔄 <strong style=\"color:#92400e\">" + str(carried_count) + "</strong> carried</span>" if carried_count else ""}
            #   {"<span>⏱ <strong style=\"color:#111\">" + str(est_total) + "h</strong> estimated</span>" if est_total else ""}
            # </div>

            <div style="background:#f9fafb;border-radius:8px;padding:10px 14px;
                        display:flex;gap:16px;font-size:12px;color:#6b7280">
              <span>📊 <strong style="color:#111">{total_tasks}</strong> tasks planned</span>
              {carried_span}
              {est_span}
            </div>
          </div>
          <p style="font-size:11px;color:#aaa;text-align:center;margin-top:8px">
            Automated confirmation · StandardTouch Attendance System · Do not reply
          </p>
        </div>"""

        frappe.sendmail(
            recipients=[emp_email],
            subject=f"Checked in at {str(login_time)[:5]} — Your plan for {date}",
            message=html,
            now=True,
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
        adhoc_tasks   = [t for t in done_tasks if t.get("task_type") == "Ad-hoc"]

        def task_row(t, color="#15803d", icon="✅"):
            est  = f'<span style="color:#9ca3af;font-size:11px;margin-left:8px">Est: {t["estimated_time"]}</span>' if t.get("estimated_time") else ""
            act  = f'<span style="color:#2563eb;font-size:11px;margin-left:6px">Actual: {t["actual_time"]}</span>' if t.get("actual_time") else ""
            proj = f'<span style="color:#9ca3af;font-size:10px;margin-left:6px">[{t["project_name"]}]</span>' if t.get("project_name") else ""
            return f'''<tr>
              <td style="padding:7px 10px;border-bottom:1px solid #f9fafb;font-size:13px;color:#374151">
                {icon} {t["description"]}{proj}{est}{act}
              </td>
            </tr>'''

        done_html = ""
        if done_tasks:
            rows = "".join(task_row(t, icon="✅") for t in done_tasks)
            done_html = f'''
            <div style="margin-bottom:16px">
              <div style="font-size:12px;font-weight:600;color:#15803d;margin-bottom:6px">
                ✅ Completed ({len(done_tasks)})
              </div>
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border-collapse:collapse;background:#f0fdf4;border-radius:6px;overflow:hidden">
                {rows}
              </table>
            </div>'''

        pending_html = ""
        if pending_tasks:
            rows = "".join(task_row(t, icon="🔄") for t in pending_tasks)
            pending_html = f'''
            <div style="margin-bottom:16px">
              <div style="font-size:12px;font-weight:600;color:#d97706;margin-bottom:6px">
                🔄 Carried to tomorrow ({len(pending_tasks)})
              </div>
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="border-collapse:collapse;background:#fffbeb;border-radius:6px;overflow:hidden">
                {rows}
              </table>
            </div>'''

        pct = int(len(done_tasks) / len(tasks) * 100) if tasks else 0
        bar_color = "#15803d" if pct >= 80 else "#d97706" if pct >= 50 else "#EE1C29"

        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
          <div style="background:#0f172a;padding:18px 22px;border-radius:6px 6px 0 0">
            <h2 style="color:#fff;margin:0;font-size:17px">
              🌙 End of Day submitted
            </h2>
            <p style="color:rgba(255,255,255,.5);margin:5px 0 0;font-size:12px">
              StandardTouch e-Solutions · {date}
            </p>
          </div>
          <div style="background:#fff;padding:18px 22px;border:1px solid #e5e7eb;border-top:none">
            <p style="font-size:14px;color:#111;margin:0 0 6px">
              Hi <strong>{employee_display_name}</strong>,
            </p>
            <p style="font-size:13px;color:#6b7280;margin:0 0 16px;line-height:1.5">
              You checked out at <strong>{str(logout_time)[:5]}</strong>.
              Here's your day summary:
            </p>

            <div style="background:#f9fafb;border-radius:8px;padding:12px 16px;
                        margin-bottom:18px;display:grid;grid-template-columns:1fr 1fr 1fr">
              <div style="text-align:center">
                <div style="font-size:22px;font-weight:600;color:#111">{len(done_tasks)}/{len(tasks)}</div>
                <div style="font-size:11px;color:#9ca3af;margin-top:2px">Tasks done</div>
              </div>
              <div style="text-align:center;border-left:1px solid #e5e7eb;border-right:1px solid #e5e7eb">
                <div style="font-size:22px;font-weight:600;color:#15803d">{net_hours or "—"}</div>
                <div style="font-size:11px;color:#9ca3af;margin-top:2px">Net hours</div>
              </div>
              <div style="text-align:center">
                <div style="font-size:22px;font-weight:600;color:#d97706">{len(pending_tasks)}</div>
                <div style="font-size:11px;color:#9ca3af;margin-top:2px">Carried fwd</div>
              </div>
            </div>

            <div style="background:#f9fafb;border-radius:6px;padding:8px 12px;margin-bottom:18px">
              <div style="font-size:11px;color:#9ca3af;margin-bottom:5px">Completion</div>
              <div style="background:#e5e7eb;border-radius:4px;height:8px;overflow:hidden">
                <div style="width:{pct}%;background:{bar_color};height:100%;border-radius:4px"></div>
              </div>
              <div style="font-size:11px;color:#6b7280;margin-top:4px">{pct}% complete</div>
            </div>

            <div style="border-top:2px solid #f3f4f6;padding-top:14px">
              {done_html}
              {pending_html}
            </div>
          </div>
          <p style="font-size:11px;color:#aaa;text-align:center;margin-top:8px">
            Automated confirmation · StandardTouch Attendance System · Do not reply
          </p>
        </div>"""

        frappe.sendmail(
            recipients=[emp_email],
            subject=f"Day complete — {len(done_tasks)}/{len(tasks)} tasks done | {net_hours} worked | {date}",
            message=html,
            now=True,
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

        task_mode = "checkin" if event == "checkin" else "checkout"
        task_html = _render_compact_task_rows(tasks or [], mode=task_mode)
        task_label = "Today's tasks" if event == "checkin" else "Task summary"

        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto">
          <div style="background:#EE1C29;padding:16px 20px;border-radius:6px 6px 0 0">
            <h2 style="color:#fff;margin:0;font-size:16px">{subject}</h2>
          </div>
          <div style="background:#fff;padding:16px 20px;border:1px solid #e0e0e0;
                      border-top:none;border-radius:0 0 6px 6px">
            <p style="font-size:14px;color:#333;margin:0 0 10px">{details}</p>
            <div style="border-top:1px solid #f3f4f6;padding-top:10px">
              <div style="font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.4px">{task_label}</div>
              {task_html}
            </div>
            <p style="font-size:12px;color:#999;margin:14px 0 0">
              StandardTouch Attendance System &nbsp;·&nbsp; {frappe.utils.today()}
            </p>
          </div>
        </div>"""

        frappe.sendmail(
            recipients=recipients,
            subject=subject,
            message=html,
            now=True,
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
        current = parent
        depth += 1
    return current


# ── Rollover on EOD ────────────────────────────────────────────────────────────

def _rollover_pending_tasks(employee, date):
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
    missed = frappe.get_all("Daily Task", filters={
        "employee": employee,
        "task_date": ["<", today_date],
        "status": ["in", ["Pending", "In Progress"]],
    }, fields=["name", "description", "task_type", "origin_date",
               "remarks", "task_date", "estimated_time", "project_name"])

    for task in missed:
        root = _get_root_task(task.name)
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
        new_task.project_name     = task.project_name or ""
        new_task.project_name    = task.project_name or ""
        new_task.remarks = f"[Auto-carried from {task.origin_date or task.task_date}]"
        new_task.insert(ignore_permissions=True)

    frappe.db.commit()


# ── Working hours calculation ──────────────────────────────────────────────────

def _calc_net_hours(login_time, logout_time, lunch_from, lunch_to, date_str):
    """
    Calculates net working hours = (logout - login) - lunch duration.

    IMPORTANT: uses total_seconds(), not .seconds. timedelta.seconds only
    returns the seconds *within the current day component* and silently
    wraps/corrupts whenever the delta is negative (e.g. logout time stored
    a moment earlier than login due to timezone drift, stray date strings,
    or an empty-but-not-None lunch field) — producing nonsensical results
    like a 22-23 hour "workday" instead of erroring out.
    """
    try:
        base = str(date_str) + " "
        login_dt  = get_datetime(base + str(login_time))
        logout_dt = get_datetime(base + str(logout_time))
        total_mins = int((logout_dt - login_dt).total_seconds() / 60)

        lunch_mins = 0
        if lunch_from and lunch_to:
            lf = get_datetime(base + str(lunch_from))
            lt = get_datetime(base + str(lunch_to))
            lunch_mins = int((lt - lf).total_seconds() / 60)
            # Invalid/reversed lunch entry — ignore rather than let it corrupt net hours
            if lunch_mins < 0 or lunch_mins > 240:
                lunch_mins = 0

        net = total_mins - lunch_mins

        # Sanity ceiling — a workday can't sensibly exceed 18 hours.
        # If it does, something upstream (bad time string, day mismatch) is
        # wrong; surface that as empty rather than display a misleading number.
        if net < 0 or net > 18 * 60:
            frappe.log_error(
                f"Suspicious net hours calc: login={login_time} logout={logout_time} "
                f"lunch_from={lunch_from} lunch_to={lunch_to} date={date_str} "
                f"-> total_mins={total_mins} lunch_mins={lunch_mins} net={net}",
                "ST Attendance Tracker — net hours sanity check"
            )
            return ""

        return f"{net // 60}h {net % 60}m"
    except Exception:
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

    # Hybrid employees have routine WFH days (every day except their
    # configured office days). No Attendance Request needed on those days.
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
        "login_time":     str(login_time_val)[:5] if login_time_val else "",
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

    # Hybrid employees have routine WFH days (every day except their
    # configured office days, e.g. Tue/Thu). On those days WFH is expected
    # and does NOT require an Attendance Request — same rule as the
    # check-in page (_get_work_location_config in daily_checkin.py).
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
        f"{employee.employee_name} checked in at {str(actual_login)[:5]}{late_text}{wfh_text} on {date}.",
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

    return {"success": True, "login_time": str(actual_login)[:5]}


# ── EOD submit ─────────────────────────────────────────────────────────────────

@frappe.whitelist()
def submit_eod_log(lunch_from, lunch_to, logout_time, task_updates, adhoc_tasks):
    employee = _get_employee()
    date = today()

    if not logout_time:
        frappe.throw("Logout time is required.")

    if frappe.db.exists("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "End of Day", "docstatus": 1,
    }):
        frappe.throw("You have already submitted End of Day for today.")

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
        task_doc.actual_time = t.get("actual_time", "")
        task_doc.remarks     = t.get("remarks", "")
        task_doc.insert(ignore_permissions=True)

    # Get login time for net hours calculation
    morning_log_name = frappe.db.get_value("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "Morning Check-In", "docstatus": 1,
    }, "name")
    login_time = ""
    if morning_log_name:
        login_time = frappe.db.get_value(
            "Daily Task Log", morning_log_name, "login_time"
        ) or ""

    net_hours = _calc_net_hours(
        login_time, logout_time, lunch_from, lunch_to, date
    ) if login_time else ""

    log = frappe.new_doc("Daily Task Log")
    log.employee    = employee.name
    log.date        = date
    log.log_type    = "End of Day"
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
        f"{employee.employee_name} submitted EOD at {str(logout_time)[:5]}. "
        f"{done_count}/{total_count} tasks done. Net hours: {net_hours or '—'}.",
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
        "logout_time":   str(logout_time)[:5],
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
               "origin_date", "remarks"],
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
            "login_time":    str(morning.login_time)[:5]  if morning and morning.login_time  else "",
            "logout_time":   str(eod.logout_time)[:5]     if eod     and eod.logout_time     else "",
            "net_hours":     eod.net_hours                if eod                              else "",
            "is_late":       bool(morning.is_late)        if morning                          else False,
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