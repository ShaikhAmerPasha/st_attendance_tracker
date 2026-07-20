import frappe


def execute():
    if not frappe.db.table_exists("Daily Task Log"):
        return

    if not frappe.db.has_column("Daily Task Log", "work_location"):
        return

    frappe.db.sql("""
        UPDATE `tabDaily Task Log`
        SET work_location = 'Office'
        WHERE (work_location IS NULL OR work_location = '')
        AND log_type = 'Morning Check-In'
    """)

    frappe.db.commit()
