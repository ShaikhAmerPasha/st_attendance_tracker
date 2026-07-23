import frappe
from frappe.model.document import Document


class RecurringTaskTemplate(Document):
    def before_insert(self):
        if not self.employee:
            self.employee = frappe.db.get_value(
                "Employee", {"user_id": frappe.session.user}, "name"
            )

    def before_save(self):
        self._was_deactivated = False
        if not self.is_new():
            was_active = frappe.db.get_value(self.doctype, self.name, "is_active")
            if was_active and not self.is_active:
                self._was_deactivated = True

    def validate(self):
        self._check_ownership()
        self._parse_estimated_time()

    def _parse_estimated_time(self):
        """Accept the same free-text time formats as Daily Task ('1h 30m',
        '45m', bare '30' = hours) instead of requiring raw decimal hours —
        matches what employees already type everywhere else in this app.
        Store the reformatted human string ('35m'), not the raw decimal-
        hours float — this field redisplays whatever's stored verbatim
        (no separate display field like Daily Task's est-in has), so
        storing the decimal directly showed ugly full-precision values
        like '0.5833333333333334' instead of '35m'."""
        from st_attendance_tracker.api import _parse_time_to_hours, _format_hours
        self.estimated_time = _format_hours(_parse_time_to_hours(self.estimated_time))

    def on_update(self):
        if getattr(self, "_was_deactivated", False):
            self._remove_todays_pending_instance()

    def _remove_todays_pending_instance(self):
        """
        Turning a template off should stop today's already-generated copy
        too, not just future days — but only if it's still untouched
        (Pending); never destroy work the employee already started/finished.
        """
        frappe.db.delete("Daily Task", {
            "employee": self.employee,
            "task_date": frappe.utils.today(),
            "task_type": "Recurring",
            "description": self.description,
            "status": "Pending",
        })

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
                "You are not allowed to edit another employee's recurring task.",
                frappe.PermissionError,
            )
