import frappe


def execute():
    if not frappe.db.table_exists("Employee"):
        return

    if not frappe.db.has_column("Employee", "work_type"):
        return

    frappe.db.sql("""
        UPDATE `tabEmployee`
        SET work_type = 'Office'
        WHERE work_type IS NULL OR work_type = ''
    """)

    frappe.db.commit()
