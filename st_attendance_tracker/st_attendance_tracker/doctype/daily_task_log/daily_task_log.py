import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

class DailyTaskLog(Document):
    def validate(self):
        existing = frappe.db.exists("Daily Task Log", {
            "employee": self.employee,
            "date": self.date,
            "log_type": self.log_type,
            "docstatus": 1,
            "name": ["!=", self.name],
        })
        if existing:
            frappe.throw(f"A {self.log_type} log for {self.employee} on {self.date} already exists: {existing}")
        if self.log_type == "Morning Check-In" and not self.login_time:
            self.login_time = now_datetime().strftime("%H:%M:%S")

    def on_submit(self):
        if self.log_type == "End of Day" and not self.logout_time:
            frappe.throw("Logout Time is required for End of Day log.")
