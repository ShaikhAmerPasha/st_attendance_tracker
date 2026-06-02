import frappe
from frappe.utils import today, getdate


def send_morning_missing_report():
    date = today()
    expected = _get_expected_employees(date)
    if not expected:
        return
    submitted = {
        r.employee for r in frappe.get_all(
            "Daily Task Log",
            filters={"date": date, "log_type": "Morning Check-In", "docstatus": 1},
            fields=["employee"],
        )
    }
    missing = [e for e in expected if e.name not in submitted]
    if not missing:
        return
    _send_report_email(
        subject=f"[{date}] Morning Check-In Missing — {len(missing)} Employee(s)",
        title="Morning Check-In Missing",
        subtitle=f"As of 11:30 AM on {date}, the following employees have not submitted their Morning Check-In & Tasks:",
        employees=missing,
        footer_note="Please follow up with them directly.",
    )


def send_eod_missing_report():
    date = today()
    expected = _get_expected_employees(date)
    if not expected:
        return
    submitted = {
        r.employee for r in frappe.get_all(
            "Daily Task Log",
            filters={"date": date, "log_type": "End of Day", "docstatus": 1},
            fields=["employee"],
        )
    }
    missing = [e for e in expected if e.name not in submitted]
    if not missing:
        return
    _send_report_email(
        subject=f"[{date}] EOD Report Missing — {len(missing)} Employee(s)",
        title="End-of-Day Report Missing",
        subtitle=f"As of 10:00 PM on {date}, the following employees have NOT submitted their End-of-Day report:",
        employees=missing,
        footer_note="Pending tasks will NOT roll over to tomorrow until EOD is submitted.",
    )


def _get_expected_employees(date):
    all_employees = frappe.get_all(
        "Employee",
        filters={"status": "Active"},
        fields=["name", "employee_name", "department", "holiday_list"],
    )
    on_leave = {
        r.employee for r in frappe.get_all(
            "Leave Application",
            filters={
                "from_date": ["<=", date],
                "to_date": [">=", date],
                "status": "Approved",
                "docstatus": 1,
            },
            fields=["employee"],
        )
    }
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
    return bool(frappe.db.exists(
        "Holiday",
        {"parent": holiday_list_name, "holiday_date": getdate(date)},
    ))


def _send_report_email(subject, title, subtitle, employees, footer_note):
    try:
        settings = frappe.get_single("ST Attendance Settings")
        recipients = [r.email for r in settings.report_recipients if r.email]
    except Exception:
        recipients = []
    if not recipients:
        frappe.log_error("ST Attendance Settings has no report recipients.", "ST Attendance Tracker")
        return

    employees_sorted = sorted(employees, key=lambda e: (e.department or "", e.employee_name))
    rows_html = ""
    current_dept = None
    row_num = 0
    for emp in employees_sorted:
        if emp.department != current_dept:
            current_dept = emp.department
            rows_html += f"<tr style='background:#f0f0f0;'><td colspan='3' style='padding:6px 10px;font-weight:600;font-size:12px;color:#555;'>{current_dept or 'No Department'}</td></tr>"
        row_num += 1
        bg = "#ffffff" if row_num % 2 == 0 else "#fafafa"
        rows_html += f"<tr style='background:{bg};'><td style='padding:8px 10px;border-bottom:1px solid #eee;'>{row_num}</td><td style='padding:8px 10px;border-bottom:1px solid #eee;font-weight:500;'>{emp.employee_name}</td><td style='padding:8px 10px;border-bottom:1px solid #eee;color:#666;'>{emp.department or '—'}</td></tr>"

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
      <div style="background:#EE1C29;padding:20px 24px;border-radius:6px 6px 0 0;">
        <h2 style="color:#fff;margin:0;font-size:18px;">{title}</h2>
        <p style="color:rgba(255,255,255,0.85);margin:6px 0 0;font-size:13px;">StandardTouch e-Solutions — Attendance System</p>
      </div>
      <div style="background:#fff;padding:20px 24px;border:1px solid #e0e0e0;border-top:none;border-radius:0 0 6px 6px;">
        <p style="font-size:14px;color:#333;margin:0 0 16px;">{subtitle}</p>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #ddd;">
          <thead><tr style="background:#131419;">
            <th style="padding:10px;text-align:left;color:#fff;font-size:13px;width:40px;">#</th>
            <th style="padding:10px;text-align:left;color:#fff;font-size:13px;">Employee Name</th>
            <th style="padding:10px;text-align:left;color:#fff;font-size:13px;">Department</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
        <p style="margin:16px 0 0;font-size:13px;color:#444;"><strong>Total Missing: {len(employees)}</strong></p>
        <p style="margin:8px 0 0;font-size:12px;color:#888;">{footer_note}</p>
      </div>
      <p style="font-size:11px;color:#aaa;text-align:center;margin-top:12px;">Automated message from StandardTouch Attendance System. Do not reply.</p>
    </div>"""

    frappe.sendmail(recipients=recipients, subject=subject, message=html, now=True)
