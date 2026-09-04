"""
Tests for the get_credentials_by_telegram_id whitelisted API method.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from st_attendance_tracker.api import get_credentials_by_telegram_id
from st_attendance_tracker.st_attendance_tracker.doctype.daily_work_log.test_daily_work_log import _make_employee


class TestGetCredentialsByTelegramId(FrappeTestCase):

    TELEGRAM_ID = "tg_cred_test_999"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")

        company = frappe.db.get_single_value("Global Defaults", "default_company") or "_Test Company"
        cls.dept = (
            frappe.db.get_value("Department", {"department_name": "_QA Dept", "company": company}, "name")
            or f"_QA Dept - {company}"
        )

        # ── Employee with telegram_id set ──────────────────────────────
        cls.target_user = "tgcred_emp@test.example.com"
        cls.target_emp = _make_employee("TgCred", cls.dept, cls.target_user, ["Employee"])

        # Set telegram_id on the employee
        frappe.db.set_value("Employee", cls.target_emp, "telegram_id", cls.TELEGRAM_ID)

        # Clear any pre-existing api_key/api_secret so we test fresh generation
        frappe.db.set_value("User", cls.target_user, "api_key", "")
        frappe.db.set_value("User", cls.target_user, "api_secret", "")

        # ── Service account that calls the API ─────────────────────────
        cls.service_user = "tgcred_hermes@test.example.com"
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
        cls.no_role_user = "tgcred_norole@test.example.com"
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
        frappe.db.sql("DELETE FROM `tabEmployee` WHERE name=%s", (cls.target_emp,))
        for email in [cls.target_user, cls.service_user, cls.no_role_user]:
            frappe.db.sql("DELETE FROM `tabUser` WHERE email=%s", (email,))
        frappe.db.commit()

    def setUp(self):
        frappe.set_user("Administrator")

    # ── (a) Happy path ─────────────────────────────────────────────────────────

    def test_valid_telegram_id_returns_credentials(self):
        """Valid telegram_id with linked user returns api_key, api_secret,
        employee, employee_name, and user."""
        frappe.set_user(self.service_user)

        result = get_credentials_by_telegram_id(telegram_id=self.TELEGRAM_ID)

        self.assertEqual(result["user"], self.target_user)
        self.assertEqual(result["employee"], self.target_emp)
        self.assertTrue(result["employee_name"])
        self.assertTrue(result["api_key"], "api_key should be non-empty")
        self.assertTrue(result["api_secret"], "api_secret should be non-empty")

        # Verify the User doc was updated with the api_key
        user_doc = frappe.get_doc("User", self.target_user)
        self.assertEqual(user_doc.api_key, result["api_key"])

    def test_second_call_reuses_existing_keys(self):
        """Calling twice returns the same api_key and api_secret
        (keys are not regenerated on every call)."""
        frappe.set_user(self.service_user)

        first = get_credentials_by_telegram_id(telegram_id=self.TELEGRAM_ID)
        second = get_credentials_by_telegram_id(telegram_id=self.TELEGRAM_ID)

        self.assertEqual(first["api_key"], second["api_key"])
        self.assertEqual(first["api_secret"], second["api_secret"])

    # ── (b) Unknown telegram_id ────────────────────────────────────────────────

    def test_unknown_telegram_id_throws(self):
        """Unknown telegram_id raises DoesNotExistError."""
        frappe.set_user(self.service_user)

        with self.assertRaises(frappe.DoesNotExistError):
            get_credentials_by_telegram_id(telegram_id="nonexistent_tg_id_12345")

    # ── (c) Missing role ──────────────────────────────────────────────────────

    def test_caller_without_role_rejected(self):
        """A user without the Telegram Credential Lookup role gets PermissionError."""
        frappe.set_user(self.no_role_user)

        with self.assertRaises(frappe.PermissionError):
            get_credentials_by_telegram_id(telegram_id=self.TELEGRAM_ID)
