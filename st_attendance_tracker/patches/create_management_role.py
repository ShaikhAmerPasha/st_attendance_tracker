import frappe


def execute():
    if frappe.db.exists("Role", "Management"):
        return

    frappe.get_doc({
        "doctype": "Role",
        "role_name": "Management",
        "desk_access": 0,
    }).insert(ignore_permissions=True)
    frappe.db.commit()
