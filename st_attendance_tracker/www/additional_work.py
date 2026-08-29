import frappe
from frappe.utils import today


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/additional-work"
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
    context.date = today()
    context.title = "Additional Work"
