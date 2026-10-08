"""Additional Work emails: queued once per employee and day, HR/Team Leader and the employee both get one."""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from st_attendance_tracker import additional_work_mail as mail

EMP = "EMP-AW-MAIL-TEST"
DAY = "2026-10-06"


def _doc():
    return frappe._dict(employee=EMP, work_date=DAY)


def _key():
    return frappe.cache().make_key(f"st_aw_mail:{EMP}:{DAY}")


class TestAdditionalWorkMail(FrappeTestCase):
    def setUp(self):
        self._flags = (frappe.flags.in_test, frappe.flags.in_import, frappe.flags.in_migrate)
        frappe.flags.in_test = frappe.flags.in_import = frappe.flags.in_migrate = False
        frappe.cache().delete(_key())

    def tearDown(self):
        frappe.flags.in_test, frappe.flags.in_import, frappe.flags.in_migrate = self._flags
        frappe.cache().delete(_key())

    def test_first_entry_queues_one_email_and_the_batch_shares_it(self):
        with patch.object(frappe, "enqueue") as enqueue:
            mail.queue_additional_work_email(_doc())
            mail.queue_additional_work_email(_doc())
            mail.queue_additional_work_email(_doc())
        self.assertEqual(enqueue.call_count, 1)
        self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

    def test_inert_under_tests(self):
        frappe.flags.in_test = True
        with patch.object(frappe, "enqueue") as enqueue:
            mail.queue_additional_work_email(_doc())
        self.assertEqual(enqueue.call_count, 0)

    def _send(self, entries, employee=True, hr=("hr@test.example.com",), tl=(), own="emp@test.example.com"):
        emp = frappe._dict(name=EMP, employee_name="Mail Tester", department="D") if employee else None
        rows = [frappe._dict(e) for e in entries]
        with patch.object(frappe.db, "get_value", return_value=emp), \
                patch.object(frappe, "get_all", return_value=rows), \
                patch.object(mail, "_get_hr_manager_emails", return_value=list(hr)), \
                patch.object(mail, "_get_team_leader_emails", return_value=list(tl)), \
                patch.object(mail, "_get_employee_email", return_value=own), \
                patch.object(mail, "_employee_mail_identity", return_value=(None, None)), \
                patch.object(frappe, "sendmail") as sendmail:
            mail.send_additional_work_emails(EMP, DAY, wait=0)
        return sendmail

    ENTRY = dict(login_time="19:00:00", logout_time="21:00:00", project_name="P", hours_spent=2.0,
                 status="Done", description="Fixed <b>bug</b>", remarks="")

    def test_leaders_and_employee_each_get_one_email(self):
        sendmail = self._send([self.ENTRY, {**self.ENTRY, "hours_spent": 1.5}], tl=("tl@test.example.com",))
        self.assertEqual(sendmail.call_count, 2)
        first, second = sendmail.call_args_list
        self.assertEqual(set(first.kwargs["recipients"]), {"hr@test.example.com", "tl@test.example.com"})
        self.assertEqual(second.kwargs["recipients"], ["emp@test.example.com"])
        self.assertIn("Additional Work", first.kwargs["subject"])
        self.assertIn("3h 30m", first.kwargs["message"])
        self.assertNotIn("<b>bug</b>", first.kwargs["message"])  # user text is escaped

    def test_no_entries_or_unknown_employee_sends_nothing(self):
        self.assertEqual(self._send([]).call_count, 0)
        self.assertEqual(self._send([self.ENTRY], employee=False).call_count, 0)

    def test_missing_employee_email_still_notifies_leaders(self):
        sendmail = self._send([self.ENTRY], own=None)
        self.assertEqual(sendmail.call_count, 1)
