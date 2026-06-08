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
