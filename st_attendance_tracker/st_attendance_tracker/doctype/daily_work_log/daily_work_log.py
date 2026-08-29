import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime, flt
from st_attendance_tracker.time_utils import resolve_zero_diff_minutes, parse_duration_to_hours
from st_attendance_tracker.api import _get_attendance_settings


_TIME_FIELDS = ("login_time", "logout_time", "lunch_from", "lunch_to")


class DailyWorkLog(Document):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Every Time-fieldtype field gets auto-defaulted to nowtime() twice:
        # once by frappe.new_doc() (frappe.model.create_new, before this
        # __init__ even runs), and again on *every* insert() via
        # Document._set_defaults() -> update_if_missing(), which re-pulls
        # from a fresh new_doc() template for any field still None. Since
        # all four now live on one doc (unlike the old two-doc model, where
        # a Morning Check-In log's unused logout/lunch fields were never
        # read), an unchecked-out check-in would otherwise inherit a
        # garbage logout_time/lunch window and fail lunch validation.
        # dont_update_if_missing (base_document.py) is the documented way
        # to suppress the second backfill; clearing here handles the first.
        if self.is_new():
            for fieldname in _TIME_FIELDS:
                self.set(fieldname, None)
        self.dont_update_if_missing = list(_TIME_FIELDS)

    def before_insert(self):
        if not self.employee:
            self.employee = frappe.db.get_value(
                "Employee", {"user_id": frappe.session.user}, "name"
            )
            if not self.employee:
                frappe.throw("No Employee record linked to your user account.")

    def validate(self):
        self._check_ownership()
        self._check_locked()
        self._validate_no_duplicate()
        self._prepare_tasks()
        if self.eod_submitted and not self.login_time:
            frappe.throw(
                "Morning Check-In is required before submitting End of Day.",
                frappe.ValidationError,
            )
        self._check_late()
        self._validate_lunch_hours()
        self._calculate_net_hours()
        self._calculate_working_hours()
        if self.eod_submitted and not self.locked_at:
            self.locked_at = now_datetime()
        elif not self.eod_submitted:
            self.locked_at = None

    def on_trash(self):
        # validate() is never called on delete — without this, the ownership
        # guard above is silently bypassed for the one action (delete) the
        # doctype's own permissions actually allow employees to do.
        self._check_ownership()

    def _check_ownership(self):
        """Block logging attendance for another employee (BOLA guard)."""
        if frappe.session.user in ("Administrator", "Guest"):
            return
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

    def _check_locked(self):
        """Block edits once End of Day has been submitted (regular employee only)."""
        if self.is_new():
            return
        if frappe.session.user in ("Administrator", "Guest"):
            return
        allowed_roles = {"HR Manager", "System Manager"}
        if allowed_roles & set(frappe.get_roles(frappe.session.user)):
            return
        was_locked = frappe.db.get_value("Daily Work Log", self.name, "eod_submitted")
        if was_locked and self.eod_submitted:
            frappe.throw(
                "Cannot modify this log after End of Day has been submitted.",
                frappe.ValidationError,
            )

    def _validate_no_duplicate(self):
        existing = frappe.db.exists("Daily Work Log", {
            "employee": self.employee,
            "date": self.date,
            "name": ["!=", self.name or ""],
        })
        if existing:
            frappe.throw(
                f"A Daily Work Log for {self.employee_name} on {self.date} already exists: {existing}"
            )

    def _prepare_tasks(self):
        for row in self.tasks:
            if not row.series_id:
                row.series_id = frappe.generate_hash(length=32)
            if not row.origin_date:
                row.origin_date = self.date
            row.estimated_time = parse_duration_to_hours(row.estimated_time)
            row.actual_time = parse_duration_to_hours(row.actual_time)
            if not (row.description or "").strip():
                frappe.throw("Task description cannot be empty.")
            if row.status == "Done" and not row.actual_time:
                frappe.throw("Time Taken for Task Completion is mandatory for completed tasks.")

    def _time_to_mins(self, t):
        import datetime
        if isinstance(t, datetime.timedelta):
            return int(t.total_seconds()) // 60
        s = str(t or "").strip().lower()
        if not s:
            return 0

        is_pm = "pm" in s
        is_am = "am" in s
        s = s.replace("pm", "").replace("am", "").strip()

        parts = s.split(":")
        if len(parts) >= 2:
            try:
                h = int(parts[0])
                m = int(parts[1])
                if is_pm and h < 12:
                    h += 12
                elif is_am and h == 12:
                    h = 0
                return h * 60 + m
            except Exception:
                pass
        return 0

    def _check_late(self):
        if not self.login_time:
            return
        try:
            threshold = _get_attendance_settings().get("late_checkin_threshold")
            if not threshold:
                return
            login_mins = self._time_to_mins(self.login_time)
            threshold_mins = self._time_to_mins(threshold)
            self.is_late = 1 if login_mins > threshold_mins else 0
        except Exception:
            pass

    def _validate_lunch_hours(self):
        """Reject reversed, zero-duration, or out-of-shift lunch intervals."""
        if not (self.login_time and self.logout_time and self.lunch_from and self.lunch_to):
            return

        login_mins = self._time_to_mins(self.login_time)
        logout_mins = self._time_to_mins(self.logout_time)
        lf_mins = self._time_to_mins(self.lunch_from)
        lt_mins = self._time_to_mins(self.lunch_to)

        shift_len = (logout_mins - login_mins) if logout_mins >= login_mins \
            else (logout_mins + 24 * 60 - login_mins)

        lf_abs = (lf_mins - login_mins) if lf_mins >= login_mins \
            else (lf_mins + 24 * 60 - login_mins)
        lt_abs = (lt_mins - login_mins) if lt_mins >= login_mins \
            else (lt_mins + 24 * 60 - login_mins)

        if lt_abs < lf_abs:
            lt_abs += 24 * 60

        lunch_duration = lt_abs - lf_abs

        if lunch_duration <= 0:
            frappe.throw(
                f"Lunch duration cannot be zero or negative. "
                f"Selected interval: {self.lunch_from} → {self.lunch_to}."
            )

        if lf_abs < 0 or lt_abs > shift_len:
            frappe.throw(
                f"Lunch interval ({self.lunch_from} → {self.lunch_to}) must fall "
                f"completely within your shift ({self.login_time} → {self.logout_time})."
            )

    def _calculate_net_hours(self):
        if not self.login_time or not self.logout_time:
            return
        try:
            login_mins = self._time_to_mins(self.login_time)
            logout_mins = self._time_to_mins(self.logout_time)
            total_mins = logout_mins - login_mins
            if total_mins < 0:
                total_mins += 24 * 60
            elif total_mins == 0:
                total_mins = resolve_zero_diff_minutes(self.date)

            lunch_mins = 0
            if self.lunch_from and self.lunch_to:
                lf_mins = self._time_to_mins(self.lunch_from)
                lt_mins = self._time_to_mins(self.lunch_to)
                d = lt_mins - lf_mins
                if d < 0:
                    d += 24 * 60
                if 0 < d:
                    lunch_mins = d

            net_mins = max(0, total_mins - lunch_mins)
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
        if not self.eod_submitted:
            self.working_hours = 0
            return
        self.working_hours = sum(flt(row.actual_time) for row in self.tasks)


def on_doctype_update():
    # Every check-in/checkout lookup in this app filters by employee + date
    # together — a composite index matches that pattern far better than
    # per-column indexes.
    frappe.db.add_index("Daily Work Log", ["employee", "date"])
