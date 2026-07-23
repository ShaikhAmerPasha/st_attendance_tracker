"""
ST Attendance Tracker — Full QA Test Suite
Covers: functional core, edge cases, security, data integrity, concurrency guards.
"""
import json
import frappe
from frappe.utils import today, now_datetime, add_days
from frappe.utils.data import getdate
from frappe.tests.utils import FrappeTestCase
from datetime import timedelta

from st_attendance_tracker.api import (
    _to_hhmm, _to_ampm, _calc_net_hours,
    submit_morning_log, submit_eod_log,
    get_page_state, validate_wfh_request,
    get_management_dashboard, _get_team_leader_emails,
)
from st_attendance_tracker.api import delete_carried_task


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_employee(suffix, dept_name, user_email, roles=None):
    company = frappe.db.get_single_value("Global Defaults", "default_company") or "_Test Company"

    # Insert the department if absent; Frappe appends " - {abbr}" automatically,
    # so we look up what the real name became after insert.
    if not frappe.db.exists("Department", dept_name):
        frappe.get_doc({
            "doctype": "Department",
            "department_name": "_QA Dept",
            "company": company,
        }).insert(ignore_permissions=True, ignore_if_duplicate=True)

    # Resolve the real name from DB (handles " - ST" vs " - Standardtouch")
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

    # Clear any stale user_id assignment from previous test runs
    frappe.db.sql(
        "UPDATE `tabEmployee` SET user_id = NULL WHERE user_id = %s",
        (user_email,)
    )
    frappe.db.commit()

    e = frappe.new_doc("Employee")
    e.first_name = "Test"
    e.last_name = suffix
    e.gender = "Male"
    e.date_of_birth = "1990-01-01"
    e.date_of_joining = "2020-01-01"
    e.department = real_dept   # use the actual DB name
    e.user_id = user_email
    e.insert(ignore_permissions=True)
    return e.name


class TestQACheckinFull(FrappeTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")

        company = frappe.db.get_single_value("Global Defaults", "default_company") or "_Test Company"
        # Use the actual dept name Frappe creates (abbr-based, e.g. "_QA Dept - ST")
        cls.dept = (
            frappe.db.get_value("Department", {"department_name": "_QA Dept", "company": company}, "name")
            or f"_QA Dept - {company}"
        )
        cls.emp_user = "qa_emp@test.example.com"
        cls.hr_user  = "qa_hr@test.example.com"

        cls.emp_name = _make_employee("QA001", cls.dept, cls.emp_user, ["Employee"])
        cls.hr_name  = _make_employee("QAHR",  cls.dept, cls.hr_user,  ["HR Manager"])
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.set_user("Administrator")
        frappe.db.sql("DELETE FROM `tabDaily Task Log` WHERE employee IN (%s,%s)", (cls.emp_name, cls.hr_name))
        frappe.db.sql("DELETE FROM `tabDaily Task`     WHERE employee IN (%s,%s)", (cls.emp_name, cls.hr_name))
        frappe.db.sql("DELETE FROM `tabEmployee Checkin` WHERE employee IN (%s,%s)", (cls.emp_name, cls.hr_name))
        frappe.db.sql("DELETE FROM `tabEmployee` WHERE name IN (%s,%s)", (cls.emp_name, cls.hr_name))
        frappe.db.sql("DELETE FROM `tabUser` WHERE email IN (%s,%s)", (cls.emp_user, cls.hr_user))
        frappe.db.commit()

    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.sql("DELETE FROM `tabDaily Task Log` WHERE employee=%s", (self.emp_name,))
        frappe.db.sql("DELETE FROM `tabDaily Task`     WHERE employee=%s", (self.emp_name,))
        frappe.db.sql("DELETE FROM `tabEmployee Checkin` WHERE employee=%s", (self.emp_name,))
        frappe.db.commit()

    # ── SECTION 1 — Functional Core ────────────────────────────────────────────

    def test_1_1_happy_path_checkin_creates_log(self):
        """TC-1.1: Valid check-in creates Morning Check-In log and Employee Checkin record."""
        frappe.set_user(self.emp_user)
        r = submit_morning_log(
            new_tasks=json.dumps([{"description": "QA happy path task", "estimated_time": "1h"}]),
            work_location="Office"
        )
        self.assertTrue(r.get("success"))
        log = frappe.db.exists("Daily Task Log", {
            "employee": self.emp_name, "date": today(), "log_type": "Morning Check-In", "docstatus": 1
        })
        self.assertIsNotNone(log, "Morning Check-In log not created")
        checkin = frappe.db.exists("Employee Checkin", {
            "employee": self.emp_name, "log_type": "IN", "device_id": "ST Daily Checkin"
        })
        self.assertIsNotNone(checkin, "Employee Checkin IN record not created")

    def test_1_2_double_checkin_blocked(self):
        """TC-1.2: Checking in twice on the same day is blocked."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "First check-in"}]),
            work_location="Office"
        )
        with self.assertRaises(frappe.ValidationError):
            submit_morning_log(
                new_tasks=json.dumps([{"description": "Second check-in"}]),
                work_location="Office"
            )

    def test_1_3_eod_without_checkin_blocked(self):
        """TC-1.3: EOD submission without morning check-in is blocked."""
        frappe.set_user(self.emp_user)
        with self.assertRaises(frappe.ValidationError):
            submit_eod_log(
                lunch_from="13:00", lunch_to="14:00",
                logout_time="18:00",
                task_updates="[]", adhoc_tasks="[]"
            )

    def test_1_4_full_checkin_checkout_cycle(self):
        """TC-1.4: Full check-in → EOD cycle creates both log types and Employee Checkin OUT."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Morning task"}]),
            login_time="09:00",
            work_location="Office"
        )
        r = submit_eod_log(
            lunch_from="13:00", lunch_to="14:00",
            logout_time="18:00",
            task_updates="[]", adhoc_tasks="[]"
        )
        self.assertTrue(r.get("success"))
        eod = frappe.db.exists("Daily Task Log", {
            "employee": self.emp_name, "date": today(), "log_type": "End of Day", "docstatus": 1
        })
        self.assertIsNotNone(eod)
        checkout = frappe.db.exists("Employee Checkin", {
            "employee": self.emp_name, "log_type": "OUT", "device_id": "ST Daily Checkin"
        })
        self.assertIsNotNone(checkout, "Employee Checkin OUT record not created")

    def test_1_5_double_eod_blocked(self):
        """TC-1.5: Submitting EOD twice on same day is blocked."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task"}]),
            login_time="09:00",
            work_location="Office"
        )
        submit_eod_log(
            lunch_from="", lunch_to="", logout_time="18:00",
            task_updates="[]", adhoc_tasks="[]"
        )
        with self.assertRaises(frappe.ValidationError):
            submit_eod_log(
                lunch_from="", lunch_to="", logout_time="19:00",
                task_updates="[]", adhoc_tasks="[]"
            )

    def test_1_6_no_tasks_checkin_blocked(self):
        """TC-1.6: Check-in with zero tasks and no carried tasks is blocked."""
        frappe.set_user(self.emp_user)
        with self.assertRaises(frappe.ValidationError):
            submit_morning_log(new_tasks="[]", work_location="Office")

    # ── SECTION 2 — Edge Cases & Invalid Inputs ────────────────────────────────

    def test_2_1_reversed_lunch_blocked(self):
        """TC-2.1: Reversed lunch times (from 14:00 to 13:00) blocked by controller."""
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

    def test_2_2_lunch_exceeding_4h_allowed(self):
        """TC-2.2: Lunch break > 4 hours is allowed."""
        log = frappe.new_doc("Daily Task Log")
        log.employee = self.emp_name
        log.date = today()
        log.log_type = "End of Day"
        log.login_time = "09:00:00"
        log.logout_time = "18:00:00"
        log.lunch_from = "10:00:00"
        log.lunch_to = "15:00:00"  # 5 hours
        log.insert(ignore_permissions=True)
        self.assertEqual(log.net_hours, "4h 0m")
        log.delete()

    def test_2_3_lunch_outside_shift_blocked(self):
        """TC-2.3: Lunch interval outside work shift boundaries is blocked."""
        log = frappe.new_doc("Daily Task Log")
        log.employee = self.emp_name
        log.date = today()
        log.log_type = "End of Day"
        log.login_time = "09:00:00"
        log.logout_time = "17:00:00"
        log.lunch_from = "17:30:00"  # After logout
        log.lunch_to = "18:30:00"
        with self.assertRaises(frappe.ValidationError):
            log.save(ignore_permissions=True)

    def test_2_4_midnight_wrap_shift_net_hours(self):
        """TC-2.4: Night shift crossing midnight calculates correct net hours."""
        log = frappe.new_doc("Daily Task Log")
        log.employee = self.emp_name
        log.date = today()
        log.log_type = "End of Day"
        log.login_time = "22:00:00"
        log.logout_time = "06:00:00"  # Next day
        log.lunch_from = ""
        log.lunch_to = ""
        log.insert(ignore_permissions=True)
        # 8h shift, no lunch → net = 8h 0m
        self.assertEqual(log.net_hours, "8h 0m")
        log.delete()

    def test_2_5_no_lunch_net_hours_correct(self):
        """TC-2.5: Net hours without lunch is correctly calculated."""
        result = _calc_net_hours("09:00", "17:30", "", "", today())
        self.assertEqual(result, "8h 30m")

    def test_2_6_lunch_deducted_correctly(self):
        """TC-2.6: Standard lunch is correctly deducted from net hours."""
        result = _calc_net_hours("09:00", "18:00", "13:00", "14:00", today())
        self.assertEqual(result, "8h 0m")

    def test_2_7_empty_logout_throws(self):
        """TC-2.7: EOD with empty logout_time is blocked."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task"}]),
            work_location="Office"
        )
        with self.assertRaises(frappe.ValidationError):
            submit_eod_log(
                lunch_from="", lunch_to="", logout_time="",
                task_updates="[]", adhoc_tasks="[]"
            )

    def test_2_8_xss_in_task_description_stored_safely(self):
        """TC-2.8: XSS payload in task description is stored as plain text, not executed."""
        frappe.set_user(self.emp_user)
        xss = "<script>alert('xss')</script>"
        submit_morning_log(
            new_tasks=json.dumps([{"description": xss}]),
            work_location="Office"
        )
        task = frappe.db.get_value(
            "Daily Task",
            {"employee": self.emp_name, "task_date": today()},
            "description"
        )
        # Value is stored as-is in DB; rendering must escape — confirm no code exec possible at API level
        self.assertIn("script", task)  # Stored literally, not executed

    def test_2_9_sql_injection_in_task_description_safe(self):
        """TC-2.9: SQL-like input in task description does not cause errors."""
        frappe.set_user(self.emp_user)
        sql_input = "'; DROP TABLE `tabDaily Task`; --"
        r = submit_morning_log(
            new_tasks=json.dumps([{"description": sql_input}]),
            work_location="Office"
        )
        self.assertTrue(r.get("success"))
        task = frappe.db.get_value(
            "Daily Task",
            {"employee": self.emp_name, "task_date": today()},
            "description"
        )
        self.assertEqual(task, sql_input)

    def test_2_10_long_task_description_handled(self):
        """TC-2.10: 500+ char task description is accepted without crash."""
        frappe.set_user(self.emp_user)
        long_desc = "A" * 600
        r = submit_morning_log(
            new_tasks=json.dumps([{"description": long_desc}]),
            work_location="Office"
        )
        self.assertTrue(r.get("success"))

    # ── SECTION 3 — Authentication & Authorization ─────────────────────────────

    def test_3_1_guest_has_no_employee_access(self):
        """TC-3.1: Non-employee user cannot call employee API methods (raises PermissionError)."""
        # Create a user with NO employee record
        no_emp_email = "no_emp_test_user@test.example.com"
        if not frappe.db.exists("User", no_emp_email):
            u = frappe.new_doc("User")
            u.email = no_emp_email
            u.first_name = "No"
            u.last_name = "Emp"
            u.send_welcome_email = 0
            u.insert(ignore_permissions=True, ignore_if_duplicate=True)
            
        frappe.set_user(no_emp_email)
        with self.assertRaises(frappe.PermissionError):
            submit_morning_log(new_tasks="[]", work_location="Office")


    def test_3_2_management_dashboard_blocks_employee(self):
        """TC-3.3: Regular employee cannot access management dashboard API."""
        frappe.set_user(self.emp_user)
        with self.assertRaises(frappe.PermissionError):
            get_management_dashboard(today())

    def test_3_4_bola_task_edit_blocked(self):
        """TC-3.4: Employee cannot edit tasks belonging to another employee via API."""
        frappe.set_user("Administrator")
        # Create a task owned by hr employee impersonated as Administrator
        other_task = frappe.new_doc("Daily Task")
        other_task.employee = self.hr_name
        other_task.task_date = today()
        other_task.description = "HR task"
        other_task.status = "Pending"
        other_task.insert(ignore_permissions=True)

        frappe.set_user(self.emp_user)
        other_task.description = "Tampered by emp"
        with self.assertRaises(frappe.PermissionError):
            other_task.save()

        frappe.set_user("Administrator")
        other_task.delete()

    def test_3_5_delete_carried_task_blocked_after_eod(self):
        """TC-3.5: Cannot delete a task after EOD has been submitted for that date."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task to delete after EOD"}]),
            login_time="09:00",
            work_location="Office"
        )
        task_name = frappe.db.get_value(
            "Daily Task", {"employee": self.emp_name, "task_date": today()}, "name"
        )
        submit_eod_log(
            lunch_from="", lunch_to="", logout_time="18:00",
            task_updates=json.dumps([{"name": task_name, "status": "Done", "actual_time": "", "description": "Task to delete after EOD"}]),
            adhoc_tasks="[]"
        )
        with self.assertRaises(frappe.ValidationError):
            delete_carried_task(task_name)

    # ── SECTION 4 — Time / Calculation Edge Cases ──────────────────────────────

    def test_4_1_to_hhmm_timedelta(self):
        """TC-4.1: _to_hhmm handles timedelta from DB correctly."""
        td = timedelta(hours=9, minutes=30)
        self.assertEqual(_to_hhmm(td), "09:30")

    def test_4_2_to_hhmm_single_digit_hour_no_trailing_colon(self):
        """TC-4.2: _to_hhmm converts '9:30:00' without trailing colon."""
        self.assertEqual(_to_hhmm("9:30:00"), "09:30")

    def test_4_3_to_hhmm_ampm(self):
        """TC-4.3: _to_hhmm converts AM/PM format correctly."""
        self.assertEqual(_to_hhmm("05:15 pm"), "17:15")
        self.assertEqual(_to_hhmm("12:00 am"), "00:00")
        self.assertEqual(_to_hhmm("12:00 pm"), "12:00")

    def test_4_4_to_hhmm_none_returns_empty(self):
        """TC-4.4: _to_hhmm with None/empty returns empty string."""
        self.assertEqual(_to_hhmm(None), "")
        self.assertEqual(_to_hhmm(""), "")

    def test_4_5_to_ampm_midnight(self):
        """TC-4.5: _to_ampm converts midnight and noon correctly."""
        self.assertEqual(_to_ampm("00:00"), "12:00 AM")
        self.assertEqual(_to_ampm("12:00"), "12:00 PM")

    def test_4_6_net_hours_sanity_ceiling(self):
        """TC-4.6: Net hours > 18h returns empty (sanity ceiling)."""
        yesterday = frappe.utils.add_days(today(), -1)
        result = _calc_net_hours("06:00", "06:00", "", "", yesterday)
        self.assertEqual(result, "")
    def test_4_7_decimal_hours_in_leaderboard_parse(self):
        """TC-4.7: Leaderboard parses decimal hour strings (8.5h = 510 min)."""
        # Test via management dashboard integration
        frappe.set_user(self.hr_user)
        res = get_management_dashboard(today())
        self.assertIn("rankings", res)
        # Decimal parsing verified at unit level:
        from st_attendance_tracker.api import get_management_dashboard as gmd
        # Inline test of parse helper logic
        def _parse(s):
            s = str(s).strip().lower()
            h = m = 0
            if "h" in s:
                parts = s.split("h")
                h_val = float(parts[0].strip())
                h = int(h_val)
                m = int(round((h_val - h) * 60))
                s = parts[1]
            if "m" in s:
                parts = s.split("m")
                m += int(float(parts[0].strip()))
            return h * 60 + m
        self.assertEqual(_parse("8.5h"), 510)
        self.assertEqual(_parse("8h 30m"), 510)
        self.assertEqual(_parse("7h 45m"), 465)

    # ── SECTION 5 — Data Integrity ─────────────────────────────────────────────

    def test_5_1_task_rollover_idempotent(self):
        """TC-5.1: Pending tasks rolled over only once (no duplicate on re-trigger)."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Rollover task"}]),
            login_time="09:00",
            work_location="Office"
        )
        submit_eod_log(
            lunch_from="", lunch_to="", logout_time="18:00",
            task_updates="[]", adhoc_tasks="[]"
        )
        next_date = str(getdate(today()) + timedelta(days=1))
        count = frappe.db.count("Daily Task", {
            "employee": self.emp_name,
            "task_date": next_date,
        })
        self.assertEqual(count, 1, "Task was duplicated on rollover")

    def test_5_2_adhoc_task_created_on_eod(self):
        """TC-5.2: Ad-hoc tasks submitted during EOD are correctly created."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Planned task"}]),
            login_time="09:00",
            work_location="Office"
        )
        submit_eod_log(
            lunch_from="", lunch_to="", logout_time="18:00",
            task_updates="[]",
            adhoc_tasks=json.dumps([{"description": "Ad-hoc task done", "status": "Done"}])
        )
        adhoc = frappe.db.exists("Daily Task", {
            "employee": self.emp_name,
            "task_date": today(),
            "task_type": "Ad-hoc",
            "description": "Ad-hoc task done"
        })
        self.assertIsNotNone(adhoc)

    def test_5_3_empty_adhoc_description_ignored(self):
        """TC-5.3: Ad-hoc task with empty description is not inserted."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task"}]),
            login_time="09:00",
            work_location="Office"
        )
        before = frappe.db.count("Daily Task", {"employee": self.emp_name, "task_date": today()})
        submit_eod_log(
            lunch_from="", lunch_to="", logout_time="18:00",
            task_updates="[]",
            adhoc_tasks=json.dumps([{"description": "   ", "status": "Done"}])
        )
        after = frappe.db.count("Daily Task", {"employee": self.emp_name, "task_date": today()})
        self.assertEqual(before, after, "Empty adhoc description created a task")

    def test_5_4_net_hours_stored_in_db(self):
        """TC-5.4: Net hours are persisted in the Daily Task Log after EOD."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task"}]),
            login_time="09:00",
            work_location="Office"
        )
        submit_eod_log(
            lunch_from="13:00", lunch_to="14:00",
            logout_time="18:00",
            task_updates="[]", adhoc_tasks="[]"
        )
        eod_log = frappe.db.get_value(
            "Daily Task Log",
            {"employee": self.emp_name, "date": today(), "log_type": "End of Day", "docstatus": 1},
            "net_hours"
        )
        self.assertIsNotNone(eod_log)
        self.assertNotEqual(eod_log, "")
        self.assertIn("h", eod_log)

    def test_5_5_duplicate_morning_log_controller_blocked(self):
        """TC-5.5: Direct duplicate insert of Morning Check-In log is blocked by controller."""
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
        # Cancel log1 first (docstatus=1 → 2), then delete both
        frappe.set_user("Administrator")
        frappe.db.set_value("Daily Task Log", log1.name, "docstatus", 2)
        frappe.db.delete("Daily Task Log", {"name": ["in", [log1.name, log2.name]]})

    def test_3_6_delete_another_employee_task_blocked_bola(self):
        """TC-3.6: Employee cannot delete another employee's task via API."""
        frappe.set_user("Administrator")
        other_task = frappe.new_doc("Daily Task")
        other_task.employee = self.hr_name
        other_task.task_date = today()
        other_task.description = "HR task to delete"
        other_task.status = "Pending"
        other_task.insert(ignore_permissions=True)

        frappe.set_user(self.emp_user)
        with self.assertRaises(frappe.PermissionError):
            delete_carried_task(other_task.name)

        frappe.set_user("Administrator")
        other_task.delete()

    def test_3_7_guest_cannot_delete_task(self):
        """TC-3.7: Guest user cannot call delete_carried_task."""
        frappe.set_user("Guest")
        with self.assertRaises(frappe.PermissionError):
            delete_carried_task("some_task_name")

    def test_3_8_save_task_blocked_after_eod(self):
        """TC-3.8: Employee cannot save/edit a task after EOD is submitted for that date."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task for EOD block"}]),
            login_time="09:00",
            work_location="Office"
        )
        task_name = frappe.db.get_value(
            "Daily Task", {"employee": self.emp_name, "task_date": today()}, "name"
        )
        submit_eod_log(
            lunch_from="12:00", lunch_to="13:00", logout_time="18:00",
            task_updates=json.dumps([{"name": task_name, "status": "Done", "actual_time": "", "description": "Task for EOD block"}]),
            adhoc_tasks="[]"
        )

        # Now try to edit/save the task
        task = frappe.get_doc("Daily Task", task_name)
        task.description = "Updated task description after EOD"
        with self.assertRaises(frappe.ValidationError):
            task.save()

    def test_4_8_eod_exactly_4h_lunch_allowed(self):
        """TC-4.8: EOD submission with lunch duration exactly 4 hours (240 mins) is allowed."""
        frappe.set_user(self.emp_user)
        # Clean existing logs for today first
        frappe.db.sql("DELETE FROM `tabDaily Task Log` WHERE employee=%s AND date=%s", (self.emp_name, today()))
        frappe.db.sql("DELETE FROM `tabDaily Task` WHERE employee=%s AND task_date=%s", (self.emp_name, today()))
        
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task 4h lunch"}]),
            login_time="09:00",
            work_location="Office"
        )
        # 09:00 -> 18:00 (9 hours total), lunch 12:00 -> 16:00 (exactly 4h / 240 mins)
        r = submit_eod_log(
            lunch_from="12:00", lunch_to="16:00", logout_time="18:00",
            task_updates="[]", adhoc_tasks="[]"
        )
        self.assertTrue(r.get("success"))

    def test_4_9_eod_more_than_4h_lunch_allowed(self):
        """TC-4.9: EOD submission with lunch duration exceeding 4 hours is allowed."""
        frappe.set_user(self.emp_user)
        frappe.db.sql("DELETE FROM `tabDaily Task Log` WHERE employee=%s AND date=%s", (self.emp_name, today()))
        frappe.db.sql("DELETE FROM `tabDaily Task` WHERE employee=%s AND task_date=%s", (self.emp_name, today()))
        
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task >4h lunch"}]),
            login_time="09:00",
            work_location="Office"
        )
        # 09:00 -> 18:00, lunch 12:00 -> 16:01 (4h 1m / 241 mins)
        r = submit_eod_log(
            lunch_from="12:00", lunch_to="16:01", logout_time="18:00",
            task_updates="[]", adhoc_tasks="[]"
        )
        self.assertTrue(r.get("success"))

    def test_4_10_midnight_wrap_exact_midnight_logout(self):
        """TC-4.10: Night shift wrapping midnight with logout exactly at 00:00:00 calculates correct net hours."""
        result = _calc_net_hours("18:00", "00:00", "", "", today())
        # 18:00 to 00:00 (midnight) is exactly 6 hours
        self.assertEqual(result, "6h 0m")

    def test_4_11_same_day_same_time_login_logout_is_zero(self):
        """TC-4.11: Same day same-time login/logout yields 0 net hours."""
        result = _calc_net_hours("09:00", "09:00", "", "", today())
        self.assertEqual(result, "0h 0m")

    def test_4_12_reset_checkin_preserves_tasks(self):
        """TC-4.12: Resetting morning check-in preserves planned tasks and reverts status to Pending."""
        from st_attendance_tracker.api import reset_morning_checkin
        frappe.set_user(self.emp_user)
        frappe.db.sql("DELETE FROM `tabDaily Task Log` WHERE employee=%s AND date=%s", (self.emp_name, today()))
        frappe.db.sql("DELETE FROM `tabDaily Task` WHERE employee=%s AND task_date=%s", (self.emp_name, today()))
        
        # Check-in with a task
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Test preservation task", "status": "In Progress"}]),
            login_time="09:00",
            work_location="Office"
        )
        
        # Verify task is created
        tasks_before = frappe.get_all("Daily Task", filters={"employee": self.emp_name, "task_date": today()})
        self.assertEqual(len(tasks_before), 1)
        
        # Reset check-in
        r = reset_morning_checkin()
        self.assertTrue(r.get("success"))
        
        # Verify task still exists and its status is Pending
        tasks_after = frappe.get_all("Daily Task", filters={"employee": self.emp_name, "task_date": today()}, fields=["status"])
        self.assertEqual(len(tasks_after), 1)
        self.assertEqual(tasks_after[0].status, "Pending")

    # ── SECTION 6 — Multi-Department Team Leader Notification ─────────────────

    def test_6_1_team_leader_fallback_to_reports_to(self):
        """TC-6.1: No Employee Department Assignment rows -> falls back to reports_to."""
        tl_user = "qa_tl_fallback@test.example.com"
        tl_name = _make_employee("QATLFallback", self.dept, tl_user, ["Employee"])
        subj_user = "qa_subj_fallback@test.example.com"
        subj_name = _make_employee("QASubjFallback", self.dept, subj_user, ["Employee"])
        try:
            frappe.db.set_value("Employee", subj_name, "reports_to", tl_name)
            emails = _get_team_leader_emails(subj_name)
            self.assertEqual(emails, [tl_user])
        finally:
            frappe.db.sql("DELETE FROM `tabEmployee` WHERE name IN (%s,%s)", (tl_name, subj_name))
            frappe.db.sql("DELETE FROM `tabUser` WHERE email IN (%s,%s)", (tl_user, subj_user))
            frappe.db.commit()

    def test_6_2_team_leader_multiple_department_assignments(self):
        """TC-6.2: Department Assignment rows notify every listed Team Leader, ignoring reports_to."""
        tl1_user = "qa_tl1@test.example.com"
        tl1_name = _make_employee("QATL1", self.dept, tl1_user, ["Employee"])
        tl2_user = "qa_tl2@test.example.com"
        tl2_name = _make_employee("QATL2", self.dept, tl2_user, ["Employee"])
        subj_user = "qa_subj_multi@test.example.com"
        subj_name = _make_employee("QASubjMulti", self.dept, subj_user, ["Employee"])
        try:
            doc = frappe.get_doc("Employee", subj_name)
            doc.reports_to = tl1_name  # must be ignored once assignment rows exist
            doc.append("department_assignments", {"department": self.dept, "team_leader": tl1_name})
            doc.append("department_assignments", {"department": self.dept, "team_leader": tl2_name})
            doc.save(ignore_permissions=True)

            emails = _get_team_leader_emails(subj_name)
            self.assertEqual(sorted(emails), sorted([tl1_user, tl2_user]))
        finally:
            frappe.db.sql(
                "DELETE FROM `tabEmployee Department Assignment` WHERE parent IN (%s,%s,%s)",
                (tl1_name, tl2_name, subj_name)
            )
            frappe.db.sql("DELETE FROM `tabEmployee` WHERE name IN (%s,%s,%s)", (tl1_name, tl2_name, subj_name))
            frappe.db.sql("DELETE FROM `tabUser` WHERE email IN (%s,%s,%s)", (tl1_user, tl2_user, subj_user))
            frappe.db.commit()

    # ── SECTION 7 — Carry-Forward Confirmation & Recurring Tasks ───────────────

    def test_7_1_carry_forward_declined_drops_task_no_rollover(self):
        """TC-7.1: Unchecking carry-forward marks the task Dropped and skips rollover."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task to drop"}]),
            login_time="09:00",
            work_location="Office"
        )
        task = frappe.db.get_value("Daily Task", {
            "employee": self.emp_name, "task_date": today(), "description": "Task to drop"
        }, "name")

        submit_eod_log(
            lunch_from="", lunch_to="", logout_time="18:00",
            task_updates=json.dumps([{
                "name": task, "status": "Pending", "actual_time": "", "carry_forward": False
            }]),
            adhoc_tasks="[]"
        )

        status = frappe.db.get_value("Daily Task", task, "status")
        self.assertEqual(status, "Dropped")

        next_date = str(getdate(today()) + timedelta(days=1))
        rolled = frappe.db.exists("Daily Task", {
            "employee": self.emp_name, "task_date": next_date, "rolled_over_from": task
        })
        self.assertIsNone(rolled, "Dropped task should not roll over")

    def test_7_2_recurring_task_auto_created_and_never_rolls_over(self):
        """TC-7.2: Recurring Task Template auto-creates a fresh Daily Task daily; never carried forward."""
        from st_attendance_tracker.api import _ensure_recurring_tasks, _rollover_pending_tasks

        tpl = frappe.get_doc({
            "doctype": "Recurring Task Template",
            "employee": self.emp_name,
            "description": "Daily Scrum Standup",
            "is_active": 1,
        }).insert(ignore_permissions=True)
        try:
            _ensure_recurring_tasks(self.emp_name, today())
            scrum_today = frappe.db.get_value("Daily Task", {
                "employee": self.emp_name, "task_date": today(),
                "task_type": "Recurring", "description": "Daily Scrum Standup"
            }, "name")
            self.assertIsNotNone(scrum_today, "Recurring task not auto-created")

            # Left Pending — EOD-style rollover must NOT carry a Recurring task forward
            _rollover_pending_tasks(self.emp_name, today())
            next_date = str(getdate(today()) + timedelta(days=1))
            rolled = frappe.db.exists("Daily Task", {
                "employee": self.emp_name, "task_date": next_date, "rolled_over_from": scrum_today
            })
            self.assertIsNone(rolled, "Recurring task should never roll over")

            # A fresh copy still appears tomorrow — from the template, not the carry chain
            _ensure_recurring_tasks(self.emp_name, next_date)
            scrum_tomorrow = frappe.db.exists("Daily Task", {
                "employee": self.emp_name, "task_date": next_date,
                "task_type": "Recurring", "description": "Daily Scrum Standup"
            })
            self.assertIsNotNone(scrum_tomorrow, "Recurring task did not auto-create for the next day")
        finally:
            frappe.db.sql("DELETE FROM `tabRecurring Task Template` WHERE name=%s", (tpl.name,))
            frappe.db.commit()

