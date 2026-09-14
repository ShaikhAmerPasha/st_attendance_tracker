import frappe
from st_attendance_tracker.api import _is_team_leader


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/task-backlog"
        raise frappe.Redirect

    employee = frappe.db.get_value(
        "Employee", {"user_id": frappe.session.user, "status": "Active"},
        ["name", "employee_name", "department"], as_dict=True,
    )
    if not employee:
        frappe.throw(
            "Your account is not linked to an Employee record. "
            "Please contact HR."
        )

    context.no_cache = 1
    context.employee = employee
    context.title = "Task Backlog"
    context.is_team_leader = _is_team_leader(employee.name)
    context.is_hr_manager = "HR Manager" in frappe.get_roles(frappe.session.user)
    context.recurring_count = frappe.db.count(
        "Recurring Task Template", {"employee": employee.name, "is_active": 1}
    )
    context.backlog_count = frappe.db.count(
        "Task Backlog Item", {"employee": employee.name}
    )
