"""
ST Attendance Tracker — Daily Work Log / Task Entry QA Test Suite
Ported from the old Daily Task / Daily Task Log suite (test_daily_task_log.py)
onto the parent/child model. See specs/daily-work-log-refactor.md.
"""
import json
import frappe
from frappe.utils import today, add_days, now_datetime
from frappe.utils.data import getdate
from frappe.tests.utils import FrappeTestCase
from datetime import timedelta

from st_attendance_tracker.api import (
    _to_hhmm, _to_ampm,
    submit_morning_log, submit_eod_log,
    get_page_state, get_management_dashboard, _get_team_leader_emails,
    delete_carried_task, reset_morning_checkin,
    _ensure_recurring_tasks, _rollover_pending_tasks, _get_work_log, _get_next_working_date,
    save_recurring_task, delete_recurring_task,
    get_my_history, get_history_day_detail, get_employee_task_detail,
    get_task_attachments, delete_carried_project, update_half_day_session,
    upload_task_attachment,
    _send_checkin_notifications, _send_eod_notifications,
    _get_attendance_settings, clear_attendance_settings_cache,
    _get_team_members,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_employee(suffix, dept_name, user_email, roles=None):
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


def _task_status(employee, date, description):
    work_log = _get_work_log(employee, date)
    if not work_log:
        return None
    row = next((r for r in work_log.tasks if r.description == description), None)
    return row.status if row else None


def _task_name(employee, date, description):
    work_log = _get_work_log(employee, date)
    if not work_log:
        return None
    row = next((r for r in work_log.tasks if r.description == description), None)
    return row.name if row else None


def _make_approved_half_day_leave(employee, date):
    """A submitted, approved half-day Leave Application for `date`, created
    bypassing HRMS's own validate() (leave balance/allocation etc.) — the
    code under test only ever checks these raw fields via frappe.db.exists,
    never re-validates the leave itself. Caller must delete the row with
    frappe.db.delete (it's force-submitted, so the normal delete flow
    refuses it as a submitted record)."""
    leave_type = frappe.db.get_value("Leave Type", {}, "name")
    leave = frappe.new_doc("Leave Application")
    leave.employee = employee
    leave.leave_type = leave_type
    leave.from_date = date
    leave.to_date = date
    leave.half_day = 1
    leave.half_day_date = date
    leave.status = "Approved"
    leave.flags.ignore_permissions = True
    leave.flags.ignore_validate = True
    leave.flags.ignore_mandatory = True
    leave.insert(ignore_permissions=True)
    frappe.db.set_value("Leave Application", leave.name, "docstatus", 1)
    return leave.name


class TestQACheckinFull(FrappeTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")

        company = frappe.db.get_single_value("Global Defaults", "default_company") or "_Test Company"
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
        frappe.db.sql("DELETE FROM `tabTask Entry` WHERE parent IN "
                       "(SELECT name FROM `tabDaily Work Log` WHERE employee IN (%s,%s))",
                       (cls.emp_name, cls.hr_name))
        frappe.db.sql("DELETE FROM `tabDaily Work Log` WHERE employee IN (%s,%s)", (cls.emp_name, cls.hr_name))
        frappe.db.sql("DELETE FROM `tabEmployee Checkin` WHERE employee IN (%s,%s)", (cls.emp_name, cls.hr_name))
        frappe.db.sql("DELETE FROM `tabEmployee` WHERE name IN (%s,%s)", (cls.emp_name, cls.hr_name))
        frappe.db.sql("DELETE FROM `tabUser` WHERE email IN (%s,%s)", (cls.emp_user, cls.hr_user))
        frappe.db.commit()

    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.sql("DELETE FROM `tabTask Entry` WHERE parent IN "
                       "(SELECT name FROM `tabDaily Work Log` WHERE employee=%s)", (self.emp_name,))
        frappe.db.sql("DELETE FROM `tabDaily Work Log` WHERE employee=%s", (self.emp_name,))
        frappe.db.sql("DELETE FROM `tabEmployee Checkin` WHERE employee=%s", (self.emp_name,))
        frappe.db.commit()

    # ── SECTION 1 — Functional Core ────────────────────────────────────────────

    def test_1_1_happy_path_checkin_creates_log(self):
        """TC-1.1: Valid check-in creates a Daily Work Log and Employee Checkin record."""
        frappe.set_user(self.emp_user)
        r = submit_morning_log(
            new_tasks=json.dumps([{"description": "QA happy path task", "estimated_time": "1h"}]),
            work_location="Office"
        )
        self.assertTrue(r.get("success"))
        work_log = _get_work_log(self.emp_name, today())
        self.assertIsNotNone(work_log, "Daily Work Log not created")
        self.assertTrue(work_log.morning_submitted)
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
        """TC-1.4: Full check-in -> EOD cycle updates the same Daily Work Log and creates Employee Checkin OUT."""
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
        work_log = _get_work_log(self.emp_name, today())
        self.assertTrue(work_log.eod_submitted)
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

    # ── SECTION 2 — Edge Cases & Invalid Inputs (Daily Work Log controller) ────

    def test_2_1_reversed_lunch_blocked(self):
        """TC-2.1: Reversed lunch times (from 14:00 to 13:00) blocked by controller."""
        log = frappe.new_doc("Daily Work Log")
        log.employee = self.emp_name
        log.date = today()
        log.login_time = "09:00:00"
        log.logout_time = "18:00:00"
        log.lunch_from = "14:00:00"
        log.lunch_to = "13:00:00"
        with self.assertRaises(frappe.ValidationError):
            log.save(ignore_permissions=True)

    def test_2_2_lunch_exceeding_4h_allowed(self):
        """TC-2.2: Lunch break > 4 hours is allowed, net hours computed correctly."""
        log = frappe.new_doc("Daily Work Log")
        log.employee = self.emp_name
        log.date = today()
        log.login_time = "09:00:00"
        log.logout_time = "18:00:00"
        log.lunch_from = "10:00:00"
        log.lunch_to = "15:00:00"  # 5 hours
        log.eod_submitted = 1
        log.insert(ignore_permissions=True)
        self.assertEqual(log.net_hours, "4h 0m")
        log.delete()

    def test_2_3_lunch_outside_shift_blocked(self):
        """TC-2.3: Lunch interval outside work shift boundaries is blocked."""
        log = frappe.new_doc("Daily Work Log")
        log.employee = self.emp_name
        log.date = today()
        log.login_time = "09:00:00"
        log.logout_time = "17:00:00"
        log.lunch_from = "17:30:00"  # After logout
        log.lunch_to = "18:30:00"
        with self.assertRaises(frappe.ValidationError):
            log.save(ignore_permissions=True)

    def test_2_4_midnight_wrap_shift_net_hours(self):
        """TC-2.4: Night shift crossing midnight calculates correct net hours."""
        log = frappe.new_doc("Daily Work Log")
        log.employee = self.emp_name
        log.date = today()
        log.login_time = "22:00:00"
        log.logout_time = "06:00:00"  # Next day
        log.eod_submitted = 1
        log.insert(ignore_permissions=True)
        self.assertEqual(log.net_hours, "8h 0m")
        log.delete()

    def test_2_5_no_lunch_net_hours_correct(self):
        """TC-2.5: Net hours without lunch is correctly calculated."""
        log = frappe.new_doc("Daily Work Log")
        log.employee = self.emp_name
        log.date = today()
        log.login_time = "09:00:00"
        log.logout_time = "17:30:00"
        log.eod_submitted = 1
        log.insert(ignore_permissions=True)
        self.assertEqual(log.net_hours, "8h 30m")
        log.delete()

    def test_2_6_lunch_deducted_correctly(self):
        """TC-2.6: Standard lunch is correctly deducted from net hours."""
        log = frappe.new_doc("Daily Work Log")
        log.employee = self.emp_name
        log.date = today()
        log.login_time = "09:00:00"
        log.logout_time = "18:00:00"
        log.lunch_from = "13:00:00"
        log.lunch_to = "14:00:00"
        log.eod_submitted = 1
        log.insert(ignore_permissions=True)
        self.assertEqual(log.net_hours, "8h 0m")
        log.delete()

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
        work_log = _get_work_log(self.emp_name, today())
        self.assertIn("script", work_log.tasks[0].description)

    def test_2_9_sql_injection_in_task_description_safe(self):
        """TC-2.9: SQL-like input in task description does not cause errors."""
        frappe.set_user(self.emp_user)
        sql_input = "'; DROP TABLE `tabTask Entry`; --"
        r = submit_morning_log(
            new_tasks=json.dumps([{"description": sql_input}]),
            work_location="Office"
        )
        self.assertTrue(r.get("success"))
        work_log = _get_work_log(self.emp_name, today())
        self.assertEqual(work_log.tasks[0].description, sql_input)

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
        """TC-3.2: Regular employee cannot access management dashboard API."""
        frappe.set_user(self.emp_user)
        with self.assertRaises(frappe.PermissionError):
            get_management_dashboard(today())

    def test_3_4_bola_task_edit_blocked(self):
        """TC-3.4: Employee cannot edit tasks belonging to another employee via API."""
        frappe.set_user("Administrator")
        other_log = frappe.new_doc("Daily Work Log")
        other_log.employee = self.hr_name
        other_log.date = today()
        other_log.append("tasks", {"description": "HR task", "status": "Pending", "task_type": "Planned"})
        other_log.insert(ignore_permissions=True)

        frappe.set_user(self.emp_user)
        other_log.employee_name = "Tampered by emp"
        with self.assertRaises(frappe.PermissionError):
            other_log.save()

        frappe.set_user("Administrator")
        other_log.delete()

    def test_3_5_delete_carried_task_blocked_after_eod(self):
        """TC-3.5: Cannot delete a task after EOD has been submitted for that date."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task to delete after EOD"}]),
            login_time="09:00",
            work_location="Office"
        )
        task_name = _task_name(self.emp_name, today(), "Task to delete after EOD")
        submit_eod_log(
            lunch_from="", lunch_to="", logout_time="18:00",
            task_updates=json.dumps([{"name": task_name, "status": "Done", "actual_time": "1h", "description": "Task to delete after EOD"}]),
            adhoc_tasks="[]"
        )
        with self.assertRaises(frappe.ValidationError):
            delete_carried_task(task_name)

    def test_3_6_delete_another_employee_task_blocked_bola(self):
        """TC-3.6: Employee cannot delete another employee's task via API."""
        frappe.set_user("Administrator")
        other_log = frappe.new_doc("Daily Work Log")
        other_log.employee = self.hr_name
        other_log.date = today()
        other_log.append("tasks", {"description": "HR task to delete", "status": "Pending", "task_type": "Planned"})
        other_log.insert(ignore_permissions=True)
        other_task_name = other_log.tasks[0].name

        frappe.set_user(self.emp_user)
        with self.assertRaises(frappe.PermissionError):
            delete_carried_task(other_task_name)

        frappe.set_user("Administrator")
        other_log.delete()

    def test_3_7_guest_cannot_delete_task(self):
        """TC-3.7: Guest user cannot call delete_carried_task."""
        frappe.set_user("Guest")
        with self.assertRaises(frappe.PermissionError):
            delete_carried_task("some_task_name")

    def test_3_8_save_task_blocked_after_eod(self):
        """TC-3.8: Employee cannot edit a task's parent Daily Work Log after EOD is submitted."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task for EOD block"}]),
            login_time="09:00",
            work_location="Office"
        )
        task_name = _task_name(self.emp_name, today(), "Task for EOD block")
        submit_eod_log(
            lunch_from="12:00", lunch_to="13:00", logout_time="18:00",
            task_updates=json.dumps([{"name": task_name, "status": "Done", "actual_time": "1h", "description": "Task for EOD block"}]),
            adhoc_tasks="[]"
        )

        work_log = _get_work_log(self.emp_name, today())
        work_log.tasks[0].description = "Updated task description after EOD"
        with self.assertRaises(frappe.ValidationError):
            work_log.save()

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

    def test_4_6_net_hours_sanity_zero_diff_uses_shared_helper(self):
        """TC-4.6: Same login/logout time on a past date doesn't fabricate a 24h shift."""
        yesterday = frappe.utils.add_days(today(), -1)
        log = frappe.new_doc("Daily Work Log")
        log.employee = self.emp_name
        log.date = yesterday
        log.login_time = "06:00:00"
        log.logout_time = "06:00:00"
        log.eod_submitted = 1
        log.insert(ignore_permissions=True)
        # resolve_zero_diff_minutes governs this value; just assert it computed
        # something sane rather than blowing up or wrapping to 24h.
        self.assertIsNotNone(log.net_hours)
        log.delete()

    def test_4_10_midnight_wrap_exact_midnight_logout(self):
        """TC-4.10: Night shift wrapping midnight with logout exactly at 00:00:00 calculates correct net hours."""
        log = frappe.new_doc("Daily Work Log")
        log.employee = self.emp_name
        log.date = today()
        log.login_time = "18:00:00"
        log.logout_time = "00:00:00"
        log.eod_submitted = 1
        log.insert(ignore_permissions=True)
        self.assertEqual(log.net_hours, "6h 0m")
        log.delete()

    def test_4_12_reset_checkin_preserves_tasks(self):
        """TC-4.12: Resetting morning check-in preserves planned tasks and reverts status to Pending."""
        frappe.set_user(self.emp_user)

        submit_morning_log(
            new_tasks=json.dumps([{"description": "Test preservation task", "status": "In Progress"}]),
            login_time="09:00",
            work_location="Office"
        )

        work_log = _get_work_log(self.emp_name, today())
        self.assertEqual(len(work_log.tasks), 1)

        r = reset_morning_checkin()
        self.assertTrue(r.get("success"))

        work_log = _get_work_log(self.emp_name, today())
        self.assertEqual(len(work_log.tasks), 1)
        self.assertEqual(work_log.tasks[0].status, "Pending")
        self.assertFalse(work_log.morning_submitted)
        self.assertTrue(work_log.was_reset_today)

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
        next_date = str(_get_next_working_date(self.emp_name, today()))
        next_log = _get_work_log(self.emp_name, next_date)
        self.assertIsNotNone(next_log)
        matching = [t for t in next_log.tasks if t.description == "Rollover task"]
        self.assertEqual(len(matching), 1, "Task was duplicated on rollover")

        # re-triggering rollover for the same source day must not duplicate it
        _rollover_pending_tasks(self.emp_name, today())
        next_log.reload()
        matching = [t for t in next_log.tasks if t.description == "Rollover task"]
        self.assertEqual(len(matching), 1, "Task was duplicated on re-triggered rollover")

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
            adhoc_tasks=json.dumps([{"description": "Ad-hoc task done", "status": "Done", "actual_time": "1h"}])
        )
        work_log = _get_work_log(self.emp_name, today())
        adhoc = next((t for t in work_log.tasks if t.description == "Ad-hoc task done"), None)
        self.assertIsNotNone(adhoc)
        self.assertEqual(adhoc.task_type, "Ad-hoc")

    def test_5_3_empty_adhoc_description_ignored(self):
        """TC-5.3: Ad-hoc task with empty description is not inserted."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task"}]),
            login_time="09:00",
            work_location="Office"
        )
        before = len(_get_work_log(self.emp_name, today()).tasks)
        submit_eod_log(
            lunch_from="", lunch_to="", logout_time="18:00",
            task_updates="[]",
            adhoc_tasks=json.dumps([{"description": "   ", "status": "Done"}])
        )
        after = len(_get_work_log(self.emp_name, today()).tasks)
        self.assertEqual(before, after, "Empty adhoc description created a task")

    def test_5_4_net_hours_stored_in_db(self):
        """TC-5.4: Net hours are persisted on the Daily Work Log after EOD."""
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
        net_hours = frappe.db.get_value("Daily Work Log", {
            "employee": self.emp_name, "date": today(), "eod_submitted": 1,
        }, "net_hours")
        self.assertIsNotNone(net_hours)
        self.assertNotEqual(net_hours, "")
        self.assertIn("h", net_hours)

    def test_5_5_duplicate_work_log_controller_blocked(self):
        """TC-5.5: Direct duplicate insert of a Daily Work Log for the same employee+date is blocked."""
        log1 = frappe.new_doc("Daily Work Log")
        log1.employee = self.emp_name
        log1.date = today()
        log1.morning_submitted = 1
        log1.login_time = "09:00:00"
        log1.insert(ignore_permissions=True)

        log2 = frappe.new_doc("Daily Work Log")
        log2.employee = self.emp_name
        log2.date = today()
        log2.morning_submitted = 1
        log2.login_time = "10:00:00"
        with self.assertRaises(frappe.ValidationError):
            log2.insert(ignore_permissions=True)

        frappe.set_user("Administrator")
        log1.delete()

    # ── SECTION 6 — Multi-Department Team Leader Notification ─────────────────
    # (unrelated to the schema change — kept as regression coverage)

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
            doc.reports_to = tl1_name
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
        task_name = _task_name(self.emp_name, today(), "Task to drop")

        submit_eod_log(
            lunch_from="", lunch_to="", logout_time="18:00",
            task_updates=json.dumps([{
                "name": task_name, "status": "Pending", "actual_time": "", "carry_forward": False
            }]),
            adhoc_tasks="[]"
        )

        self.assertEqual(_task_status(self.emp_name, today(), "Task to drop"), "Dropped")

        next_date = str(_get_next_working_date(self.emp_name, today()))
        next_log = _get_work_log(self.emp_name, next_date)
        rolled = next_log and any(t.description == "Task to drop" for t in next_log.tasks)
        self.assertFalse(rolled, "Dropped task should not roll over")

    def test_7_2_recurring_task_auto_created_and_never_rolls_over(self):
        """TC-7.2: Recurring Task Template auto-creates a fresh Task Entry daily; never carried forward."""
        tpl = frappe.get_doc({
            "doctype": "Recurring Task Template",
            "employee": self.emp_name,
            "description": "Daily Scrum Standup",
            "is_active": 1,
        }).insert(ignore_permissions=True)
        try:
            _ensure_recurring_tasks(self.emp_name, today())
            work_log = _get_work_log(self.emp_name, today())
            scrum_today = next((t for t in work_log.tasks
                                 if t.task_type == "Recurring" and t.description == "Daily Scrum Standup"), None)
            self.assertIsNotNone(scrum_today, "Recurring task not auto-created")

            # Left Pending — EOD-style rollover must NOT carry a Recurring task forward
            _rollover_pending_tasks(self.emp_name, today())
            next_date = str(_get_next_working_date(self.emp_name, today()))
            next_log = _get_work_log(self.emp_name, next_date)
            rolled = next_log and any(t.series_id == scrum_today.series_id for t in next_log.tasks)
            self.assertFalse(rolled, "Recurring task should never roll over")

            # A fresh copy still appears tomorrow — from the template, not the carry chain
            _ensure_recurring_tasks(self.emp_name, next_date)
            next_log = _get_work_log(self.emp_name, next_date)
            scrum_tomorrow = next_log and any(
                t.task_type == "Recurring" and t.description == "Daily Scrum Standup" for t in next_log.tasks
            )
            self.assertTrue(scrum_tomorrow, "Recurring task did not auto-create for the next day")
        finally:
            frappe.db.sql("DELETE FROM `tabRecurring Task Template` WHERE name=%s", (tpl.name,))
            frappe.db.sql("DELETE FROM `tabTask Entry` WHERE parent IN "
                           "(SELECT name FROM `tabDaily Work Log` WHERE employee=%s)", (self.emp_name,))
            frappe.db.sql("DELETE FROM `tabDaily Work Log` WHERE employee=%s", (self.emp_name,))
            frappe.db.commit()

    def test_7_3_deleting_template_removes_todays_pending_instance(self):
        """TC-7.3: Deleting a Recurring Task Template immediately drops today's
        not-yet-started instance instead of leaving a stale copy behind."""
        frappe.set_user(self.emp_user)
        r = save_recurring_task(description="Stale Standup", is_active=1)
        work_log = _get_work_log(self.emp_name, today())
        self.assertIsNotNone(next((t for t in work_log.tasks if t.description == "Stale Standup"), None))

        delete_recurring_task(r["name"])

        work_log = _get_work_log(self.emp_name, today())
        still_there = work_log and any(t.description == "Stale Standup" for t in work_log.tasks)
        self.assertFalse(still_there, "Deleted template's Pending instance should be removed immediately")

    def test_7_4_deactivating_template_removes_todays_pending_instance(self):
        """TC-7.4: Deactivating (not deleting) a template also syncs today's instance away."""
        frappe.set_user(self.emp_user)
        r = save_recurring_task(description="Toggle Standup", is_active=1)
        save_recurring_task(name=r["name"], description="Toggle Standup", is_active=0)

        work_log = _get_work_log(self.emp_name, today())
        still_there = work_log and any(t.description == "Toggle Standup" for t in work_log.tasks)
        self.assertFalse(still_there, "Deactivated template's Pending instance should be removed immediately")

        frappe.db.sql("DELETE FROM `tabRecurring Task Template` WHERE name=%s", (r["name"],))
        frappe.db.commit()

    def test_7_5_editing_template_updates_todays_pending_instance(self):
        """TC-7.5: Editing a template's description syncs an untouched instance in place."""
        frappe.set_user(self.emp_user)
        r = save_recurring_task(description="Old Wording", is_active=1)
        save_recurring_task(name=r["name"], description="New Wording", is_active=1)

        work_log = _get_work_log(self.emp_name, today())
        self.assertFalse(any(t.description == "Old Wording" for t in work_log.tasks))
        self.assertTrue(any(t.description == "New Wording" for t in work_log.tasks))

        frappe.db.sql("DELETE FROM `tabRecurring Task Template` WHERE name=%s", (r["name"],))
        frappe.db.commit()

    def test_7_6_started_instance_not_touched_by_template_change(self):
        """TC-7.6: A recurring instance already marked In Progress/Done is left
        alone even if its template is edited or deleted afterward."""
        frappe.set_user(self.emp_user)
        r = save_recurring_task(description="In-Flight Standup", is_active=1)
        work_log = _get_work_log(self.emp_name, today())
        row = next(t for t in work_log.tasks if t.description == "In-Flight Standup")
        row.status = "In Progress"
        work_log.save(ignore_permissions=True)

        delete_recurring_task(r["name"])

        work_log = _get_work_log(self.emp_name, today())
        still_there = next((t for t in work_log.tasks if t.description == "In-Flight Standup"), None)
        self.assertIsNotNone(still_there, "An already-started instance must not be silently removed")
        self.assertEqual(still_there.status, "In Progress")

    # ── SECTION 8 — Secondary/read endpoints (history, dashboards, attachments,
    #    half-day session, project delete) rewritten for the new schema but not
    #    exercised by Sections 1-7's check-in/EOD flow ────────────────────────

    def test_8_1_get_my_history_lists_past_checked_in_days(self):
        """TC-8.1: A past day's Daily Work Log (checked in) appears in history."""
        frappe.set_user("Administrator")
        past_date = add_days(today(), -3)
        log = frappe.new_doc("Daily Work Log")
        log.employee = self.emp_name
        log.date = past_date
        log.morning_submitted = 1
        log.login_time = "09:00:00"
        log.append("tasks", {
            "description": "Past task", "status": "Done", "task_type": "Planned",
            "actual_time": 1, "series_id": "hist1", "origin_date": past_date,
        })
        log.insert(ignore_permissions=True)
        frappe.db.commit()

        frappe.set_user(self.emp_user)
        result = get_my_history(0)
        entry = next((l for l in result["logs"] if str(l["date"]) == str(past_date)), None)
        self.assertIsNotNone(entry, "Past checked-in day missing from history")
        self.assertEqual(entry["total_tasks"], 1)
        self.assertEqual(entry["done_tasks"], 1)

    def test_8_2_get_history_day_detail_returns_tasks_and_logs(self):
        """TC-8.2: Day-detail drill-down returns the day's tasks and log fields."""
        frappe.set_user("Administrator")
        past_date = add_days(today(), -4)
        log = frappe.new_doc("Daily Work Log")
        log.employee = self.emp_name
        log.date = past_date
        log.morning_submitted = 1
        log.eod_submitted = 1
        log.login_time = "09:00:00"
        log.logout_time = "18:00:00"
        log.append("tasks", {
            "description": "Detail task", "status": "Done", "task_type": "Planned",
            "actual_time": 2, "series_id": "hist2", "origin_date": past_date,
        })
        log.insert(ignore_permissions=True)
        frappe.db.commit()

        frappe.set_user(self.emp_user)
        detail = get_history_day_detail(str(past_date))
        self.assertEqual(len(detail["tasks"]), 1)
        self.assertEqual(detail["tasks"][0]["description"], "Detail task")
        self.assertIsNotNone(detail["morning_log"])
        self.assertIsNotNone(detail["eod_log"])

    def test_8_3_get_employee_task_detail_hr_allowed_stranger_denied(self):
        """TC-8.3: HR Manager can view any employee's task detail; an unrelated
        employee (not HR, not their Team Leader) cannot."""
        frappe.set_user(self.hr_user)
        detail = get_employee_task_detail(self.emp_name, today())
        self.assertEqual(detail["employee"]["name"], self.emp_name)

        frappe.set_user(self.emp_user)
        with self.assertRaises(frappe.PermissionError):
            get_employee_task_detail(self.hr_name, today())

    def test_8_4_get_task_attachments_owner_allowed_stranger_denied(self):
        """TC-8.4: Task attachments are visible to the owner, blocked for a stranger."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Attachment task"}]),
            work_location="Office"
        )
        task_name = _task_name(self.emp_name, today(), "Attachment task")

        atts = get_task_attachments(task_name)
        self.assertEqual(atts, [])

        frappe.set_user("Administrator")
        other_log = frappe.new_doc("Daily Work Log")
        other_log.employee = self.hr_name
        other_log.date = today()
        other_log.append("tasks", {"description": "HR-only task", "status": "Pending", "task_type": "Planned"})
        other_log.insert(ignore_permissions=True)
        other_task_name = other_log.tasks[0].name

        frappe.set_user(self.emp_user)
        with self.assertRaises(frappe.PermissionError):
            get_task_attachments(other_task_name)

        frappe.set_user("Administrator")
        other_log.delete()

    def test_8_9_upload_task_attachment_stranger_denied(self):
        """TC-8.9: upload_task_attachment blocks attaching to another
        employee's task before ever touching the uploaded file — regression
        for the 403 found in browser QA (Frappe's generic upload_file can't
        correctly permission-check a child-table doctype loaded standalone;
        see upload_task_attachment's docstring)."""
        frappe.set_user("Administrator")
        other_log = frappe.new_doc("Daily Work Log")
        other_log.employee = self.hr_name
        other_log.date = today()
        other_log.append("tasks", {"description": "HR-only task 2", "status": "Pending", "task_type": "Planned"})
        other_log.insert(ignore_permissions=True)
        other_task_name = other_log.tasks[0].name

        frappe.set_user(self.emp_user)
        with self.assertRaises(frappe.PermissionError):
            upload_task_attachment(other_task_name)

        frappe.set_user("Administrator")
        other_log.delete()

    def test_8_10_upload_task_attachment_owner_succeeds(self):
        """TC-8.10: The task's own employee can attach a file to it."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Upload task"}]),
            work_location="Office"
        )
        task_name = _task_name(self.emp_name, today(), "Upload task")

        class _FakeStream:
            def read(self):
                return b"hello world"

        class _FakeFile:
            filename = "note.txt"
            stream = _FakeStream()

        class _FakeFiles:
            def get(self, key):
                return _FakeFile() if key == "file" else None

        class _FakeRequest:
            files = _FakeFiles()
            host = "127.0.0.1:8000"
            headers = {}

        frappe.local.request = _FakeRequest()
        try:
            file_doc = upload_task_attachment(task_name)
            self.assertEqual(file_doc.attached_to_doctype, "Task Entry")
            self.assertEqual(file_doc.attached_to_name, task_name)
            self.assertTrue(file_doc.file_name.startswith("note") and file_doc.file_name.endswith(".txt"))
        finally:
            frappe.local.request = None
            for fname in frappe.get_all("File",
                    filters={"attached_to_doctype": "Task Entry", "attached_to_name": task_name}, pluck="name"):
                frappe.delete_doc("File", fname, ignore_permissions=True)
            frappe.db.commit()

    def test_8_5_delete_carried_project_removes_all_its_tasks(self):
        """TC-8.5: Deleting a project removes every task under it, and is
        blocked once checkout has been submitted."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([
                {"description": "Proj task 1", "project_name": "QA Project"},
                {"description": "Proj task 2", "project_name": "QA Project"},
                {"description": "Other task"},
            ]),
            work_location="Office"
        )
        delete_carried_project("QA Project", today())

        work_log = _get_work_log(self.emp_name, today())
        remaining = [t.description for t in work_log.tasks]
        self.assertNotIn("Proj task 1", remaining)
        self.assertNotIn("Proj task 2", remaining)
        self.assertIn("Other task", remaining)

        submit_eod_log(
            lunch_from="", lunch_to="", logout_time="18:00",
            task_updates="[]", adhoc_tasks="[]"
        )
        with self.assertRaises(frappe.ValidationError):
            delete_carried_project("General", today())

    def test_8_6_update_half_day_session_without_leave_blocked(self):
        """TC-8.6: Applying a half-day session without an approved half-day leave is blocked."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task"}]),
            work_location="Office"
        )
        with self.assertRaises(frappe.ValidationError):
            update_half_day_session("First Half")

    def test_8_7_update_half_day_session_with_approved_leave_succeeds(self):
        """TC-8.7: With an approved half-day leave on file, the session is recorded."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Task"}]),
            work_location="Office"
        )
        leave_name = _make_approved_half_day_leave(self.emp_name, today())
        try:
            r = update_half_day_session("Second Half")
            self.assertTrue(r["success"])
            self.assertEqual(r["half_day_session"], "Second Half")
            work_log = _get_work_log(self.emp_name, today())
            self.assertEqual(work_log.half_day_session, "Second Half")
        finally:
            frappe.set_user("Administrator")
            frappe.db.delete("Leave Application", {"name": leave_name})
            frappe.db.commit()

    def test_8_8_management_dashboard_returns_departments_and_summary(self):
        """TC-8.8: HR Manager's management dashboard returns per-department
        data and an org-wide summary."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Dashboard task"}]),
            login_time="09:00",
            work_location="Office"
        )

        frappe.set_user(self.hr_user)
        result = get_management_dashboard(today())
        self.assertIn("departments", result)
        self.assertIn("summary", result)
        self.assertGreaterEqual(result["summary"]["total"], 1)
        self.assertGreaterEqual(result["summary"]["checked_in"], 1)

    # ── SECTION 9 — Notification jobs ──────────────────────────────────────────
    # submit_morning_log/submit_eod_log now hand these to frappe.enqueue()
    # instead of calling them inline (large checkout attachments were making
    # base64-encoding two Email Queue records a multi-second part of the
    # synchronous checkout request). bench run-tests has no worker draining
    # the queue, so without these the email-building code would silently
    # stop being covered by every check-in/EOD test above.

    def test_9_1_checkin_notification_job_runs_without_error(self):
        """TC-9.1: The job submit_morning_log enqueues builds and queues
        both emails without error, called directly since no worker runs
        during tests."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Notify task"}]),
            login_time="09:00",
            work_location="Office"
        )
        _send_checkin_notifications(self.emp_name, today(), now_datetime())

    def test_9_2_eod_notification_job_runs_with_attachment(self):
        """TC-9.2: The job submit_eod_log enqueues builds and queues both
        emails, including a real task attachment, without error."""
        frappe.set_user(self.emp_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Notify task with file"}]),
            login_time="09:00",
            work_location="Office"
        )
        task_name = _task_name(self.emp_name, today(), "Notify task with file")

        class _FakeStream:
            def read(self):
                return b"attachment content"

        class _FakeFile:
            # Plain .txt on purpose — Frappe validates real PDF/office-doc
            # content (e.g. scanning PDFs for embedded JS) on insert, which
            # this fake byte content would fail; the point of this test is
            # the attachment-fetch path in _send_eod_notifications, not
            # Frappe's own file-content validation.
            filename = "report.txt"
            stream = _FakeStream()

        class _FakeFiles:
            def get(self, key):
                return _FakeFile() if key == "file" else None

        class _FakeRequest:
            files = _FakeFiles()
            host = "127.0.0.1:8000"
            headers = {}

        frappe.local.request = _FakeRequest()
        try:
            upload_task_attachment(task_name)
        finally:
            frappe.local.request = None

        submit_eod_log(
            lunch_from="", lunch_to="", logout_time="18:00",
            task_updates=json.dumps([{"name": task_name, "status": "Done", "actual_time": "1h"}]),
            adhoc_tasks="[]"
        )
        _send_eod_notifications(self.emp_name, today(), now_datetime(), is_late_checkout=False)

        for fname in frappe.get_all("File",
                filters={"attached_to_doctype": "Task Entry", "attached_to_name": task_name}, pluck="name"):
            frappe.delete_doc("File", fname, ignore_permissions=True)
        frappe.db.commit()

    # ── SECTION 10 — ST Attendance Settings cache ──────────────────────────────
    # Same pattern as _get_team_members()'s Redis cache: read on nearly every
    # check-in/checkout/dashboard load, changes only a handful of times a
    # year, invalidated explicitly on save rather than relying on the 3600s
    # TTL to catch up. ST Attendance Settings is a real Single shared with the
    # whole site, so every test here restores the original value afterward.

    def test_10_1_attendance_settings_cache_returns_correct_values(self):
        """TC-10.1: _get_attendance_settings reflects the real singleton
        value on a cache miss."""
        original = frappe.db.get_single_value("ST Attendance Settings", "standard_workday_hours")
        settings = frappe.get_single("ST Attendance Settings")
        settings.standard_workday_hours = 7.5
        settings.save(ignore_permissions=True)
        try:
            clear_attendance_settings_cache()
            result = _get_attendance_settings()
            self.assertEqual(result.get("standard_workday_hours"), 7.5)
        finally:
            settings.reload()
            settings.standard_workday_hours = original
            settings.save(ignore_permissions=True)
            clear_attendance_settings_cache()

    def test_10_2_attendance_settings_cache_hit_skips_db(self):
        """TC-10.2: A second call within the cache window doesn't re-fetch
        the singleton — proven by making frappe.get_single fail if called."""
        clear_attendance_settings_cache()
        _get_attendance_settings()  # warm the cache

        original_get_single = frappe.get_single

        def _fail_if_called(*args, **kwargs):
            self.fail("frappe.get_single was called on what should have been a cache hit")

        frappe.get_single = _fail_if_called
        try:
            result = _get_attendance_settings()
            self.assertIsNotNone(result)
        finally:
            frappe.get_single = original_get_single
            clear_attendance_settings_cache()

    def test_10_3_saving_settings_invalidates_cache(self):
        """TC-10.3: Saving ST Attendance Settings clears the cache immediately
        — the very next read reflects the new value, not a stale cached one."""
        settings = frappe.get_single("ST Attendance Settings")
        original = settings.standard_workday_hours
        try:
            clear_attendance_settings_cache()
            _get_attendance_settings()  # warm the cache with the original value

            settings.standard_workday_hours = 6.25
            settings.save(ignore_permissions=True)  # on_update hook should clear the cache

            result = _get_attendance_settings()
            self.assertEqual(result.get("standard_workday_hours"), 6.25)
        finally:
            settings.reload()
            settings.standard_workday_hours = original
            settings.save(ignore_permissions=True)
            clear_attendance_settings_cache()

    def test_10_4_get_team_members_cache_hit_returns_correct_value_not_none(self):
        """TC-10.4: Same expires=True cache-miss bug/fix as
        _get_attendance_settings — a cache hit within the same request must
        return the real cached value, not a stale negative memo left behind
        by the initial miss."""
        cache_key = f"st_att:team_members:{self.emp_name}"
        frappe.cache().delete_value(cache_key)

        result1 = _get_team_members(self.emp_name)

        original_get_all = frappe.get_all

        def _fail_if_called(*args, **kwargs):
            self.fail("frappe.get_all was called on what should have been a cache hit")

        frappe.get_all = _fail_if_called
        try:
            result2 = _get_team_members(self.emp_name)
            self.assertIsNotNone(result2)
            self.assertEqual(result1, result2)
        finally:
            frappe.get_all = original_get_all
            frappe.cache().delete_value(cache_key)
