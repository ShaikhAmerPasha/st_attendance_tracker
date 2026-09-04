"""
Tests for the link_telegram_account whitelisted API method.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from st_attendance_tracker.api import link_telegram_account
from st_attendance_tracker.st_attendance_tracker.doctype.daily_work_log.test_daily_work_log import _make_employee


class TestLinkTelegramAccount(FrappeTestCase):

    TELEGRAM_ID = "tg_test_123456"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")

        company = frappe.db.get_single_value("Global Defaults", "default_company") or "_Test Company"
        cls.dept = (
            frappe.db.get_value("Department", {"department_name": "_QA Dept", "company": company}, "name")
            or f"_QA Dept - {company}"
        )

        # ── Target user whose credentials will be verified ─────────────
        cls.target_user = "tg_target@test.example.com"
        cls.target_password = "TestPassword@123"
        cls.target_emp = _make_employee("TgTarget", cls.dept, cls.target_user, ["Employee"])

        # Set a known password on the target user so check_password can verify it
        target_user_doc = frappe.get_doc("User", cls.target_user)
        target_user_doc.new_password = cls.target_password
        target_user_doc.save(ignore_permissions=True)

        # ── Service account that calls the API ─────────────────────────
        cls.service_user = "tg_hermes@test.example.com"
        if not frappe.db.exists("User", cls.service_user):
            u = frappe.new_doc("User")
            u.email = cls.service_user
            u.first_name = "Hermes"
            u.last_name = "Telegram"
            u.send_welcome_email = 0
            u.insert(ignore_permissions=True, ignore_if_duplicate=True)

        # Create role if not exists
        if not frappe.db.exists("Role", "Telegram Credential Lookup"):
            role = frappe.new_doc("Role")
            role.role_name = "Telegram Credential Lookup"
            role.insert(ignore_permissions=True)

        # Assign role to service user
        service_doc = frappe.get_doc("User", cls.service_user)
        if "Telegram Credential Lookup" not in [r.role for r in service_doc.roles]:
            service_doc.append("roles", {"role": "Telegram Credential Lookup"})
            service_doc.save(ignore_permissions=True)

        # ── Unprivileged user (no role) ────────────────────────────────
        cls.no_role_user = "tg_norole@test.example.com"
        if not frappe.db.exists("User", cls.no_role_user):
            u = frappe.new_doc("User")
            u.email = cls.no_role_user
            u.first_name = "No"
            u.last_name = "Role"
            u.send_welcome_email = 0
            u.insert(ignore_permissions=True, ignore_if_duplicate=True)

        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.set_user("Administrator")
        # Clean up Telegram User Mappings
        for tg_id in [cls.TELEGRAM_ID]:
            if frappe.db.exists("Telegram User Mapping", {"telegram_id": tg_id}):
                frappe.db.delete("Telegram User Mapping", {"telegram_id": tg_id})

        # Clean up test users, employees
        frappe.db.sql("DELETE FROM `tabEmployee` WHERE name=%s", (cls.target_emp,))
        for email in [cls.target_user, cls.service_user, cls.no_role_user]:
            frappe.db.sql("DELETE FROM `tabUser` WHERE email=%s", (email,))
        frappe.db.commit()

    def setUp(self):
        frappe.set_user("Administrator")
        # Clean any mapping from previous test
        if frappe.db.exists("Telegram User Mapping", {"telegram_id": self.TELEGRAM_ID}):
            frappe.db.delete("Telegram User Mapping", {"telegram_id": self.TELEGRAM_ID})
        frappe.db.commit()

    # ── Happy path ─────────────────────────────────────────────────────────────

    def test_valid_credentials_link_successfully(self):
        """Valid credentials create a Telegram User Mapping and return linked=True."""
        frappe.set_user(self.service_user)

        result = link_telegram_account(
            telegram_id=self.TELEGRAM_ID,
            user_email=self.target_user,
            password=self.target_password,
        )

        self.assertTrue(result["linked"])
        self.assertEqual(result["user"], self.target_user)
        self.assertEqual(result["employee"], self.target_emp)

        # Verify mapping record exists
        self.assertTrue(
            frappe.db.exists("Telegram User Mapping", {"telegram_id": self.TELEGRAM_ID}),
            "Telegram User Mapping was not created",
        )
        mapping = frappe.get_doc("Telegram User Mapping", self.TELEGRAM_ID)
        self.assertEqual(mapping.user, self.target_user)
        self.assertTrue(mapping.api_key)
        self.assertEqual(mapping.enabled, 1)

        # Verify the User doc got an API key
        user_doc = frappe.get_doc("User", self.target_user)
        self.assertTrue(user_doc.api_key)

    # ── Wrong password ─────────────────────────────────────────────────────────

    def test_wrong_password_rejected_no_mapping(self):
        """Wrong password raises AuthenticationError and no mapping is created."""
        frappe.set_user(self.service_user)

        with self.assertRaises(frappe.AuthenticationError):
            link_telegram_account(
                telegram_id=self.TELEGRAM_ID,
                user_email=self.target_user,
                password="WrongPassword!999",
            )

        # Verify no mapping was created
        self.assertFalse(
            frappe.db.exists("Telegram User Mapping", {"telegram_id": self.TELEGRAM_ID}),
            "Mapping should NOT be created when password is wrong",
        )

    # ── Missing role ───────────────────────────────────────────────────────────

    def test_caller_without_role_rejected(self):
        """A user without the Telegram Credential Lookup role is denied."""
        frappe.set_user(self.no_role_user)

        with self.assertRaises(frappe.PermissionError):
            link_telegram_account(
                telegram_id=self.TELEGRAM_ID,
                user_email=self.target_user,
                password=self.target_password,
            )

    # ── Upsert (re-link) ──────────────────────────────────────────────────────

    def test_upsert_reuses_existing_mapping(self):
        """Calling link_telegram_account twice with the same telegram_id
        updates the existing record instead of creating a duplicate."""
        frappe.set_user(self.service_user)

        link_telegram_account(
            telegram_id=self.TELEGRAM_ID,
            user_email=self.target_user,
            password=self.target_password,
        )
        first_key = frappe.get_doc("Telegram User Mapping", self.TELEGRAM_ID).api_key

        # Link again — should upsert, not duplicate
        link_telegram_account(
            telegram_id=self.TELEGRAM_ID,
            user_email=self.target_user,
            password=self.target_password,
        )

        count = frappe.db.count("Telegram User Mapping", {"telegram_id": self.TELEGRAM_ID})
        self.assertEqual(count, 1, "Duplicate mapping created on re-link")

        # API key should be preserved (only secret rotates)
        updated_key = frappe.get_doc("Telegram User Mapping", self.TELEGRAM_ID).api_key
        self.assertEqual(first_key, updated_key)
