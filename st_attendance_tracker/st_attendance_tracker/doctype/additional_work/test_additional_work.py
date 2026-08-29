"""Additional Work — doctype, ownership (BOLA), and self-service API tests."""
import frappe
from frappe.utils import today, add_days
from frappe.tests.utils import FrappeTestCase

from st_attendance_tracker.api import (
    get_additional_work, save_additional_work, delete_additional_work,
    submit_morning_log, submit_eod_log,
)


def _make_employee(suffix, dept_name, user_email, roles=None):
    company = frappe.db.get_single_value("Global Defaults", "default_company") or "_Test Company"

    if not frappe.db.exists("Department", dept_name):
        frappe.get_doc({
            "doctype": "Department",
            "department_name": "_QA AW Dept",
            "company": company,
        }).insert(ignore_permissions=True, ignore_if_duplicate=True)

    real_dept = frappe.db.get_value(
        "Department", {"department_name": "_QA AW Dept", "company": company}, "name"
    ) or dept_name

    if not frappe.db.exists("User", user_email):
        u = frappe.new_doc("User")
        u.email = user_email
        u.first_name = "Test"
        u.last_name = suffix
        u.send_welcome_email = 0
        u.insert(ignore_permissions=True, ignore_if_duplicate=True)

    if roles:
        user_doc = frappe.get_doc("User", user_email)
        existing = [r.role for r in user_doc.roles]
        for role in roles:
            if role not in existing:
                user_doc.append("roles", {"role": role})
        user_doc.save(ignore_permissions=True)

    existing_emp = frappe.db.get_value("Employee", {"user_id": user_email}, "name")
    if existing_emp:
        return existing_emp

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


class TestAdditionalWork(FrappeTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")
        company = frappe.db.get_single_value("Global Defaults", "default_company") or "_Test Company"
        cls.dept = (
            frappe.db.get_value("Department", {"department_name": "_QA AW Dept", "company": company}, "name")
            or f"_QA AW Dept - {company}"
        )
        cls.emp_user = "qa_aw_emp@test.example.com"
        cls.other_user = "qa_aw_other@test.example.com"
        cls.emp_name = _make_employee("AW001", cls.dept, cls.emp_user, ["Employee"])
        cls.other_name = _make_employee("AW002", cls.dept, cls.other_user, ["Employee"])
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.set_user("Administrator")
        frappe.db.sql("DELETE FROM `tabAdditional Work` WHERE employee IN (%s,%s)", (cls.emp_name, cls.other_name))
        frappe.db.sql("DELETE FROM `tabDaily Task Log` WHERE employee=%s", (cls.emp_name,))
        frappe.db.sql("DELETE FROM `tabDaily Task` WHERE employee=%s", (cls.emp_name,))
        frappe.db.sql("DELETE FROM `tabEmployee Checkin` WHERE employee=%s", (cls.emp_name,))
        frappe.db.sql("DELETE FROM `tabEmployee` WHERE name IN (%s,%s)", (cls.emp_name, cls.other_name))
        frappe.db.sql("DELETE FROM `tabUser` WHERE email IN (%s,%s)", (cls.emp_user, cls.other_user))
        frappe.db.commit()

    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.sql("DELETE FROM `tabAdditional Work` WHERE employee IN (%s,%s)", (self.emp_name, self.other_name))
        frappe.db.sql("DELETE FROM `tabDaily Task Log` WHERE employee=%s", (self.emp_name,))
        frappe.db.sql("DELETE FROM `tabDaily Task` WHERE employee=%s", (self.emp_name,))
        frappe.db.sql("DELETE FROM `tabEmployee Checkin` WHERE employee=%s", (self.emp_name,))
        frappe.db.commit()

    # ── Doctype validation ───────────────────────────────────────────────

    def test_happy_path_insert(self):
        doc = frappe.get_doc({
            "doctype": "Additional Work",
            "employee": self.emp_name,
            "work_date": today(),
            "description": "Fixed prod incident",
            "hours_spent": "1h 30m",
        }).insert(ignore_permissions=True)
        self.assertEqual(doc.employee_name, frappe.db.get_value("Employee", self.emp_name, "employee_name"))
        self.assertAlmostEqual(doc.hours_spent, 1.5)

    def test_future_dated_blocked(self):
        doc = frappe.get_doc({
            "doctype": "Additional Work",
            "employee": self.emp_name,
            "work_date": add_days(today(), 1),
            "description": "Future work",
            "hours_spent": "1h",
        })
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_backdated_allowed(self):
        doc = frappe.get_doc({
            "doctype": "Additional Work",
            "employee": self.emp_name,
            "work_date": add_days(today(), -30),
            "description": "Old backfilled work",
            "hours_spent": "45m",
        }).insert(ignore_permissions=True)
        self.assertTrue(doc.name)

    def test_missing_description_blocked(self):
        doc = frappe.get_doc({
            "doctype": "Additional Work",
            "employee": self.emp_name,
            "work_date": today(),
            "hours_spent": "1h",
        })
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    # ── BOLA ─────────────────────────────────────────────────────────────

    def test_bola_edit_blocked(self):
        other_doc = frappe.get_doc({
            "doctype": "Additional Work",
            "employee": self.other_name,
            "work_date": today(),
            "description": "Other employee's work",
            "hours_spent": "1h",
        }).insert(ignore_permissions=True)

        frappe.set_user(self.emp_user)
        other_doc.description = "Tampered"
        with self.assertRaises(frappe.PermissionError):
            other_doc.save()

    def test_bola_delete_via_api_blocked(self):
        other_doc = frappe.get_doc({
            "doctype": "Additional Work",
            "employee": self.other_name,
            "work_date": today(),
            "description": "Other employee's work",
            "hours_spent": "1h",
        }).insert(ignore_permissions=True)

        frappe.set_user(self.emp_user)
        with self.assertRaises(frappe.PermissionError):
            delete_additional_work(other_doc.name)

    def test_guest_blocked(self):
        frappe.set_user("Guest")
        with self.assertRaises(frappe.PermissionError):
            save_additional_work(work_date=today(), description="x", hours_spent="1h")

    # ── API create/list/update/delete ───────────────────────────────────

    def test_save_creates_owned_entry(self):
        frappe.set_user(self.emp_user)
        r = save_additional_work(work_date=today(), description="API created", hours_spent="2h")
        self.assertTrue(r.get("success"))
        owner = frappe.db.get_value("Additional Work", r["name"], "employee")
        self.assertEqual(owner, self.emp_name)

    def test_save_updates_existing_entry(self):
        frappe.set_user(self.emp_user)
        r = save_additional_work(work_date=today(), description="Original", hours_spent="1h")
        save_additional_work(name=r["name"], work_date=today(), description="Updated", hours_spent="2h")
        desc = frappe.db.get_value("Additional Work", r["name"], "description")
        self.assertEqual(desc, "Updated")

    def test_get_additional_work_scoped_to_caller(self):
        frappe.set_user(self.emp_user)
        save_additional_work(work_date=today(), description="Mine", hours_spent="1h")
        frappe.set_user("Administrator")
        frappe.get_doc({
            "doctype": "Additional Work",
            "employee": self.other_name,
            "work_date": today(),
            "description": "Not mine",
            "hours_spent": "1h",
        }).insert(ignore_permissions=True)

        frappe.set_user(self.emp_user)
        result = get_additional_work()
        descriptions = [e.description for e in result["entries"]]
        self.assertIn("Mine", descriptions)
        self.assertNotIn("Not mine", descriptions)

    def test_delete_removes_entry(self):
        frappe.set_user(self.emp_user)
        r = save_additional_work(work_date=today(), description="To delete", hours_spent="1h")
        delete_additional_work(r["name"])
        self.assertFalse(frappe.db.exists("Additional Work", r["name"]))

    # ── Independence from Daily Task Log ────────────────────────────────

    def test_independent_of_submitted_eod_log(self):
        """Logging additional work for a date with a submitted EOD must not
        raise, and must not touch that log's hours/docstatus."""
        frappe.set_user(self.emp_user)
        import json
        submit_morning_log(new_tasks=json.dumps([{"description": "Task"}]), login_time="09:00", work_location="Office")
        submit_eod_log(lunch_from="", lunch_to="", logout_time="18:00", task_updates="[]", adhoc_tasks="[]")

        eod_before = frappe.db.get_value(
            "Daily Task Log",
            {"employee": self.emp_name, "date": today(), "log_type": "End of Day"},
            ["net_hours", "working_hours", "docstatus"], as_dict=True,
        )

        r = save_additional_work(work_date=today(), description="Extra after checkout", hours_spent="1h")
        self.assertTrue(r.get("success"))

        eod_after = frappe.db.get_value(
            "Daily Task Log",
            {"employee": self.emp_name, "date": today(), "log_type": "End of Day"},
            ["net_hours", "working_hours", "docstatus"], as_dict=True,
        )
        self.assertEqual(eod_before, eod_after)
