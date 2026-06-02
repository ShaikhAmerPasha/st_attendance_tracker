import frappe
from frappe.utils import today, now_datetime


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/daily-checkin"
        raise frappe.Redirect

    employee = frappe.db.get_value(
        "Employee",
        {"user_id": frappe.session.user},
        ["name", "employee_name", "department"],
        as_dict=True,
    )
    if not employee:
        frappe.throw(
            "Your user account is not linked to an Employee record. "
            "Please contact HR or System Administrator."
        )

    date = today()

    morning_log = frappe.db.exists("Daily Task Log", {
        "employee": employee.name,
        "date": date,
        "log_type": "Morning Check-In",
        "docstatus": 1,
    })

    eod_log = frappe.db.exists("Daily Task Log", {
        "employee": employee.name,
        "date": date,
        "log_type": "End of Day",
        "docstatus": 1,
    })

    # Safety net — run rollover if not yet checked in today
    if not morning_log:
        from st_attendance_tracker.api import _safety_rollover
        _safety_rollover(employee.name, date)

    # Load tasks for both morning (carried) and EOD (update status) views
    tasks = frappe.get_all(
        "Daily Task",
        filters={"employee": employee.name, "task_date": date},
        fields=[
            "name", "description", "status", "task_type",
            "origin_date", "rolled_over_from", "remarks",
        ],
        order_by="task_type asc, creation asc",
    )

    for task in tasks:
        task["is_carried"] = bool(task.get("rolled_over_from"))

    context.no_cache = 1
    context.employee = employee
    context.date = date
    context.current_time = now_datetime().strftime("%H:%M")
    context.is_checked_in = bool(morning_log)
    context.eod_done = bool(eod_log)
    context.tasks = tasks
    context.carried_count = sum(1 for t in tasks if t.get("is_carried"))
    context.title = "Daily Check-In"