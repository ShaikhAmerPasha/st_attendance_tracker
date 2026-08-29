import frappe
from frappe.model.document import Document
from frappe.utils import getdate, today
from st_attendance_tracker.time_utils import parse_duration_to_hours


class AdditionalWork(Document):
    def before_insert(self):
        if not self.employee:
            self.employee = frappe.db.get_value(
                "Employee", {"user_id": frappe.session.user}, "name"
            )
            if not self.employee:
                frappe.throw("No Employee record linked to your user account.")

    def validate(self):
        self._check_ownership()
        self.hours_spent = parse_duration_to_hours(self.hours_spent)
        self._check_not_future_dated()

    def _check_not_future_dated(self):
        if self.work_date and getdate(self.work_date) > getdate(today()):
            frappe.throw("Work date cannot be in the future.")

    def on_trash(self):
        # validate() is never called on delete — without this, the ownership
        # guard above is silently bypassed for the one action (delete) the
        # doctype's own permissions actually allow employees to do.
        self._check_ownership()

    def _check_ownership(self):
        """Block cross-employee edits (BOLA guard) — same pattern as Daily Task."""
        if frappe.session.user in ("Administrator", "Guest"):
            return

        # Team Lead oversight goes through the reports_to-scoped dashboard/API
        # (_is_team_leader), not a blanket role bypass here — that role alone
        # doesn't imply this specific employee is one of their actual reports.
        allowed_roles = {"HR Manager", "System Manager"}
        user_roles = set(frappe.get_roles(frappe.session.user))
        if allowed_roles & user_roles:
            return

        current_employee = frappe.db.get_value(
            "Employee", {"user_id": frappe.session.user}, "name"
        )
        if self.employee != current_employee:
            frappe.throw(
                "You are not allowed to edit another employee's additional work entry.",
                frappe.PermissionError,
            )


def on_doctype_update():
    # Every list/lookup filters by employee + work_date together — a composite
    # index matches that pattern far better than per-column indexes.
    frappe.db.add_index("Additional Work", ["employee", "work_date"])
