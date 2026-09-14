"""Team Leader task assignment (assign_task_to_employee) — the real,
logged-in-user counterpart to the bot-only assign_task_via_agent. Also
covers the assigned_by/assigned_by_name stamp both paths now share."""
import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from st_attendance_tracker.api import (
    assign_task_to_employee, assign_task_via_agent, submit_morning_log, _get_work_log, _task_entry_dict,
)
from st_attendance_tracker.st_attendance_tracker.doctype.daily_work_log.test_daily_work_log import _make_employee


class TestAssignTaskToEmployee(FrappeTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")

        company = frappe.db.get_single_value("Global Defaults", "default_company") or "_Test Company"
        cls.dept = (
            frappe.db.get_value("Department", {"department_name": "_QA Dept", "company": company}, "name")
            or f"_QA Dept - {company}"
        )

        cls.leader_user = "assign_tl@test.example.com"
        cls.report_user = "assign_emp@test.example.com"
        cls.stranger_user = "assign_stranger@test.example.com"

        cls.leader_name = _make_employee("AssignTL", cls.dept, cls.leader_user, ["Employee"])
        cls.report_name = _make_employee("AssignEmp", cls.dept, cls.report_user, ["Employee"])
        cls.stranger_name = _make_employee("AssignStranger", cls.dept, cls.stranger_user, ["Employee"])

        frappe.db.set_value("Employee", cls.report_name, "reports_to", cls.leader_name)
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.set_user("Administrator")
        for emp in (cls.report_name, cls.stranger_name):
            frappe.db.sql("DELETE FROM `tabTask Entry` WHERE parent IN "
                           "(SELECT name FROM `tabDaily Work Log` WHERE employee=%s)", (emp,))
            frappe.db.sql("DELETE FROM `tabDaily Work Log` WHERE employee=%s", (emp,))
        frappe.db.sql("DELETE FROM `tabEmployee` WHERE name IN (%s,%s,%s)",
                      (cls.leader_name, cls.report_name, cls.stranger_name))
        frappe.db.sql("DELETE FROM `tabUser` WHERE email IN (%s,%s,%s)",
                      (cls.leader_user, cls.report_user, cls.stranger_user))
        frappe.db.commit()

    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.set_user("Administrator")
        for emp in (self.report_name, self.stranger_name):
            frappe.db.sql("DELETE FROM `tabTask Entry` WHERE parent IN "
                           "(SELECT name FROM `tabDaily Work Log` WHERE employee=%s)", (emp,))
            frappe.db.sql("DELETE FROM `tabDaily Work Log` WHERE employee=%s", (emp,))
        frappe.db.commit()

    def test_team_leader_assigns_to_own_report(self):
        frappe.set_user(self.leader_user)
        r = assign_task_to_employee(
            assignee_employee=self.report_name,
            description="Prepare the Q3 summary",
        )
        self.assertTrue(r["success"])

        work_log = _get_work_log(self.report_name, today())
        row = next(t for t in work_log.tasks if t.description == "Prepare the Q3 summary")
        self.assertEqual(row.task_type, "Ad-hoc")
        self.assertEqual(row.status, "Pending")
        self.assertEqual(row.assigned_by, self.leader_name)
        self.assertEqual(row.name, r["task_name"])
        self.assertEqual(row.series_id, r["series_id"])

    def test_assigned_by_name_shows_on_task_entry_dict(self):
        """This is what daily_checkin.html renders "Assigned by {name}" from."""
        frappe.set_user(self.leader_user)
        assign_task_to_employee(assignee_employee=self.report_name, description="Review the PR")

        work_log = _get_work_log(self.report_name, today())
        row = next(t for t in work_log.tasks if t.description == "Review the PR")
        entry = _task_entry_dict(row, today())
        self.assertEqual(entry["assigned_by_name"], "Test AssignTL")

    def test_self_planned_task_has_no_assigned_by(self):
        """A task the employee added themselves must not show "Assigned by
        anyone" — assigned_by/assigned_by_name stay blank."""
        frappe.set_user(self.report_user)
        submit_morning_log(
            new_tasks=json.dumps([{"description": "Self planned task"}]),
            login_time="09:00",
            work_location="Office",
        )
        work_log = _get_work_log(self.report_name, today())
        row = next(t for t in work_log.tasks if t.description == "Self planned task")
        entry = _task_entry_dict(row, today())
        self.assertFalse(entry["assigned_by_name"])

    def test_rejects_assignment_to_non_team_member(self):
        frappe.set_user(self.leader_user)
        with self.assertRaises(frappe.PermissionError):
            assign_task_to_employee(assignee_employee=self.stranger_name, description="Do this")

    def test_rejects_empty_description(self):
        frappe.set_user(self.leader_user)
        with self.assertRaises(frappe.ValidationError):
            assign_task_to_employee(assignee_employee=self.report_name, description="   ")

    def test_rejects_caller_with_no_employee_record(self):
        """A logged-in user with no Employee record at all (e.g. Management
        role) can't call this — _get_employee() throws first."""
        frappe.set_user("Guest")
        with self.assertRaises(frappe.PermissionError):
            assign_task_to_employee(assignee_employee=self.report_name, description="Task")

    def test_bot_assignment_also_stamps_assigned_by(self):
        """Regression: assign_task_via_agent now shares _assign_task with
        assign_task_to_employee, so a bot-assigned task must show
        "Assigned by" too, not just ones from the Team Dashboard."""
        role_name = "ST Task Assignment Agent"
        if not frappe.db.exists("Role", role_name):
            frappe.get_doc({"doctype": "Role", "role_name": role_name}).insert(ignore_permissions=True)
        agent_user = "assign_bot_agent@test.example.com"
        if not frappe.db.exists("User", agent_user):
            u = frappe.new_doc("User")
            u.email = agent_user
            u.first_name = "Bot"
            u.send_welcome_email = 0
            u.insert(ignore_permissions=True, ignore_if_duplicate=True)
        user_doc = frappe.get_doc("User", agent_user)
        if role_name not in [r.role for r in user_doc.roles]:
            user_doc.append("roles", {"role": role_name})
            user_doc.save(ignore_permissions=True)
        frappe.db.commit()

        try:
            frappe.set_user(agent_user)
            assign_task_via_agent(
                assignee_employee=self.report_name,
                description="Bot assigned task",
                assigned_by_employee=self.leader_name,
            )
            work_log = _get_work_log(self.report_name, today())
            row = next(t for t in work_log.tasks if t.description == "Bot assigned task")
            self.assertEqual(row.assigned_by, self.leader_name)
        finally:
            frappe.set_user("Administrator")
            frappe.db.sql("DELETE FROM `tabUser` WHERE email=%s", (agent_user,))
            frappe.db.commit()
