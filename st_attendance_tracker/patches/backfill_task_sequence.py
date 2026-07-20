import frappe


def execute():
    if not frappe.db.table_exists("Daily Task"):
        return

    if not frappe.db.has_column("Daily Task", "sequence"):
        return

    groups = frappe.db.sql("""
        SELECT employee, task_date
        FROM `tabDaily Task`
        WHERE sequence = 0 OR sequence IS NULL
        GROUP BY employee, task_date
    """, as_dict=True)

    for group in groups:
        task_names = frappe.db.sql("""
            SELECT name
            FROM `tabDaily Task`
            WHERE employee = %s AND task_date = %s
            ORDER BY creation ASC
        """, (group.employee, group.task_date))

        for i, (name,) in enumerate(task_names):
            frappe.db.set_value("Daily Task", name, "sequence", i + 1, update_modified=False)

    frappe.db.commit()
