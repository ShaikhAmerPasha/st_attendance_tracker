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
