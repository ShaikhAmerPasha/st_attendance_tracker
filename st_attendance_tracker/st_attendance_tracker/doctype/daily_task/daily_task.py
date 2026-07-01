import frappe
from frappe.model.document import Document


class DailyTask(Document):
    def before_insert(self):
        if not self.employee:
            self.employee = frappe.db.get_value(
                "Employee", {"user_id": frappe.session.user}, "name"
            )
        if not self.origin_date:
            self.origin_date = self.task_date

    def validate(self):
        if not self.description:
            frappe.throw("Task description cannot be empty.")
        self._check_ownership()
        self._check_eod_submitted()

    def _check_ownership(self):
        """
        Block cross-employee edits (BOLA guard).
        Fires on every save path — insert, update, delete via controller.
        Allowed: the task's own employee, HR Managers, Administrators, Team Leaders.
        """
        # Skip for system/background operations
        if frappe.session.user in ("Administrator", "Guest"):
            return

        # Determine which employee the task belongs to
        if self.is_new():
            task_employee = self.employee
        else:
            task_employee = frappe.db.get_value("Daily Task", self.name, "employee")

        if not task_employee:
            return

        # Determine the current user's employee
        current_employee = frappe.db.get_value(
            "Employee", {"user_id": frappe.session.user}, "name"
        )

        # Allowed privileged roles
        allowed_roles = {"HR Manager", "Team Lead", "System Manager"}
        user_roles = set(frappe.get_roles(frappe.session.user))
        if allowed_roles & user_roles:
            return

        if task_employee != current_employee:
            frappe.throw(
                "You are not allowed to edit another employee's task.",
                frappe.PermissionError,
            )

    def _check_eod_submitted(self):
        """Block editing tasks after EOD submission for that date (regular employee only)."""
        if frappe.session.user in ("Administrator", "Guest"):
            return

        # Allowed privileged roles can bypass
        allowed_roles = {"HR Manager", "Team Lead", "System Manager"}
        user_roles = set(frappe.get_roles(frappe.session.user))
        if allowed_roles & user_roles:
            return

        if frappe.db.exists("Daily Task Log", {
            "employee": self.employee,
            "date": self.task_date,
            "log_type": "End of Day",
            "docstatus": 1
        }):
            frappe.throw(
                "Cannot modify tasks after End of Day is submitted.",
                frappe.ValidationError
            )
