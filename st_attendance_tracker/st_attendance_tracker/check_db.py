import frappe

def run():
    print("Leave Application Fields:")
    try:
        meta = frappe.get_meta("Leave Application")
        fields = [f.fieldname for f in meta.fields]
        print(", ".join(fields))
        
        # Check if there are any Leave Applications in the system
        apps = frappe.db.get_all("Leave Application", limit=5, fields=["name", "employee", "from_date", "to_date", "half_day", "half_day_date", "status", "docstatus"])
        for app in apps:
            print(app)
    except Exception as e:
        print(f"Error checking Leave Application: {e}")
