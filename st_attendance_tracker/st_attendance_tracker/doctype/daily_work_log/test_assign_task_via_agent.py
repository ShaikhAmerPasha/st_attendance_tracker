import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from st_attendance_tracker.api import assign_task_via_agent, _get_work_log
from st_attendance_tracker.st_attendance_tracker.doctype.daily_work_log.test_daily_work_log import _make_employee


class TestAssignTaskViaAgent(FrappeTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")
        
        company = frappe.db.get_single_value("Global Defaults", "default_company") or "_Test Company"
        cls.dept = (
            frappe.db.get_value("Department", {"department_name": "_QA Dept", "company": company}, "name")
            or f"_QA Dept - {company}"
        )

        cls.leader_user = "agent_tl@test.example.com"
        cls.report_user = "agent_emp@test.example.com"
        cls.agent_user = "agent_service@test.example.com"
        cls.stranger_user = "agent_stranger@test.example.com"

        cls.leader_name = _make_employee("AgentTL", cls.dept, cls.leader_user, ["Employee"])
        cls.report_name = _make_employee("AgentEmp", cls.dept, cls.report_user, ["Employee"])
        cls.stranger_name = _make_employee("AgentStranger", cls.dept, cls.stranger_user, ["Employee"])

        # Set up reporting structure
        frappe.db.set_value("Employee", cls.report_name, "reports_to", cls.leader_name)
        frappe.db.set_value("Employee", cls.report_name, "cell_number", "+919876543210")

        # Create Agent User
        if not frappe.db.exists("User", cls.agent_user):
            u = frappe.new_doc("User")
            u.email = cls.agent_user
            u.first_name = "Hermes"
            u.last_name = "Agent"
            u.send_welcome_email = 0
            u.insert(ignore_permissions=True, ignore_if_duplicate=True)

        # Create Role if not exists
        if not frappe.db.exists("Role", "ST Task Assignment Agent"):
            role = frappe.new_doc("Role")
            role.role_name = "ST Task Assignment Agent"
            role.insert(ignore_permissions=True)

        # Assign Role to Agent User
        user_doc = frappe.get_doc("User", cls.agent_user)
        if "ST Task Assignment Agent" not in [r.role for r in user_doc.roles]:
            user_doc.append("roles", {"role": "ST Task Assignment Agent"})
            user_doc.save(ignore_permissions=True)

        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.set_user("Administrator")
        frappe.db.sql("DELETE FROM `tabTask Entry` WHERE parent IN "
                       "(SELECT name FROM `tabDaily Work Log` WHERE employee=%s)", (cls.report_name,))
        frappe.db.sql("DELETE FROM `tabDaily Work Log` WHERE employee=%s", (cls.report_name,))
        
        frappe.db.sql("DELETE FROM `tabEmployee` WHERE name IN (%s,%s,%s)", 
                      (cls.leader_name, cls.report_name, cls.stranger_name))
        frappe.db.sql("DELETE FROM `tabUser` WHERE email IN (%s,%s,%s,%s)", 
                      (cls.leader_user, cls.report_user, cls.stranger_user, cls.agent_user))
        frappe.db.commit()

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.db.sql("DELETE FROM `tabTask Entry` WHERE parent IN "
                       "(SELECT name FROM `tabDaily Work Log` WHERE employee=%s)", (self.report_name,))
        frappe.db.sql("DELETE FROM `tabDaily Work Log` WHERE employee=%s", (self.report_name,))
        frappe.db.commit()

    def test_happy_path(self):
        """Happy path: leader assigns to a direct report, task appears in today's work log"""
        frappe.set_user(self.agent_user)
        
        res = assign_task_via_agent(
            assignee_employee=self.report_name, 
            description="Prepare deck (Fri, 5 Sep)", 
            assigned_by_employee=self.leader_name
        )
        
        self.assertEqual(res["employee"], self.report_name)
        self.assertEqual(res["date"], today())
        self.assertIn("employee_name", res)
        self.assertTrue(res["employee_name"])  # non-empty
        self.assertIn("cell_number", res)
        self.assertEqual(res["cell_number"], "+919876543210")
        
        work_log = _get_work_log(self.report_name, today())
        self.assertIsNotNone(work_log)
        
        tasks = [t for t in work_log.tasks if t.description == "Prepare deck (Fri, 5 Sep)"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].task_type, "Ad-hoc")
        self.assertEqual(tasks[0].status, "Pending")
        self.assertEqual(str(tasks[0].origin_date), today())
        self.assertIsNotNone(tasks[0].series_id)

    def test_rejection_not_direct_report(self):
        """Rejection: assignee does not report to assigned_by_employee"""
        frappe.set_user(self.agent_user)
        with self.assertRaises(frappe.PermissionError):
            assign_task_via_agent(
                assignee_employee=self.stranger_name, 
                description="Do this", 
                assigned_by_employee=self.leader_name
            )

    def test_rejection_lacks_role(self):
        """Rejection: caller lacks the new role"""
        # A normal user without the agent role
        frappe.set_user(self.leader_user)
        with self.assertRaises(frappe.PermissionError):
            assign_task_via_agent(
                assignee_employee=self.report_name, 
                description="Task", 
                assigned_by_employee=self.leader_name
            )

    def test_rejection_empty_description(self):
        """Rejection: empty description -> validation error"""
        frappe.set_user(self.agent_user)
        with self.assertRaises(frappe.ValidationError):
            assign_task_via_agent(
                assignee_employee=self.report_name, 
                description="   ", 
                assigned_by_employee=self.leader_name
            )

    def test_sequence_increment(self):
        """Sequence check: assigning two tasks same day increments sequence correctly"""
        frappe.set_user(self.agent_user)
        
        assign_task_via_agent(
            assignee_employee=self.report_name, 
            description="First Task", 
            assigned_by_employee=self.leader_name
        )
        assign_task_via_agent(
            assignee_employee=self.report_name, 
            description="Second Task", 
            assigned_by_employee=self.leader_name
        )
        
        work_log = _get_work_log(self.report_name, today())
        tasks = sorted(work_log.tasks, key=lambda t: t.sequence)
        
        self.assertEqual(tasks[0].description, "First Task")
        self.assertEqual(tasks[1].description, "Second Task")
        self.assertTrue(tasks[1].sequence > tasks[0].sequence)

    def test_assign_to_locked_day(self):
        """Test assigning a task to an employee who has already submitted their EOD for the day"""
        frappe.set_user("Administrator")
        
        # Create a locked Daily Work Log
        log = frappe.new_doc("Daily Work Log")
        log.employee = self.report_name
        log.date = today()
        log.login_time = "09:00:00"
        log.eod_submitted = 1
        log.insert(ignore_permissions=True)
        
        # Switch to Agent User
        frappe.set_user(self.agent_user)
        
        # Attempt assignment, should append without error
        res = assign_task_via_agent(
            assignee_employee=self.report_name,
            description="After hours task",
            assigned_by_employee=self.leader_name
        )
        
        self.assertEqual(res["employee"], self.report_name)
        
        # Verify
        work_log = _get_work_log(self.report_name, today())
        self.assertEqual(work_log.eod_submitted, 1)
        self.assertEqual(work_log.tasks[0].description, "After hours task")
