"""
ST Attendance Tracker — Setup
Creates custom fields on standard ERPNext doctypes.
Called automatically after every migrate via hooks.py after_migrate.
"""

import frappe


def create_custom_fields():
    """
    Create all custom fields needed by ST Attendance Tracker.
    Safe to run multiple times — checks existence before creating.
    """
    _create_employee_work_type()
    _create_attendance_request_button()
    _create_employee_department_assignments()
    _create_department_task_summary_format()
    _create_employee_telegram_id()
    frappe.db.commit()


def _create_employee_work_type():
    """Add work_type select field to Employee doctype."""
    if frappe.db.exists("Custom Field", "Employee-work_type"):
        return

    custom_field = frappe.new_doc("Custom Field")
    custom_field.dt = "Employee"
    custom_field.label = "Work Type"
    custom_field.fieldname = "work_type"
    custom_field.fieldtype = "Select"
    custom_field.options = "\nOffice\nHybrid\nRemote"
    custom_field.default = "Office"
    custom_field.insert_after = "employment_type"
    custom_field.in_list_view = 0
    custom_field.description = (
        "Office: works from office only. "
        "Hybrid: office on configured days, WFH others. "
        "Remote: always remote."
    )
    custom_field.insert(ignore_permissions=True)
    frappe.msgprint("Custom field 'work_type' added to Employee doctype.", alert=True)


def _create_employee_department_assignments():
    """
    Add department_assignments child table to Employee doctype.
    Lists every department an employee works in and that department's
    Team Leader — lets employees working across multiple departments
    (e.g. Web + Digital Marketing) notify every Team Leader on
    check-in/check-out, not just the single reports_to manager.
    """
    if frappe.db.exists("Custom Field", "Employee-department_assignments"):
        return

    custom_field = frappe.new_doc("Custom Field")
    custom_field.dt = "Employee"
    custom_field.label = "Department Assignments"
    custom_field.fieldname = "department_assignments"
    custom_field.fieldtype = "Table"
    custom_field.options = "Employee Department Assignment"
    custom_field.insert_after = "department"
    custom_field.description = (
        "Departments this employee works in and each department's Team "
        "Leader. Used to notify every relevant Team Leader on check-in/"
        "check-out for employees working across multiple departments."
    )
    custom_field.insert(ignore_permissions=True)
    frappe.msgprint(
        "Custom field 'department_assignments' added to Employee doctype.",
        alert=True
    )


def _create_department_task_summary_format():
    """
    Add task_summary_email_format select field to Department doctype.
    Controls how the task list renders in check-in/checkout emails for
    employees in that department — everything else in the email (heading,
    detail card) stays identical regardless of this setting.
    """
    if frappe.db.exists("Custom Field", "Department-task_summary_email_format"):
        return

    custom_field = frappe.new_doc("Custom Field")
    custom_field.dt = "Department"
    custom_field.label = "Task Summary Email Format"
    custom_field.fieldname = "task_summary_email_format"
    custom_field.fieldtype = "Select"
    custom_field.options = "\nTabular\nGrouped List"
    custom_field.default = "Tabular"
    custom_field.insert_after = "disabled"
    custom_field.description = (
        "How the task list renders in check-in/checkout emails for this "
        "department's employees. Tabular: today's table layout. Grouped "
        "List: project heading followed by a bulleted task list."
    )
    custom_field.insert(ignore_permissions=True)
    frappe.msgprint(
        "Custom field 'task_summary_email_format' added to Department doctype.",
        alert=True
    )


def _create_employee_telegram_id():
    """Add telegram_id Data field to Employee doctype."""
    if frappe.db.exists("Custom Field", "Employee-telegram_id"):
        return

    custom_field = frappe.new_doc("Custom Field")
    custom_field.dt = "Employee"
    custom_field.label = "Telegram ID"
    custom_field.fieldname = "telegram_id"
    custom_field.fieldtype = "Data"
    custom_field.unique = 1
    custom_field.insert_after = "cell_phone"
    custom_field.in_list_view = 0
    custom_field.description = (
        "Telegram numeric user ID. Set by an admin to enable the "
        "Hermes Telegram bot to look up this employee's ERPNext "
        "API credentials without requiring a password."
    )
    custom_field.insert(ignore_permissions=True)
    frappe.msgprint("Custom field 'telegram_id' added to Employee doctype.", alert=True)


def _create_attendance_request_button():
    """
    Add a 'Go to Check-In Page' HTML field to Attendance Request.
    Shows a button when reason is Work From Home.
    """
    if frappe.db.exists("Custom Field", "Attendance Request-go_to_checkin_btn"):
        return

    custom_field = frappe.new_doc("Custom Field")
    custom_field.dt = "Attendance Request"
    custom_field.label = "Go to Check-In"
    custom_field.fieldname = "go_to_checkin_btn"
    custom_field.fieldtype = "HTML"
    custom_field.options = """
        <div id="st-checkin-btn-wrap" style="margin:8px 0">
            <a href="/daily-checkin"
               style="display:inline-flex;align-items:center;gap:6px;
                      padding:7px 16px;background:#EE1C29;color:#fff;
                      border-radius:6px;font-size:13px;font-weight:500;
                      text-decoration:none;font-family:'Inter',sans-serif">
                <i class="ti ti-login"></i> Go to Check-In Page
            </a>
        </div>
    """
    custom_field.insert_after = "reason"
    custom_field.insert(ignore_permissions=True)
    frappe.msgprint(
        "Custom button 'Go to Check-In Page' added to Attendance Request.",
        alert=True
    )


def sync_team_lead_role(doc, method=None):
    """
    Employee "on_update"/"on_trash" doc_event (see hooks.py). Keeps the
    "Team Lead" role assigned exactly to employees who currently have at
    least one active direct report (reports_to) — used to gate role-based
    content (e.g. Wiki spaces) by the same "is a team leader" definition
    the rest of the app already uses for reports_to.

    This role is fully derived, not manually maintained: any manual grant
    on someone without active reports gets removed on their next save.
    (A prior attempt at a role-based Team Leader check was reverted — see
    validate_department_assignments — because manually-tagged roles didn't
    match reality. Deriving it here instead of trusting manual tagging is
    the fix for that; backfill_team_lead_role patch does the same for
    already-existing data.)
    """
    managers_to_check = set()
    if doc.reports_to:
        managers_to_check.add(doc.reports_to)
    if method != "on_trash":
        before = doc.get_doc_before_save()
        if before and before.reports_to and before.reports_to != doc.reports_to:
            managers_to_check.add(before.reports_to)

    for manager_name in managers_to_check:
        _sync_team_lead_role_for(manager_name)


def _sync_team_lead_role_for(employee_name):
    """Add/remove the Team Lead role on one employee's user to match whether they currently have any active direct reports."""
    user_id = frappe.db.get_value("Employee", employee_name, "user_id")
    if not user_id:
        return

    has_reports = frappe.db.exists("Employee", {"reports_to": employee_name, "status": "Active"})
    has_role = "Team Lead" in frappe.get_roles(user_id)

    if has_reports and not has_role:
        frappe.get_doc("User", user_id).add_roles("Team Lead")
    elif not has_reports and has_role:
        frappe.get_doc("User", user_id).remove_roles("Team Lead")


def validate_department_assignments(doc, method=None):
    """
    Employee "validate" doc_event (see hooks.py). Child table rows
    (Employee Department Assignment) don't get their own validate() called
    by Frappe when the parent saves, so this check has to live here.

    team_leader ignores User Permissions (so the parent Employee record
    stays viewable regardless of who their leader is) — that removes the
    one implicit check that would otherwise apply. A role-based check
    ("must hold Team Lead/HR Manager") was tried and reverted — this
    org's real team leaders (e.g. reports_to chains already in use) are
    not tagged with those Frappe roles, so it broke legitimate existing
    data. Enforcing instead: must be a real, active employee, and can't
    name yourself as your own team leader — catches garbage/self-referential
    values without requiring role tagging this org doesn't actually use.
    """
    for row in doc.get("department_assignments") or []:
        if not row.team_leader:
            continue
        if row.team_leader == doc.name:
            frappe.throw("An employee cannot be set as their own Team Leader.")
        is_active = frappe.db.get_value("Employee", row.team_leader, "status")
        if is_active != "Active":
            frappe.throw(f"{row.team_leader} is not an active employee and cannot be a Team Leader.")
