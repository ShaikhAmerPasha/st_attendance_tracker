import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime, get_datetime, time_diff_in_hours
from st_attendance_tracker.time_utils import time_to_minutes, validate_lunch_hours, calculate_net_minutes


class DailyTaskLog(Document):
    def validate(self):
        self._check_ownership()
        self._validate_no_duplicate()
        if self.log_type == "Morning Check-In" and not self.login_time:
            self.login_time = now_datetime().strftime("%H:%M:%S")
        elif self.log_type == "End of Day":
            if not self.login_time:
                morning_login = frappe.db.get_value("Daily Task Log", {
                    "employee": self.employee,
                    "date": self.date,
                    "log_type": "Morning Check-In",
                    "docstatus": 1
                }, "login_time")
                if morning_login:
                    self.login_time = morning_login
            
            if not self.login_time:
                frappe.throw(
                    "Morning Check-In is required before submitting End of Day.",
                    frappe.ValidationError
                )
            # FIX 2: validate AFTER login_time is resolved from DB
            self._validate_lunch_hours()
            self._calculate_net_hours()
            self._calculate_working_hours()
        self._check_late()

    def on_submit(self):
        if self.log_type == "End of Day":
            if not self.logout_time:
                frappe.throw("Logout Time is required for End of Day log.")
            self._validate_lunch_hours()
            self._calculate_net_hours()
            self._calculate_working_hours()
            self.db_update()

    def on_trash(self):
        # validate() is never called on delete — without this, the ownership
        # guard above is silently bypassed for the one action (delete) the
        # doctype's own permissions actually allow employees to do.
        self._check_ownership()

    def _check_ownership(self):
        """Block logging attendance for another employee (BOLA guard)."""
        if frappe.session.user in ("Administrator", "Guest"):
            return
        # Team Lead oversight goes through the reports_to-scoped dashboard/API
        # (_is_team_leader), not a blanket role bypass here.
        allowed_roles = {"HR Manager", "System Manager"}
        if allowed_roles & set(frappe.get_roles(frappe.session.user)):
            return
        current_employee = frappe.db.get_value(
            "Employee", {"user_id": frappe.session.user}, "name"
        )
        if self.employee != current_employee:
            frappe.throw(
                "You are not allowed to create or edit another employee's attendance log.",
                frappe.PermissionError,
            )

    def _validate_no_duplicate(self):
        existing = frappe.db.exists("Daily Task Log", {
            "employee": self.employee,
            "date": self.date,
            "log_type": self.log_type,
            "docstatus": 1,
            "name": ["!=", self.name],
        })
        if existing:
            frappe.throw(
                f"A {self.log_type} log for {self.employee_name} "
                f"on {self.date} already exists: {existing}"
            )

    def _check_late(self):
        if self.log_type != "Morning Check-In" or not self.login_time:
            return
        try:
            threshold = frappe.db.get_single_value(
                "ST Attendance Settings", "late_checkin_threshold"
            )
            if not threshold:
                return
            login_mins = time_to_minutes(self.login_time)
            threshold_mins = time_to_minutes(threshold)
            self.is_late = 1 if login_mins > threshold_mins else 0
        except Exception:
            pass

    def _validate_lunch_hours(self):
        validate_lunch_hours(self.login_time, self.logout_time, self.lunch_from, self.lunch_to)

    def _calculate_net_hours(self):
        if not self.login_time or not self.logout_time:
            return
        try:
            net_mins = calculate_net_minutes(
                self.login_time, self.logout_time, self.lunch_from, self.lunch_to, self.date
            )
            hours = net_mins // 60
            mins = net_mins % 60
            self.net_hours = f"{hours}h {mins}m"
        except Exception:
            frappe.log_error(frappe.get_traceback(), "ST Attendance Tracker — net hours calculation failed")
            frappe.msgprint(
                "Could not calculate your net working hours automatically. "
                "Your checkout has still been recorded — please contact HR to verify your hours.",
                indicator="orange", alert=True,
            )

    def _calculate_working_hours(self):
        if self.log_type != "End of Day":
            self.working_hours = 0
            return
        total_actual_hours = frappe.db.sql(
            """SELECT SUM(actual_time) FROM `tabDaily Task`
               WHERE employee = %s AND task_date = %s""",
            (self.employee, self.date),
        )[0][0] or 0.0
        self.working_hours = float(total_actual_hours)


def on_doctype_update():
    # Every check-in/checkout lookup in this app filters by employee + date
    # together — a composite index matches that pattern far better than
    # per-column indexes.
    frappe.db.add_index("Daily Task Log", ["employee", "date"])
