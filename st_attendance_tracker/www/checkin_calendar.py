"""Daily Check-in, calendar layout. Served at /daily-checkin (the previous page stays at /daily-checkin-classic)."""
import frappe
from frappe.utils import getdate, today

from st_attendance_tracker.api import (
    _get_attendance_settings,
    _is_half_day_leave_today,
    _is_team_leader,
    _resolve_active_checkin_date,
    _to_hhmm,
)
from st_attendance_tracker.ui_shell import get_shell_context, redirect_for_design
from st_attendance_tracker.www.daily_checkin import _get_work_location_config


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/daily-checkin"
        raise frappe.Redirect

    redirect_for_design()

    # Management has no Employee record and never checks in.
    if "Management" in frappe.get_roles(frappe.session.user):
        frappe.local.flags.redirect_location = "/management-dashboard"
        raise frappe.Redirect

    employee = frappe.db.get_value(
        "Employee",
        {"user_id": frappe.session.user, "status": "Active"},
        ["name", "employee_name", "department", "reports_to", "work_type"],
        as_dict=True,
    )
    if not employee:
        frappe.throw(
            "Your account is not linked to an Employee record. "
            "Please contact HR or System Administrator."
        )

    # The open check-in may be yesterday's (forgot to check out): checkout then acts on that date.
    active_date = str(getdate(_resolve_active_checkin_date(employee.name)))
    todays_date = today()

    context.no_cache = 1
    context.employee = employee
    context.title = "Daily Check-In"
    context.st_shell = get_shell_context("checkin", employee)
    context.st_shell["menu_links"] = [
        {"label": "Classic check-in", "href": "/daily-checkin-classic", "icon": "ti-layout-list", "switch": "classic"},
    ]
    context.boot = {
        "employee": {"name": employee.name, "employee_name": employee.employee_name},
        "today": todays_date,
        "active_date": active_date,
        "is_late_checkout": active_date != todays_date,
        "location_config": _get_work_location_config(employee, getdate(active_date)),
        "half_day_leave": bool(_is_half_day_leave_today(employee.name, todays_date)),
        "is_team_leader": _is_team_leader(employee.name),
        "late_after": _to_hhmm(_get_attendance_settings().get("late_checkin_threshold")) or "09:35",
    }
