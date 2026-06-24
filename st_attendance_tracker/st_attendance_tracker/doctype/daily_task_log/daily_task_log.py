import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime, get_datetime, time_diff_in_hours


class DailyTaskLog(Document):

    def validate(self):
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
            self._calculate_net_hours()
        self._check_late()

    def on_submit(self):
        if self.log_type == "End of Day":
            if not self.logout_time:
                frappe.throw("Logout Time is required for End of Day log.")
            self._calculate_net_hours()
            self.db_update()

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
        if self.log_type != "Morning Check-In" or not self.login_time:
            return
        try:
            threshold = frappe.db.get_single_value(
                "ST Attendance Settings", "late_checkin_threshold"
            )
            if not threshold:
                return
            login_mins = self._time_to_mins(self.login_time)
            threshold_mins = self._time_to_mins(threshold)
            self.is_late = 1 if login_mins > threshold_mins else 0
        except Exception:
            pass

    def _calculate_net_hours(self):
        if not self.login_time or not self.logout_time:
            return
        try:
            login_mins = self._time_to_mins(self.login_time)
            logout_mins = self._time_to_mins(self.logout_time)
            total_mins = logout_mins - login_mins
            if total_mins < 0:
                return

            lunch_mins = 0
            if self.lunch_from and self.lunch_to:
                lf_mins = self._time_to_mins(self.lunch_from)
                lt_mins = self._time_to_mins(self.lunch_to)
                lunch_mins = max(0, lt_mins - lf_mins)

            net_mins = max(0, total_mins - lunch_mins)
            hours = net_mins // 60
            mins  = net_mins % 60
            self.net_hours = f"{hours}h {mins}m"
        except Exception:
            pass
