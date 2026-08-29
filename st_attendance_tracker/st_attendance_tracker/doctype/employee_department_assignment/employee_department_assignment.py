import frappe
from frappe.model.document import Document


class EmployeeDepartmentAssignment(Document):
    """
    Plain child table row — Frappe does NOT call a child doctype's own
    validate() when the parent (Employee) saves, only internal framework
    field checks. The team_leader manager-role check therefore lives on
    Employee's own "validate" event instead, via doc_events in hooks.py
    (see st_attendance_tracker.setup.validate_department_assignments).
    """
    pass


def on_doctype_update():
    # _get_team_members filters by team_leader on every team-dashboard and
    # task-visibility check. Without this index only the implicit
    # parent/parenttype index exists, which doesn't help a team_leader filter.
    frappe.db.add_index("Employee Department Assignment", ["team_leader"])
