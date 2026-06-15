"""
Scheduled email tasks for ST Attendance Tracker.
  10:30 AM IST Mon-Sat → send_employee_checkin_reminder  (to employees who haven't checked in)
  11:30 AM IST Mon-Sat → send_morning_combined_report    (to HR: missing + late summary)
  10:00 PM IST Mon-Sat → send_eod_missing_report         (to HR: employees without EOD)

HR reports go to all users with HR Manager role.
Employee reminders go directly to each employee's work email.
"""

import frappe
from frappe.utils import today, getdate, now_datetime


# ─────────────────────────────────────────────────────────────────────────────
# EMPLOYEE REMINDER — 10:30 AM
# ─────────────────────────────────────────────────────────────────────────────

def send_employee_checkin_reminder():
    """
    Sent at 10:30 AM IST Mon-Sat.
    Emails each employee individually who hasn't checked in today.
    """
    date = today()
    day_label = getdate(date).strftime("%A, %d %B %Y")

    expected = _get_expected_employees(date)
    if not expected:
        return

    emp_names = [e.name for e in expected]
    checked_in = {r.employee for r in frappe.get_all("Daily Task Log", filters={
        "date": date,
        "log_type": "Morning Check-In",
        "docstatus": 1,
        "employee": ["in", emp_names],
    }, fields=["employee"])}

    not_checked = [e for e in expected if e.name not in checked_in]
    if not not_checked:
        return

    for emp in not_checked:
        # Get employee's personal email or company email
        emp_email = frappe.db.get_value("Employee", emp.name,
            ["prefered_email", "company_email", "personal_email"], as_dict=True)

        email = (
            emp_email.get("prefered_email") or
            emp_email.get("company_email") or
            emp_email.get("personal_email")
        )

        # Also try user account email
        if not email:
            user_id = frappe.db.get_value("Employee", emp.name, "user_id")
            if user_id:
                email = frappe.db.get_value("User", user_id, "email") or user_id

        if not email:
            continue

        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
          <div style="background:#EE1C29;padding:20px 24px;border-radius:6px 6px 0 0">
            <h2 style="color:#fff;margin:0;font-size:18px">Check-In Reminder</h2>
            <p style="color:rgba(255,255,255,0.8);margin:5px 0 0;font-size:13px">
              StandardTouch e-Solutions · Attendance System
            </p>
          </div>
          <div style="background:#fff;padding:20px 24px;border:1px solid #e0e0e0;
                      border-top:none;border-radius:0 0 6px 6px">
            <p style="font-size:15px;color:#111;margin:0 0 10px">
              Hi <strong>{emp.employee_name}</strong>,
            </p>
            <p style="font-size:13px;color:#444;line-height:1.6;margin:0 0 16px">
              You have not checked in for today — <strong>{day_label}</strong>.
            </p>
            <p style="font-size:13px;color:#444;line-height:1.6;margin:0 0 20px">
              Please check in as soon as possible by visiting the attendance portal.
            </p>
            <a href="/daily-checkin"
               style="display:inline-block;background:#EE1C29;color:#fff;
                      padding:10px 22px;border-radius:6px;text-decoration:none;
                      font-size:13px;font-weight:500">
              Check In Now →
            </a>
          </div>
          <p style="font-size:11px;color:#aaa;text-align:center;margin-top:10px">
            Automated reminder · StandardTouch Attendance System · Do not reply
          </p>
        </div>"""

        frappe.sendmail(
            recipients=[email],
            subject=f"Reminder: You have not checked in today ({date})",
            message=html,
            now=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# MORNING HR REPORT — 11:30 AM
# ─────────────────────────────────────────────────────────────────────────────

def send_morning_combined_report():
    """
    Combined report: Section 1 = not checked in, Section 2 = checked in late.
    Sent at 11:30 AM IST Mon-Sat to all HR Manager role users.
    """
    date = today()
    expected = _get_expected_employees(date)
    if not expected:
        return

    emp_names = [e.name for e in expected]

    morning_logs = frappe.get_all("Daily Task Log", filters={
        "date": date,
        "log_type": "Morning Check-In",
        "docstatus": 1,
        "employee": ["in", emp_names],
    }, fields=["employee", "login_time", "is_late"])

    checked_in     = {l.employee for l in morning_logs}
    late_employees = [l for l in morning_logs if l.is_late]
    missing        = [e for e in expected if e.name not in checked_in]

    if not missing and not late_employees:
        return

    late_emp_map = {l.employee: str(l.login_time)[:5] for l in late_employees}

    missing_rows = ""
    for i, emp in enumerate(
        sorted(missing, key=lambda e: (e.department or "", e.employee_name)), 1
    ):
        bg = "#ffffff" if i % 2 == 0 else "#fafafa"
        missing_rows += f"""
        <tr style="background:{bg}">
          <td style="padding:8px 10px;border-bottom:1px solid #eee">{i}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;font-weight:500">{emp.employee_name}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;color:#666">{emp.department or "—"}</td>
        </tr>"""

    late_emp_objs = [e for e in expected if e.name in late_emp_map]
    late_rows = ""
    for i, emp in enumerate(
        sorted(late_emp_objs, key=lambda e: (e.department or "", e.employee_name)), 1
    ):
        bg = "#ffffff" if i % 2 == 0 else "#fffbeb"
        late_rows += f"""
        <tr style="background:{bg}">
          <td style="padding:8px 10px;border-bottom:1px solid #eee">{i}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;font-weight:500">{emp.employee_name}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;color:#666">{emp.department or "—"}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;color:#d97706;font-weight:500">{late_emp_map.get(emp.name, "—")}</td>
        </tr>"""

    missing_section = ""
    if missing:
        missing_section = f"""
        <h3 style="font-size:14px;color:#131419;margin:0 0 10px">
          Not checked in &nbsp;<span style="background:#fee2e2;color:#991b1b;
          font-size:12px;padding:2px 8px;border-radius:20px">{len(missing)}</span>
        </h3>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;border:1px solid #ddd;border-radius:4px;margin-bottom:20px">
          <thead>
            <tr style="background:#131419">
              <th style="padding:9px 10px;text-align:left;color:#fff;font-size:12px;width:36px">#</th>
              <th style="padding:9px 10px;text-align:left;color:#fff;font-size:12px">Employee</th>
              <th style="padding:9px 10px;text-align:left;color:#fff;font-size:12px">Department</th>
            </tr>
          </thead>
          <tbody>{missing_rows}</tbody>
        </table>"""

    late_section = ""
    if late_employees:
        late_section = f"""
        <h3 style="font-size:14px;color:#131419;margin:0 0 10px">
          Late check-ins &nbsp;<span style="background:#fef9c3;color:#854d0e;
          font-size:12px;padding:2px 8px;border-radius:20px">{len(late_employees)}</span>
        </h3>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;border:1px solid #ddd;border-radius:4px;margin-bottom:20px">
          <thead>
            <tr style="background:#131419">
              <th style="padding:9px 10px;text-align:left;color:#fff;font-size:12px;width:36px">#</th>
              <th style="padding:9px 10px;text-align:left;color:#fff;font-size:12px">Employee</th>
              <th style="padding:9px 10px;text-align:left;color:#fff;font-size:12px">Department</th>
              <th style="padding:9px 10px;text-align:left;color:#fff;font-size:12px">Check-In Time</th>
            </tr>
          </thead>
          <tbody>{late_rows}</tbody>
        </table>"""

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto">
      <div style="background:#EE1C29;padding:20px 24px;border-radius:6px 6px 0 0">
        <h2 style="color:#fff;margin:0;font-size:18px">Morning Attendance Report</h2>
        <p style="color:rgba(255,255,255,0.8);margin:5px 0 0;font-size:13px">
          As of 11:30 AM &nbsp;·&nbsp; {date} &nbsp;·&nbsp; StandardTouch e-Solutions
        </p>
      </div>
      <div style="background:#fff;padding:20px 24px;border:1px solid #e0e0e0;
                  border-top:none;border-radius:0 0 6px 6px">
        {missing_section}
        {late_section}
      </div>
      <p style="font-size:11px;color:#aaa;text-align:center;margin-top:10px">
        Automated report · StandardTouch Attendance System · Do not reply
      </p>
    </div>"""

    _send_to_hr_managers(
        subject=f"[{date}] Morning Report — {len(missing)} missing, {len(late_employees)} late",
        message=html,
    )


# ─────────────────────────────────────────────────────────────────────────────
# EOD HR REPORT — 10:00 PM
# ─────────────────────────────────────────────────────────────────────────────

def send_eod_missing_report():
    """EOD missing report sent at 10:00 PM IST Mon-Sat to all HR Manager role users."""
    date = today()
    expected = _get_expected_employees(date)
    if not expected:
        return

    submitted = {r.employee for r in frappe.get_all("Daily Task Log", filters={
        "date": date,
        "log_type": "End of Day",
        "docstatus": 1,
    }, fields=["employee"])}

    missing = [e for e in expected if e.name not in submitted]
    if not missing:
        return

    rows = ""
    for i, emp in enumerate(
        sorted(missing, key=lambda e: (e.department or "", e.employee_name)), 1
    ):
        bg = "#ffffff" if i % 2 == 0 else "#fafafa"
        rows += f"""
        <tr style="background:{bg}">
          <td style="padding:8px 10px;border-bottom:1px solid #eee">{i}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;font-weight:500">{emp.employee_name}</td>
          <td style="padding:8px 10px;border-bottom:1px solid #eee;color:#666">{emp.department or "—"}</td>
        </tr>"""

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto">
      <div style="background:#131419;padding:20px 24px;border-radius:6px 6px 0 0">
        <h2 style="color:#fff;margin:0;font-size:18px">EOD Report Missing</h2>
        <p style="color:rgba(255,255,255,0.6);margin:5px 0 0;font-size:13px">
          As of 10:00 PM &nbsp;·&nbsp; {date} &nbsp;·&nbsp; StandardTouch e-Solutions
        </p>
      </div>
      <div style="background:#fff;padding:20px 24px;border:1px solid #e0e0e0;
                  border-top:none;border-radius:0 0 6px 6px">
        <p style="font-size:13px;color:#333;margin:0 0 14px">
          The following employees have not submitted their End-of-Day report.
        </p>
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;border:1px solid #ddd;border-radius:4px">
          <thead>
            <tr style="background:#131419">
              <th style="padding:9px 10px;text-align:left;color:#fff;font-size:12px;width:36px">#</th>
              <th style="padding:9px 10px;text-align:left;color:#fff;font-size:12px">Employee</th>
              <th style="padding:9px 10px;text-align:left;color:#fff;font-size:12px">Department</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        <p style="margin:14px 0 0;font-size:13px;color:#444">
          Total missing: <strong>{len(missing)}</strong>
        </p>
      </div>
      <p style="font-size:11px;color:#aaa;text-align:center;margin-top:10px">
        Automated report · StandardTouch Attendance System · Do not reply
      </p>
    </div>"""

    _send_to_hr_managers(
        subject=f"[{date}] EOD Report Missing — {len(missing)} employee(s)",
        message=html,
    )


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_expected_employees(date):
    """Active employees excluding those on approved leave or holiday today."""
    all_employees = frappe.get_all("Employee",
        filters={"status": "Active"},
        fields=["name", "employee_name", "department", "holiday_list"],
    )
    on_leave = {r.employee for r in frappe.get_all("Leave Application", filters={
        "from_date": ["<=", date],
        "to_date":   [">=", date],
        "status":    "Approved",
        "docstatus": 1,
    }, fields=["employee"])}

    result = []
    for emp in all_employees:
        if emp.name in on_leave:
            continue
        if _is_holiday(emp.holiday_list, date):
            continue
        result.append(emp)
    return result


def _is_holiday(holiday_list_name, date):
    if not holiday_list_name:
        return False
    return bool(frappe.db.exists("Holiday", {
        "parent":       holiday_list_name,
        "holiday_date": getdate(date),
    }))


def _send_to_hr_managers(subject, message):
    """
    Fetch all enabled users with HR Manager role and send email.
    Uses SQL to reliably get the recipients.
    """
    recipients = frappe.db.sql("""
        SELECT DISTINCT u.email
        FROM `tabUser` u
        INNER JOIN `tabHas Role` hr ON hr.parent = u.name
        WHERE hr.role = 'HR Manager'
          AND u.enabled = 1
          AND u.email IS NOT NULL
          AND u.email != ''
    """, as_dict=True)

    emails = [r.email for r in recipients if r.email]

    if not emails:
        frappe.log_error(
            "ST Attendance Tracker: No users with HR Manager role found. "
            "Report not sent. Please assign HR Manager role to at least one user.",
            "ST Attendance Tracker — Report"
        )
        return

    frappe.sendmail(
        recipients=emails,
        subject=subject,
        message=message,
        now=True,
    )