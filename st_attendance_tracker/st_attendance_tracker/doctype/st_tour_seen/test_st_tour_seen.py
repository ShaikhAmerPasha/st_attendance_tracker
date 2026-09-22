"""Product tour seen-tracking — mark seen, idempotency, unknown tour ids."""
import frappe
from frappe.tests.utils import FrappeTestCase

from st_attendance_tracker.tours import has_seen_tour, mark_tour_seen


def _make_user(email):
    if not frappe.db.exists("User", email):
        u = frappe.new_doc("User")
        u.email = email
        u.first_name = "QA"
        u.last_name = "Tour"
        u.send_welcome_email = 0
        u.insert(ignore_permissions=True, ignore_if_duplicate=True)
    return email


class TestSTTourSeen(FrappeTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")
        cls.user = _make_user("qa_tour_emp@test.example.com")
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.set_user("Administrator")
        frappe.db.delete("ST Tour Seen", {"user": cls.user})
        frappe.db.sql("DELETE FROM `tabUser` WHERE email=%s", (cls.user,))
        frappe.db.commit()

    def setUp(self):
        frappe.set_user(self.user)
        frappe.db.delete("ST Tour Seen", {"user": self.user})
        frappe.db.commit()

    def test_unseen_tour_reports_not_seen(self):
        result = has_seen_tour(tour_id="task_backlog_v1")
        self.assertFalse(result["seen"])

    def test_mark_seen_then_reports_seen(self):
        mark_tour_seen(tour_id="task_backlog_v1")
        result = has_seen_tour(tour_id="task_backlog_v1")
        self.assertTrue(result["seen"])

    def test_mark_seen_is_idempotent_update_not_duplicate(self):
        mark_tour_seen(tour_id="task_backlog_v1")
        mark_tour_seen(tour_id="task_backlog_v1")
        self.assertEqual(
            frappe.db.count("ST Tour Seen", {"user": self.user}), 1
        )

    def test_unknown_tour_id_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            has_seen_tour(tour_id="not_a_real_tour")
        with self.assertRaises(frappe.ValidationError):
            mark_tour_seen(tour_id="not_a_real_tour")
