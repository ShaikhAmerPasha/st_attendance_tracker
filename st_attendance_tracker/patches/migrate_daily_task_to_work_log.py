"""v1 -> v2 data migration: standalone `Daily Task` + submit/cancel
`Daily Task Log` -> one `Daily Work Log` per (employee, date) with `Task
Entry` child rows. See specs/daily-work-log-refactor.md Section 7.

Idempotent: safe to re-run — any (employee, date) that already has a
Daily Work Log is skipped. Each employee-day group is wrapped in its own
savepoint so one bad row aborts only that group, not the whole migration.
Old `Daily Task` / `Daily Task Log` tables are left untouched — this patch
only reads them.
"""

import frappe


def execute():
    have_tasks = frappe.db.table_exists("Daily Task")
    have_logs = frappe.db.table_exists("Daily Task Log")
    if not have_tasks and not have_logs:
        return

    task_rows = frappe.get_all("Daily Task", fields=[
        "name", "employee", "task_date", "description", "project_name", "task_type",
        "status", "estimated_time", "actual_time", "remarks", "sequence",
        "origin_date", "rolled_over_from",
    ]) if have_tasks else []

    log_rows = frappe.get_all("Daily Task Log", filters={"docstatus": 1}, fields=[
        "employee", "date", "log_type", "work_location", "half_day_session",
        "is_late", "login_time", "lunch_from", "logout_time", "lunch_to", "net_hours",
    ]) if have_logs else []

    task_by_name = {t.name: t for t in task_rows}

    # One series_id per rolled_over_from chain, resolved once here — the
    # last time this app ever walks that chain (spec Section 7 step 3).
    series_by_root = {}
    series_by_task = {}

    def resolve_series(name):
        if name in series_by_task:
            return series_by_task[name]
        chain = []
        current = name
        seen = set()
        while current and current not in seen:
            seen.add(current)
            chain.append(current)
            current_row = task_by_name.get(current)
            parent = current_row.rolled_over_from if current_row else None
            if not parent or parent not in task_by_name:
                break
            current = parent
        root = chain[-1]
        series_id = series_by_root.get(root)
        if not series_id:
            series_id = frappe.generate_hash(length=32)
            series_by_root[root] = series_id
        for n in chain:
            series_by_task[n] = series_id
        return series_id

    tasks_by_day = {}
    for t in task_rows:
        tasks_by_day.setdefault((t.employee, str(t.task_date)), []).append(t)

    logs_by_day = {}
    for l in log_rows:
        logs_by_day.setdefault((l.employee, str(l.date)), []).append(l)

    all_days = set(tasks_by_day.keys()) | set(logs_by_day.keys())

    migrated = 0
    for employee, date in all_days:
        if not employee or not date or date == "None":
            continue
        if frappe.db.exists("Daily Work Log", {"employee": employee, "date": date}):
            continue  # already migrated — safe to re-run

        savepoint = f"v1_to_v2_{abs(hash((employee, date)))}"
        frappe.db.savepoint(savepoint)
        try:
            day_logs = logs_by_day.get((employee, date), [])
            morning = next((l for l in day_logs if l.log_type == "Morning Check-In"), None)
            eod = next((l for l in day_logs if l.log_type == "End of Day"), None)

            doc = frappe.new_doc("Daily Work Log")
            doc.employee = employee
            doc.date = date
            doc.morning_submitted = 1 if morning else 0
            doc.eod_submitted = 1 if eod else 0
            # DailyWorkLog.__init__ already cleared the four Time fields'
            # nowtime() auto-default and set dont_update_if_missing so they
            # stay genuinely blank for days this migration has no data for.

            if morning:
                doc.login_time = morning.login_time
                doc.work_location = morning.work_location
                doc.half_day_session = morning.half_day_session
                doc.is_late = morning.is_late
            if eod:
                doc.logout_time = eod.logout_time
                doc.lunch_from = eod.lunch_from
                doc.lunch_to = eod.lunch_to
                doc.net_hours = eod.net_hours
                # The morning log is the source of truth for login_time/
                # work_location when both exist (matches the old app's own
                # read pattern) — only fall back to the EOD log's copy when
                # there was no morning check-in at all.
                if not morning:
                    doc.login_time = eod.login_time
                    doc.work_location = eod.work_location

            for t in sorted(tasks_by_day.get((employee, date), []), key=lambda r: r.sequence or 0):
                row = doc.append("tasks", {})
                row.series_id = resolve_series(t.name)
                row.origin_date = t.origin_date or t.task_date
                row.description = t.description
                row.project_name = t.project_name
                row.task_type = t.task_type
                row.status = t.status
                row.estimated_time = t.estimated_time
                row.actual_time = t.actual_time
                row.remarks = t.remarks
                row.sequence = t.sequence

            # Skip validate()/before_save() entirely — this writes historical
            # values verbatim (is_late against the threshold that applied on
            # that date, already-parsed hour floats, ...); the controller's
            # own validate() would recompute several of these against
            # *today's* settings, silently corrupting old data.
            doc.flags.ignore_validate = True
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            frappe.db.release_savepoint(savepoint)
            migrated += 1
        except Exception:
            frappe.db.rollback(save_point=savepoint)
            frappe.log_error(
                frappe.get_traceback(),
                f"ST Attendance Tracker v1->v2 migration failed for {employee} {date}",
            )

    frappe.db.commit()
    frappe.logger().info(
        f"ST Attendance Tracker v1->v2 migration: {migrated} Daily Work Log(s) created "
        f"out of {len(all_days)} employee-day(s) found"
    )
