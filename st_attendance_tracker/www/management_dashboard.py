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

    context.no_cache = 1
    context.date = today()
    context.title = "Management Dashboard"
