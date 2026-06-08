import frappe
from frappe.model.document import Document

class DailyTask(Document):
    def before_insert(self):
        if not self.employee:
            self.employee = frappe.db.get_value("Employee", {"user_id": frappe.session.user}, "name")
        if not self.origin_date:
            self.origin_date = self.task_date
    def validate(self):
        if not self.description:
            frappe.throw("Task description cannot be empty.")
