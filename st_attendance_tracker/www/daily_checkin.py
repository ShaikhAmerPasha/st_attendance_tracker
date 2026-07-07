import frappe
from frappe.utils import today, now_datetime, getdate
from st_attendance_tracker.api import _to_hhmm, _to_ampm, _format_hours


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/daily-checkin"
        raise frappe.Redirect

    # Management → redirect immediately to management dashboard
    user_roles = frappe.get_roles(frappe.session.user)
    if "HR Manager" in user_roles:
        frappe.local.flags.redirect_location = "/management-dashboard"
        raise frappe.Redirect

    employee = frappe.db.get_value(
        "Employee",
        {"user_id": frappe.session.user},
        ["name", "employee_name", "department", "reports_to", "work_type"],
        as_dict=True,
    )
    if not employee:
        frappe.throw(
            "Your account is not linked to an Employee record. "
            "Please contact HR or System Administrator."
        )

    date = today()
    latest_checkin = frappe.db.get_value("Daily Task Log", {
        "employee": employee.name,
        "log_type": "Morning Check-In",
        "docstatus": 1
    }, ["name", "date"], order_by="date desc", as_dict=True)

    if latest_checkin:
        has_eod = frappe.db.exists("Daily Task Log", {
            "employee": employee.name,
            "date": latest_checkin.date,
            "log_type": "End of Day",
            "docstatus": 1
        })
        if not has_eod:
            date = latest_checkin.date

    date_obj = getdate(date)

    # ── Work location configuration ───────────────────────────────────────
    work_location_config = _get_work_location_config(employee, date_obj)

    # ── Safety rollover — ALWAYS runs against today() ─────────────────────
    # Even if the page renders an older open check-in date, we must still
    # carry forward any missed Pending/In-Progress tasks to today's date.
    actual_today = today()
    existing_morning_today = frappe.db.exists("Daily Task Log", {
        "employee": employee.name, "date": actual_today,
        "log_type": "Morning Check-In", "docstatus": 1,
    })
    if not existing_morning_today:
        from st_attendance_tracker.api import _safety_rollover
        _safety_rollover(employee.name, actual_today)


    morning_log = frappe.db.exists("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "Morning Check-In", "docstatus": 1,
    })
    eod_log = frappe.db.exists("Daily Task Log", {
        "employee": employee.name, "date": date,
        "log_type": "End of Day", "docstatus": 1,
    })

    login_time_val  = ""
    net_hours_val   = ""
    logout_time_val = ""
    work_location_val = ""
    lunch_from_val  = ""
    lunch_to_val    = ""

    # Check if employee has an approved half-day leave today
    is_half_day_leave = False
    try:
        dt = getdate(date)
        is_half_day_leave = bool(frappe.db.exists("Leave Application", {
            "employee": employee.name,
            "status": "Approved",
            "docstatus": 1,
            "half_day": 1,
            "half_day_date": dt
        }))
        if not is_half_day_leave:
            # Also check if within from_date and to_date range
            is_half_day_leave = bool(frappe.db.exists("Leave Application", {
                "employee": employee.name,
                "status": "Approved",
                "docstatus": 1,
                "half_day": 1,
                "from_date": ["<=", dt],
                "to_date": [">=", dt]
            }))
    except Exception:
        pass


    # Always load tasks for today — needed for pre-checkin carried display
    tasks = frappe.get_all("Daily Task",
        filters={"employee": employee.name, "task_date": date},
        fields=["name", "description", "status", "task_type",
                "origin_date", "rolled_over_from", "remarks",
                "estimated_time", "actual_time", "project_name"],
        order_by="project_name asc, creation asc",
    )
    for task in tasks:
        task["is_carried"] = bool(task.get("rolled_over_from"))
        if task.get("rolled_over_from") and task.get("origin_date"):
            task["days_pending"] = (
                frappe.utils.getdate(date) -
                frappe.utils.getdate(task["origin_date"])
            ).days
        else:
            task["days_pending"] = 0

    if morning_log:
        raw_login = frappe.db.get_value(
            "Daily Task Log", morning_log, "login_time"
        ) or ""
        # FIX: _to_ampm converts timedelta → 'HH:MM AM/PM' with leading zero.
        # The old str(timedelta)[:5] produced '9:30:' (trailing colon) for
        # single-digit hours, breaking both display and recalc().
        login_time_val = _to_ampm(raw_login)
        work_location_val = frappe.db.get_value(
            "Daily Task Log", morning_log, "work_location"
        ) or ""

    working_hours_val = ""
    if eod_log:
        eod_data = frappe.db.get_value(
            "Daily Task Log", eod_log,
            ["net_hours", "logout_time", "lunch_from", "lunch_to", "working_hours"], as_dict=True
        )
        net_hours_val   = eod_data.net_hours    or ""
        # FIX: use _to_ampm for all time fields — timedelta → 'HH:MM AM/PM'
        logout_time_val = _to_ampm(eod_data.logout_time)
        lunch_from_val  = _to_ampm(eod_data.lunch_from)
        lunch_to_val    = _to_ampm(eod_data.lunch_to)
        working_hours_val = _format_hours(eod_data.working_hours) or "0h"

    # Group tasks by project
    projects = {}
    standalone = []
    for task in tasks:
        pname = (task.get("project_name") or "").strip()
        if pname:
            if pname not in projects:
                projects[pname] = []
            projects[pname].append(task)
        else:
            standalone.append(task)

    is_team_leader = bool(frappe.db.exists("Employee", {
        "reports_to": employee.name, "status": "Active"
    }))

    done_count = sum(1 for t in tasks if t.get("status") == "Done")

    # ── Fetch Active Shift, Leaves & Reset Status ─────────────────────
    shift_info = None
    try:
        from hrms.hr.doctype.shift_assignment.shift_assignment import get_employee_shift
        shift_details = get_employee_shift(employee.name, consider_default_shift=True)
        if shift_details:
            shift_info = {
                "name": shift_details.shift_type.name,
                "start_time": _to_hhmm(shift_details.shift_type.start_time),
                "end_time": _to_hhmm(shift_details.shift_type.end_time),
                "start_time_ampm": _to_ampm(shift_details.shift_type.start_time),
                "end_time_ampm": _to_ampm(shift_details.shift_type.end_time),
            }
    except Exception:
        pass

    leave_today = None
    try:
        leave_today = frappe.db.get_value("Leave Application", {
            "employee": employee.name,
            "from_date": ["<=", date],
            "to_date": [">=", date],
            "status": "Approved",
            "docstatus": 1
        }, "leave_type")
    except Exception:
        pass

    has_reset_today = bool(frappe.db.exists("Daily Task Log", {
        "employee": employee.name,
        "date": date,
        "log_type": "Morning Check-In",
        "docstatus": 2
    }))

    context.no_cache = 1
    context.employee = employee
    context.date = date
    context.current_time = now_datetime().strftime("%H:%M")
    context.current_time_ampm = _to_ampm(now_datetime().strftime("%H:%M:%S"))
    context.is_checked_in = bool(morning_log)
    context.eod_done = bool(eod_log)
    context.tasks = tasks
    context.projects = projects
    context.standalone = standalone
    context.carried_count = sum(1 for t in tasks if t.get("is_carried"))
    context.login_time = login_time_val
    context.logout_time = logout_time_val
    context.net_hours = net_hours_val
    context.lunch_from = lunch_from_val
    context.lunch_to   = lunch_to_val
    context.work_location = work_location_val
    context.is_team_leader = is_team_leader
    context.done_count = done_count
    context.total_count = len(tasks)
    context.work_location_config = work_location_config
    context.is_half_day_leave = is_half_day_leave
    context.shift_info = shift_info
    context.leave_today = leave_today
    context.has_reset_today = has_reset_today
    context.working_hours = working_hours_val
    context.title = "Daily Check-In"


def _get_work_location_config(employee, date_obj):
    """
    Returns work location field config for the check-in form as a DICT.
    Jinja will serialize with | tojson.

    Rules:
      - Saturday          → WFH, readonly, no validation (everyone)
      - Remote employee   → Remote, readonly, no validation
      - Office employee   → Office / WFH (WFH needs Attendance Request)
      - Hybrid + office day (Tue/Thu by default)
                           → Office / WFH (WFH needs Attendance Request,
                             since they're expected in office that day)
      - Hybrid + non-office day
                           → Office / Hybrid (WFH) — no Attendance Request
                             needed, this IS their normal routine. They can
                             still choose Office if they want to come in.
    """
    work_type = (employee.get("work_type") or "Office").strip()
    weekday_num = date_obj.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
    is_saturday = weekday_num == 5

    # Saturday — everyone is WFH, no validation needed
    if is_saturday:
        return {
            "value":    "WFH",
            "readonly": True,
            "options":  ["WFH"],
            "validate": False,
            "note":     "All employees work from home on Saturdays.",
        }

    # Remote employee — always remote, readonly
    if work_type == "Remote":
        return {
            "value":    "Remote",
            "readonly": True,
            "options":  ["Remote"],
            "validate": False,
            "note":     "You are a remote employee.",
        }

    # Get hybrid office days from settings (dynamic)
    hybrid_days = _get_hybrid_office_days()
    is_hybrid_office_day = (work_type == "Hybrid") and (weekday_num in hybrid_days)

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    if work_type == "Hybrid":
        if is_hybrid_office_day:
            # Office day for hybrid — same strict rule as Office employees
            note = (
                f"Today ({day_names[weekday_num]}) is an office day for hybrid "
                f"employees. Select Office, or apply for WFH first."
            )
            return {
                "value":    "Office",
                "readonly": False,
                "options":  ["Office", "WFH"],
                "validate": True,
                "hybrid_office_day": True,
                "note":     note,
            }
        else:
            # Non-office day — WFH is their normal routine, no AR needed.
            # Still allow them to choose Office if they want to come in.
            # NOTE: value/options must stay within Daily Task Log.work_location's
            # allowed Select values ("", "Office", "WFH", "Remote"). The friendlier
            # "Hybrid (WFH)" wording is applied client-side as a display label only —
            # the underlying value saved to the database is always "WFH".
            note = (
                f"Today ({day_names[weekday_num]}) is a regular WFH day for "
                f"hybrid employees. No Attendance Request needed."
            )
            return {
                "value":    "WFH",
                "readonly": False,
                "options":  ["WFH", "Office"],
                "validate": False,
                "hybrid_office_day": False,
                "hybrid_routine_day": True,
                "note":     note,
            }

    # Office employee
    return {
        "value":    "Office",
        "readonly": False,
        "options":  ["Office", "WFH"],
        "validate": True,
        "hybrid_office_day": False,
        "note":     "Select your work location for today.",
    }


def _get_hybrid_office_days():
    """
    Returns list of weekday numbers for hybrid office days.
    Reads from ST Attendance Settings — fully dynamic.
    0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat
    """
    day_map = {
        "monday": 0, "tuesday": 1, "wednesday": 2,
        "thursday": 3, "friday": 4, "saturday": 5,
    }
    try:
        raw = frappe.db.get_single_value(
            "ST Attendance Settings", "hybrid_office_days"
        ) or "Tuesday\nThursday"
        days = []
        for line in raw.strip().split("\n"):
            d = line.strip().lower()
            if d in day_map:
                days.append(day_map[d])
        return days if days else [1, 3]  # Default: Tue, Thu
    except Exception:
        return [1, 3]