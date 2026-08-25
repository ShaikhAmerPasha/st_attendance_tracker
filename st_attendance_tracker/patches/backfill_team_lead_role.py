import frappe


def execute():
    if not frappe.db.table_exists("Employee"):
        return

    managers = frappe.get_all("Employee", filters={
        "reports_to": ["is", "set"],
        "status": "Active",
    }, pluck="reports_to")

    from st_attendance_tracker.setup import _sync_team_lead_role_for

    for manager_name in set(managers):
        _sync_team_lead_role_for(manager_name)

    # Also strip the role from anyone who holds it but has no active reports —
    # covers stale manual grants that predate this sync (see setup.py).
    stale_holders = frappe.get_all("Has Role", filters={"role": "Team Lead", "parenttype": "User"}, pluck="parent")
    for user_id in stale_holders:
        employee_name = frappe.db.get_value("Employee", {"user_id": user_id}, "name")
        if employee_name:
            _sync_team_lead_role_for(employee_name)

    frappe.db.commit()
