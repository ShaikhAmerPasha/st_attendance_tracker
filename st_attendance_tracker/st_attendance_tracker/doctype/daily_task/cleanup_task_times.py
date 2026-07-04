import frappe

def run():
    # Fetch all tasks directly using SQL to bypass any model validation during schema mismatch
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
        est_float = parse_time_to_hours(t.get("estimated_time"))
        act_float = parse_time_to_hours(t.get("actual_time"))
        
        frappe.db.sql(
            "UPDATE `tabDaily Task` SET estimated_time = %s, actual_time = %s WHERE name = %s",
            (str(est_float), str(act_float), t.name)
        )
        count += 1

    frappe.db.commit()
    print(f"Successfully cleaned up {count} Daily Task records in database.")

if __name__ == "__main__":
    run()
