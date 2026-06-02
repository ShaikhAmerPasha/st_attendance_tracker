"""
Whitelisted API methods consumed by /daily-checkin web page.
All methods resolve employee from frappe.session.user.

Fixes included:
  - lunch_from / lunch_to (time range) instead of single lunch_time
  - _get_root_task() to prevent duplicate rollover across multiple days
  - _rollover_pending_tasks() always points to ROOT task
  - _safety_rollover() on morning page load for missed EOD submissions
  - Checkin auto-heal if HR deletes the checkin record
  - Manual login_time entry support
"""

import frappe
import datetime
import json
from frappe.utils import today, now_datetime, getdate, add_days


# ── Employee helper ────────────────────────────────────────────────────────────

def _get_employee():
    employee = frappe.db.get_value(
        "Employee",
        {"user_id": frappe.session.user},
        ["name", "employee_name", "department"],
        as_dict=True,
    )
    if not employee:
        frappe.throw(
            "No Employee record is linked to your user account. "
            "Please contact HR.",
            frappe.PermissionError,
        )
    return employee


# ── Checkin helper ─────────────────────────────────────────────────────────────

def _make_checkin(employee, log_type, time_value=None):
    """Create an ERPNext Employee Checkin record."""
    try:
        checkin = frappe.new_doc("Employee Checkin")
        checkin.employee = employee
        checkin.log_type = log_type

        if time_value:
            if isinstance(time_value, str):
                parts = (time_value + ":00").split(":")[:3]
                t = datetime.time(int(parts[0]), int(parts[1]), int(parts[2]))
            else:
                t = time_value
            checkin.time = datetime.datetime.combine(datetime.date.today(), t)
        else:
            checkin.time = now_datetime()

        checkin.device_id = "ST Daily Checkin"
        checkin.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(
            f"Checkin failed for {employee} ({log_type}): {e}",
            "ST Attendance Tracker"
        )


# ── Next working date helper ───────────────────────────────────────────────────

def _get_next_working_date(employee, from_date):
    """
    Returns the next date that is not Sunday and not a declared holiday
    on the employee's holiday list.
    """
    holiday_list_name = frappe.db.get_value("Employee", employee, "holiday_list")
    holidays = set()
    if holiday_list_name:
        rows = frappe.get_all(
            "Holiday",
            filters={"parent": holiday_list_name},
            pluck="holiday_date"
        )
        holidays = {getdate(h) for h in rows}

    candidate = getdate(add_days(from_date, 1))
    while True:
        if candidate.weekday() == 6:  # Sunday
            candidate = getdate(add_days(candidate, 1))
            continue
        if candidate in holidays:
            candidate = getdate(add_days(candidate, 1))
            continue
        break

    return candidate


# ── Root task tracer ───────────────────────────────────────────────────────────

def _get_root_task(task_name):
    """
    Walk up the rolled_over_from chain to find the original task.
    Prevents a task pending for N days from being duplicated N times.
    Depth limit of 365 prevents infinite loops.
    """
    current = task_name
    depth = 0
    while depth < 365:
        parent = frappe.db.get_value("Daily Task", current, "rolled_over_from")
        if not parent:
            return current  # This IS the root
        current = parent
        depth += 1
    return current  # Fallback after depth limit


# ── Rollover on EOD submit ─────────────────────────────────────────────────────

def _rollover_pending_tasks(employee, date):
    """
    Called on EOD submission.
    Rolls Pending / In-Progress tasks to next working day.
    Always points rolled_over_from to ROOT task — prevents duplicates
    when same task stays pending across multiple days.
    """
    next_date = _get_next_working_date(employee, date)

    pending_tasks = frappe.get_all(
        "Daily Task",
        filters={
            "employee": employee,
            "task_date": date,
            "status": ["in", ["Pending", "In Progress"]],
        },
        fields=[
            "name", "description", "task_type",
            "origin_date", "remarks", "rolled_over_from"
        ],
    )

    rolled = 0
    for task in pending_tasks:
        # Always trace back to root to use as the unique key
        root_task = _get_root_task(task.name)

        # Skip if next_date already has a task from this root
        already = frappe.db.exists("Daily Task", {
            "employee": employee,
            "task_date": next_date,
            "rolled_over_from": root_task,
        })

        # Skip if root itself is already on next_date
        root_date = frappe.db.get_value("Daily Task", root_task, "task_date")

        if already or str(root_date) == str(next_date):
            continue

        new_task = frappe.new_doc("Daily Task")
        new_task.employee = employee
        new_task.task_date = next_date
        new_task.description = task.description
        new_task.task_type = "Planned"
        new_task.status = "Pending"
        new_task.origin_date = task.origin_date or date
        new_task.rolled_over_from = root_task
        new_task.remarks = f"[Carried forward from {task.origin_date or date}]"
        new_task.insert(ignore_permissions=True)
        rolled += 1

    if rolled:
        frappe.db.commit()

    return rolled


# ── Safety rollover on page load ───────────────────────────────────────────────

def _safety_rollover(employee, today_date):
    """
    Runs silently on morning page load (before check-in).
    Catches any pending tasks from previous days where employee
    forgot to submit EOD — pulls them into today automatically.
    Uses root task as unique key to prevent duplicates.
    """
    missed = frappe.get_all(
        "Daily Task",
        filters={
            "employee": employee,
            "task_date": ["<", today_date],
            "status": ["in", ["Pending", "In Progress"]],
        },
        fields=[
            "name", "description", "task_type",
            "origin_date", "remarks", "task_date"
        ],
    )

    for task in missed:
        root_task = _get_root_task(task.name)

        # Skip if today already has a task from this root
        already = frappe.db.exists("Daily Task", {
            "employee": employee,
            "task_date": today_date,
            "rolled_over_from": root_task,
        })

        # Skip if root itself is today's task
        root_date = frappe.db.get_value("Daily Task", root_task, "task_date")

        if already or str(root_date) == str(today_date):
            continue

        new_task = frappe.new_doc("Daily Task")
        new_task.employee = employee
        new_task.task_date = today_date
        new_task.description = task.description
        new_task.task_type = "Planned"
        new_task.status = "Pending"
        new_task.origin_date = task.origin_date or task.task_date
        new_task.rolled_over_from = root_task
        new_task.remarks = f"[Auto-carried from {task.origin_date or task.task_date}]"
        new_task.insert(ignore_permissions=True)

    frappe.db.commit()


# ── Page state API ─────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_page_state():
    """
    Returns everything the /daily-checkin page needs on load.
    Runs safety rollover before check-in to catch missed pending tasks.
    Auto-heals Employee Checkin if HR deleted it.
    """
    employee = _get_employee()
    date = today()

    morning_log = frappe.db.exists("Daily Task Log", {
        "employee": employee.name,
        "date": date,
        "log_type": "Morning Check-In",
        "docstatus": 1,
    })

    # Safety rollover — only runs if employee hasn't checked in yet today
    if not morning_log:
        _safety_rollover(employee.name, date)

    # Auto-heal: if morning log exists but checkin was deleted by HR — recreate it
    if morning_log:
        checkin_today = frappe.db.exists("Employee Checkin", {
            "employee": employee.name,
            "log_type": "IN",
            "device_id": "ST Daily Checkin",
            "time": ["between", [
                frappe.utils.get_datetime(date + " 00:00:00"),
                frappe.utils.get_datetime(date + " 23:59:59"),
            ]],
        })
        if not checkin_today:
            login_time = frappe.db.get_value(
                "Daily Task Log", morning_log, "login_time"
            )
            _make_checkin(employee.name, "IN", login_time)

    eod_log = frappe.db.exists("Daily Task Log", {
        "employee": employee.name,
        "date": date,
        "log_type": "End of Day",
        "docstatus": 1,
    })

    tasks = []
    if morning_log:
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

    return {
        "employee": employee,
        "date": date,
        "morning_done": bool(morning_log),
        "eod_done": bool(eod_log),
        "tasks": tasks,
        "current_time": now_datetime().strftime("%H:%M"),
    }


# ── Morning submit ─────────────────────────────────────────────────────────────

@frappe.whitelist()
def submit_morning_log(new_tasks, login_time=None):
    """
    Called when employee clicks 'Check In & Start Day'.
    Accepts new task list (JSON) and optional manual login_time.
    Creates Daily Task docs, Daily Task Log, Employee Checkin (IN).
    """
    employee = _get_employee()
    date = today()

    # Guard: prevent double submit
    if frappe.db.exists("Daily Task Log", {
        "employee": employee.name,
        "date": date,
        "log_type": "Morning Check-In",
        "docstatus": 1,
    }):
        frappe.throw("You have already checked in today.")

    tasks = json.loads(new_tasks) if isinstance(new_tasks, str) else new_tasks

    if not tasks:
        frappe.throw("Please add at least one planned task before checking in.")

    # Insert new planned tasks
    for t in tasks:
        desc = (t.get("description") or "").strip()
        if not desc:
            continue
        task_doc = frappe.new_doc("Daily Task")
        task_doc.employee = employee.name
        task_doc.task_date = date
        task_doc.description = desc
        task_doc.task_type = "Planned"
        task_doc.status = "Pending"
        task_doc.origin_date = date
        task_doc.insert(ignore_permissions=True)

    # Use provided login time or capture now
    actual_login_time = login_time or now_datetime().strftime("%H:%M:%S")

    # Create and submit Morning Log
    log = frappe.new_doc("Daily Task Log")
    log.employee = employee.name
    log.date = date
    log.log_type = "Morning Check-In"
    log.login_time = actual_login_time
    log.insert(ignore_permissions=True)
    log.submit()

    # Employee Checkin IN
    _make_checkin(employee.name, "IN", actual_login_time)
    frappe.db.commit()

    return {"success": True, "login_time": actual_login_time}


# ── EOD submit ─────────────────────────────────────────────────────────────────

@frappe.whitelist()
def submit_eod_log(lunch_from, lunch_to, logout_time, task_updates, adhoc_tasks):
    """
    Called when employee clicks 'Check Out & Submit'.
    Updates task statuses, inserts ad-hoc tasks,
    creates EOD Daily Task Log, triggers Checkin (OUT),
    then rolls over pending tasks to next working day.

    lunch_from / lunch_to replace the old single lunch_time field.
    """
    employee = _get_employee()
    date = today()

    if not logout_time:
        frappe.throw("Logout time is required.")

    # Guard: prevent double submit
    if frappe.db.exists("Daily Task Log", {
        "employee": employee.name,
        "date": date,
        "log_type": "End of Day",
        "docstatus": 1,
    }):
        frappe.throw("You have already submitted End of Day for today.")

    updates = json.loads(task_updates) if isinstance(task_updates, str) else task_updates
    adhocs  = json.loads(adhoc_tasks)  if isinstance(adhoc_tasks,  str) else adhoc_tasks

    # Update existing task statuses
    for t in updates:
        name = t.get("name")
        if not name:
            continue
        frappe.db.set_value("Daily Task", name, {
            "status":  t.get("status", "Pending"),
            "remarks": t.get("remarks", ""),
        })

    # Insert ad-hoc tasks
    for t in adhocs:
        desc = (t.get("description") or "").strip()
        if not desc:
            continue
        task_doc = frappe.new_doc("Daily Task")
        task_doc.employee   = employee.name
        task_doc.task_date  = date
        task_doc.description = desc
        task_doc.task_type  = "Ad-hoc"
        task_doc.status     = t.get("status", "Done")
        task_doc.origin_date = date
        task_doc.remarks    = t.get("remarks", "")
        task_doc.insert(ignore_permissions=True)

    # Create and submit EOD log
    log = frappe.new_doc("Daily Task Log")
    log.employee    = employee.name
    log.date        = date
    log.log_type    = "End of Day"
    log.lunch_from  = lunch_from or ""
    log.lunch_to    = lunch_to   or ""
    log.logout_time = logout_time
    log.insert(ignore_permissions=True)
    log.submit()

    # Employee Checkin OUT
    _make_checkin(employee.name, "OUT", logout_time)

    # Roll over pending tasks to next working day
    pending_count = _rollover_pending_tasks(employee.name, date)

    frappe.db.commit()

    return {
        "success":       True,
        "pending_count": pending_count,
        "logout_time":   logout_time,
    }