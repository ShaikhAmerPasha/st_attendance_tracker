import frappe


def execute():
    if not frappe.db.table_exists("Employee"):
        return

    if not frappe.db.exists("Custom Field", "Employee-department_assignments"):
        return

    employees = frappe.get_all("Employee",
        filters={"department": ["is", "set"], "reports_to": ["is", "set"]},
        fields=["name"])

    for emp in employees:
        doc = frappe.get_doc("Employee", emp.name)
        if doc.get("department_assignments"):
            continue
        doc.append("department_assignments", {
            "department": doc.department,
            "team_leader": doc.reports_to,
        })
        doc.save(ignore_permissions=True)

    frappe.db.commit()
