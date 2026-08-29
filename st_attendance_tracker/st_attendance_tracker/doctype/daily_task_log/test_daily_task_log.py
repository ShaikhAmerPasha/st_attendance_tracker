"""
Legacy `Daily Task` / `Daily Task Log` controller regression tests.

These doctypes are no longer written to by api.py (see
specs/daily-work-log-refactor.md — replaced by `Daily Work Log` /
`Task Entry`, tested in
../daily_work_log/test_daily_work_log.py). They remain installed, unused,
for the migration verification window (spec Section 7) and as a rollback
source; this file only guards their own standalone controller logic against
regressions until they're removed.
"""
import frappe
from frappe.utils import today
from frappe.tests.utils import FrappeTestCase


def _make_employee(suffix, dept_name, user_email):
    company = frappe.db.get_single_value("Global Defaults", "default_company") or "_Test Company"

    if not frappe.db.exists("Department", dept_name):
        frappe.get_doc({
            "doctype": "Department",
            "department_name": "_QA Dept",
            "company": company,
        }).insert(ignore_permissions=True, ignore_if_duplicate=True)

    real_dept = frappe.db.get_value(
        "Department", {"department_name": "_QA Dept", "company": company}, "name"
    ) or dept_name

    if not frappe.db.exists("User", user_email):
        u = frappe.new_doc("User")
        u.email = user_email
        u.first_name = "Test"
        u.last_name = suffix
        u.send_welcome_email = 0
        u.insert(ignore_permissions=True, ignore_if_duplicate=True)

    existing_emp = frappe.db.get_value("Employee", {"user_id": user_email}, "name")
    if existing_emp:
        return existing_emp

    frappe.db.sql("UPDATE `tabEmployee` SET user_id = NULL WHERE user_id = %s", (user_email,))
    frappe.db.commit()

    e = frappe.new_doc("Employee")
    e.first_name = "Test"
    e.last_name = suffix
    e.gender = "Male"
    e.date_of_birth = "1990-01-01"
    e.date_of_joining = "2020-01-01"
    e.department = real_dept
    e.user_id = user_email
    e.insert(ignore_permissions=True)
    return e.name


class TestLegacyDailyTaskLog(FrappeTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")
        company = frappe.db.get_single_value("Global Defaults", "default_company") or "_Test Company"
        cls.dept = (
            frappe.db.get_value("Department", {"department_name": "_QA Dept", "company": company}, "name")
            or f"_QA Dept - {company}"
        )
        cls.emp_user = "qa_legacy_emp@test.example.com"
        cls.emp_name = _make_employee("QALegacy", cls.dept, cls.emp_user)
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.set_user("Administrator")
        frappe.db.sql("DELETE FROM `tabDaily Task Log` WHERE employee=%s", (cls.emp_name,))
        frappe.db.sql("DELETE FROM `tabDaily Task` WHERE employee=%s", (cls.emp_name,))
        frappe.db.sql("DELETE FROM `tabEmployee` WHERE name=%s", (cls.emp_name,))
        frappe.db.sql("DELETE FROM `tabUser` WHERE email=%s", (cls.emp_user,))
        frappe.db.commit()

    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.sql("DELETE FROM `tabDaily Task Log` WHERE employee=%s", (self.emp_name,))
        frappe.db.sql("DELETE FROM `tabDaily Task` WHERE employee=%s", (self.emp_name,))
        frappe.db.commit()

    def test_reversed_lunch_blocked(self):
        log = frappe.new_doc("Daily Task Log")
        log.employee = self.emp_name
        log.date = today()
        log.log_type = "End of Day"
        log.login_time = "09:00:00"
        log.logout_time = "18:00:00"
        log.lunch_from = "14:00:00"
        log.lunch_to = "13:00:00"
        with self.assertRaises(frappe.ValidationError):
            log.save(ignore_permissions=True)

    def test_lunch_exceeding_4h_allowed(self):
        log = frappe.new_doc("Daily Task Log")
        log.employee = self.emp_name
        log.date = today()
        log.log_type = "End of Day"
        log.login_time = "09:00:00"
        log.logout_time = "18:00:00"
        log.lunch_from = "10:00:00"
        log.lunch_to = "15:00:00"
        log.insert(ignore_permissions=True)
        self.assertEqual(log.net_hours, "4h 0m")
        log.delete()

    def test_lunch_outside_shift_blocked(self):
        log = frappe.new_doc("Daily Task Log")
        log.employee = self.emp_name
        log.date = today()
        log.log_type = "End of Day"
        log.login_time = "09:00:00"
        log.logout_time = "17:00:00"
        log.lunch_from = "17:30:00"
        log.lunch_to = "18:30:00"
        with self.assertRaises(frappe.ValidationError):
            log.save(ignore_permissions=True)

    def test_midnight_wrap_shift_net_hours(self):
        log = frappe.new_doc("Daily Task Log")
        log.employee = self.emp_name
        log.date = today()
        log.log_type = "End of Day"
        log.login_time = "22:00:00"
        log.logout_time = "06:00:00"
        log.lunch_from = ""
        log.lunch_to = ""
        log.insert(ignore_permissions=True)
        self.assertEqual(log.net_hours, "8h 0m")
        log.delete()

    def test_duplicate_morning_log_controller_blocked(self):
        log1 = frappe.new_doc("Daily Task Log")
        log1.employee = self.emp_name
        log1.date = today()
        log1.log_type = "Morning Check-In"
        log1.login_time = "09:00:00"
        log1.insert(ignore_permissions=True)
        log1.submit()

        log2 = frappe.new_doc("Daily Task Log")
        log2.employee = self.emp_name
        log2.date = today()
        log2.log_type = "Morning Check-In"
        log2.login_time = "10:00:00"
        with self.assertRaises(frappe.ValidationError):
            log2.insert(ignore_permissions=True)
            log2.submit()

        frappe.set_user("Administrator")
        frappe.db.set_value("Daily Task Log", log1.name, "docstatus", 2)
        frappe.db.delete("Daily Task Log", {"name": ["in", [log1.name, log2.name]]})
