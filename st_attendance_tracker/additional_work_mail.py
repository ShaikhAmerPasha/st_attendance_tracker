"""Emails for Additional Work, sent the same way check-in and check-out are: HR Managers and the
Team Leader get one message, the employee gets a confirmation. Entries made together ("Several
tasks") go out as one email per employee and day."""
import time

import frappe
from frappe.utils import getdate
from frappe.utils import escape_html

from st_attendance_tracker.api import (
    EMAIL_HEADING_STYLE, EMAIL_SUBHEADING_STYLE, EMAIL_WRAPPER_STYLE,
    _employee_mail_identity, _format_email_date, _format_hours, _get_employee_email,
    _get_hr_manager_emails, _get_team_leader_emails, _render_detail_table, _to_ampm,
)

# Entries saved within this many seconds of the first one share its email.
BATCH_WINDOW_SECONDS = 8
_MARKER_TTL = 120


def queue_additional_work_email(doc, method=None):
    """Additional Work after_insert."""
    if frappe.flags.in_test or frappe.flags.in_import or frappe.flags.in_migrate:
        return
    day = str(getdate(doc.work_date))
    # Atomic SET NX: only the first entry of a batch queues the email.
    cache = frappe.cache()
    if not cache.set(cache.make_key(f"st_aw_mail:{doc.employee}:{day}"), 1, nx=True, ex=_MARKER_TTL):
        return
    frappe.enqueue(
        send_additional_work_emails,
        queue="short",
        enqueue_after_commit=True,
        employee_name=doc.employee,
        work_date=day,
    )


def send_additional_work_emails(employee_name, work_date, wait=BATCH_WINDOW_SECONDS):
    try:
        if wait:
            time.sleep(wait)  # let the rest of a multi-task submit land first
        employee = frappe.db.get_value(
            "Employee", employee_name, ["name", "employee_name", "department"], as_dict=True
        )
        entries = frappe.get_all(
            "Additional Work",
            filters={"employee": employee_name, "work_date": work_date},
            fields=["login_time", "logout_time", "project_name", "hours_spent", "status", "description", "remarks"],
            order_by="login_time asc, creation asc",
        )
        if not employee or not entries:
            return
        html = _render(employee, work_date, entries)
        label = getdate(work_date).strftime("%d-%m-%Y")
        subject = f"{employee.employee_name} - Additional Work - {label}"
        sender, reply_to = _employee_mail_identity(employee.name, employee.employee_name)

        recipients = list(dict.fromkeys(_get_hr_manager_emails() + _get_team_leader_emails(employee.name)))
        if recipients:
            frappe.sendmail(recipients=recipients, subject=subject, message=html, now=False, sender=sender, reply_to=reply_to)
        own = _get_employee_email(employee.name)
        if own:
            frappe.sendmail(recipients=[own], subject=subject, message=html, now=False, sender=sender, reply_to=reply_to)
    except Exception as e:
        frappe.log_error(f"Additional work email failed for {employee_name} ({work_date}): {e}", "ST Attendance Tracker")


def _render(employee, work_date, entries):
    total = sum(float(e.hours_spent or 0) for e in entries)
    detail = _render_detail_table([
        ("Employee", employee.employee_name),
        ("Date", _format_email_date(work_date)),
        ("Entries", str(len(entries))),
        ("Total Additional Hours", _format_hours(total) or "0h"),
    ])
    cell = "padding:8px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top;font-size:13px"
    head = "padding:8px 10px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280;border-bottom:1px solid #d1d5db"
    rows = "".join(
        f"<tr><td style=\"{cell}\">{escape_html(_window(e))}</td>"
        f"<td style=\"{cell}\"><b>{escape_html(e.description or '')}</b>"
        + (f"<div style=\"color:#6b7280\">{escape_html(e.remarks)}</div>" if e.remarks else "")
        + f"</td><td style=\"{cell}\">{escape_html(e.project_name or '-')}</td>"
        f"<td style=\"{cell}\">{escape_html(_format_hours(e.hours_spent) or '-')}</td>"
        f"<td style=\"{cell}\">{escape_html(e.status or '')}</td></tr>"
        for e in entries
    )
    table = (
        '<table style="width:100%;border-collapse:collapse"><thead><tr>'
        + "".join(f'<th style="{head}">{h}</th>' for h in ("Time", "Work", "Project", "Hours", "Status"))
        + f"</tr></thead><tbody>{rows}</tbody></table>"
    )
    return (
        f'<div style="{EMAIL_WRAPPER_STYLE}"><div style="{EMAIL_HEADING_STYLE}">Additional Work</div>{detail}'
        f'<div style="{EMAIL_SUBHEADING_STYLE}">What was done</div>{table}</div>'
    )


def _window(entry):
    start, end = _to_ampm(entry.login_time), _to_ampm(entry.logout_time)
    return f"{start} - {end}" if start and end else (start or end or "-")
