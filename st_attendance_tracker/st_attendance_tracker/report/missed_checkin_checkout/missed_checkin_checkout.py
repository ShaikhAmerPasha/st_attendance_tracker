import frappe
from frappe import _
from frappe.utils import getdate, today

from st_attendance_tracker.api import _to_ampm
from st_attendance_tracker.tasks import _get_expected_employees


def execute(filters=None):
    filters = frappe._dict(filters or {})
    date = getdate(filters.get("date") or today())

    columns = get_columns()
    data = get_data(date, filters.get("department"))
    return columns, data


def get_columns():
    return [
        {"label": _("Employee"), "fieldname": "employee", "fieldtype": "Link",
         "options": "Employee", "width": 130},
        {"label": _("Employee Name"), "fieldname": "employee_name", "fieldtype": "Data", "width": 180},
        {"label": _("Department"), "fieldname": "department", "fieldtype": "Link",
         "options": "Department", "width": 180},
        {"label": _("Missed Check-in"), "fieldname": "missed_checkin", "fieldtype": "Data", "width": 120},
        {"label": _("Missed Check-out"), "fieldname": "missed_checkout", "fieldtype": "Data", "width": 130},
        {"label": _("Check-in Time"), "fieldname": "login_time", "fieldtype": "Data", "width": 110},
        {"label": _("Check-out Time"), "fieldname": "logout_time", "fieldtype": "Data", "width": 110},
    ]


def get_data(date, department=None):
    """One row per employee who is expected to check in/out on `date` (per
    _get_expected_employees — Active, not on approved leave/Attendance "On
    Leave", not on a pending/draft leave request either, not a holiday, and
    not a User holding the "Management" role) but hasn't checked in and/or
    hasn't checked out.
    """
    expected = _get_expected_employees(date)
    if department:
        expected = [e for e in expected if e.department == department]
    if not expected:
        return []

    emp_names = [e.name for e in expected]
    work_logs = frappe.get_all("Daily Work Log", filters={
        "date": date, "employee": ["in", emp_names],
    }, fields=["employee", "morning_submitted", "eod_submitted", "login_time", "logout_time"])
    work_log_by_emp = {w.employee: w for w in work_logs}

    rows = []
    for emp in sorted(expected, key=lambda e: (e.department or "", e.employee_name)):
        work_log = work_log_by_emp.get(emp.name)
        checked_in = bool(work_log and work_log.morning_submitted)
        checked_out = bool(work_log and work_log.eod_submitted)
        if checked_in and checked_out:
            continue

        rows.append({
            "employee": emp.name,
            "employee_name": emp.employee_name,
            "department": emp.department,
            "missed_checkin": _("No") if checked_in else _("Yes"),
            "missed_checkout": _("No") if checked_out else _("Yes"),
            "login_time": _to_ampm(work_log.login_time) if work_log and work_log.login_time else "—",
            "logout_time": _to_ampm(work_log.logout_time) if work_log and work_log.logout_time else "—",
        })
    return rows
