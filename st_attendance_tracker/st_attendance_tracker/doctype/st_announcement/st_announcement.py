import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class STAnnouncement(Document):
    def before_insert(self):
        if not self.publish_date:
            self.publish_date = now_datetime()
