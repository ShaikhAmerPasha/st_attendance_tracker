import frappe
from frappe.model.document import Document


class TaskEntry(Document):
    pass


def on_doctype_update():
    # Rollover status cascade (Section 4.2) does UPDATE ... WHERE series_id = %s
    # across every copy of a task's lineage — this replaces the old
    # rolled_over_from chain walk with a single indexed query.
    frappe.db.add_index("Task Entry", ["series_id"])
