import frappe
from frappe.utils import today


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/team-dashboard"
        raise frappe.Redirect

    employee = frappe.db.get_value(
        "Employee", {"user_id": frappe.session.user, "status": "Active"},
        ["name", "employee_name", "department"], as_dict=True,
    )
    if not employee:
        frappe.local.flags.redirect_location = "/daily-checkin"
        raise frappe.Redirect

    is_tl = bool(frappe.db.exists("Employee", {
        "reports_to": employee.name, "status": "Active"
    }))
    if not is_tl:
        frappe.local.flags.redirect_location = "/daily-checkin"
        raise frappe.Redirect

    context.no_cache = 1
    context.employee = employee
    context.date = today()
    context.title = "Team Dashboard"
