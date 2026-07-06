import frappe

def execute():
    # Only run if the table Daily Task exists
    if not frappe.db.table_exists("Daily Task"):
        return

    # Only run if the columns estimated_time and actual_time exist in Daily Task
    if not frappe.db.has_column("Daily Task", "estimated_time") or not frappe.db.has_column("Daily Task", "actual_time"):
        return

    # First, handle any NULL or empty string values directly via SQL to make sure we don't hit conversion issues.
    # On sites already migrated, comparing to '' will fail with truncated decimal warning/error, so we catch and fallback.
    try:
        frappe.db.sql("UPDATE `tabDaily Task` SET estimated_time = 0.0 WHERE estimated_time IS NULL OR estimated_time = ''")
    except Exception:
        frappe.db.sql("UPDATE `tabDaily Task` SET estimated_time = 0.0 WHERE estimated_time IS NULL")

    try:
        frappe.db.sql("UPDATE `tabDaily Task` SET actual_time = 0.0 WHERE actual_time IS NULL OR actual_time = ''")
    except Exception:
        frappe.db.sql("UPDATE `tabDaily Task` SET actual_time = 0.0 WHERE actual_time IS NULL")

    # Fetch all tasks to parse and convert any text-based time representations (e.g. "2 hrs, 30 mins")
    tasks = frappe.db.sql("SELECT name, estimated_time, actual_time FROM `tabDaily Task` inline_alias", as_dict=True)
    
    def parse_time_to_hours(s):
        if not s:
            return 0.0
        s = str(s).strip().lower()
        try:
            return float(s)
        except ValueError:
            pass

        h = 0.0
        m = 0.0

        for term in ['hours', 'hour', 'hrs', 'hr']:
            s = s.replace(term, 'h')
        for term in ['minutes', 'minute', 'mins', 'min', 'm']:
            s = s.replace(term, 'm')

        if 'h' in s:
            parts = s.split('h')
            try:
                h = float(parts[0].strip())
            except ValueError:
                pass
            s = parts[1]
        if 'm' in s:
            parts = s.split('m')
            try:
                m = float(parts[0].strip())
            except ValueError:
                pass

        return h + (m / 60.0)

    count = 0
    for t in tasks:
        est_val = t.get("estimated_time")
        act_val = t.get("actual_time")

        # Skip updating if they are already simple numbers/floats
        try:
            if est_val is not None:
                float(est_val)
            est_is_float = True
        except (ValueError, TypeError):
            est_is_float = False

        try:
            if act_val is not None:
                float(act_val)
            act_is_float = True
        except (ValueError, TypeError):
            act_is_float = False

        if est_is_float and act_is_float:
            continue

        est_float = parse_time_to_hours(est_val)
        act_float = parse_time_to_hours(act_val)
        
        frappe.db.sql(
            "UPDATE `tabDaily Task` SET estimated_time = %s, actual_time = %s WHERE name = %s",
            (est_float, act_float, t.name)
        )
        count += 1

    frappe.db.commit()
    print(f"Successfully cleaned up {count} Daily Task records in database.")
