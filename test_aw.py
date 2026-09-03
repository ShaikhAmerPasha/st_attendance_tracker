import frappe
frappe.init(site="excel")
frappe.connect()

from st_attendance_tracker.api import save_additional_work

try:
    res = save_additional_work(
        work_date="2026-09-03",
        project_name="Test",
        hours_spent="3 hr",
        description="Testing from python",
        login_time="09:00",
        logout_time="18:00",
        status="Done"
    )
    print("SUCCESS", res)
except Exception as e:
    print("FAILED", e)
