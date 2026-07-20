import frappe
from frappe.utils import today


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/management-dashboard"
        raise frappe.Redirect

    user_roles = frappe.get_roles(frappe.session.user)
    if "HR Manager" not in user_roles:
        frappe.local.flags.redirect_location = "/daily-checkin"
        raise frappe.Redirect

    employee = frappe.db.get_value(
        "Employee", {"user_id": frappe.session.user, "status": "Active"},
        ["name", "employee_name", "department"], as_dict=True,
    )
    if not employee:
        full_name = frappe.db.get_value("User", frappe.session.user, "full_name") or "HR Manager"
        employee = frappe._dict({
            "name": "",
            "employee_name": full_name,
            "department": ""
        })

    context.no_cache = 1
    context.employee = employee
    context.date = today()
    context.title = "Management Dashboard"
