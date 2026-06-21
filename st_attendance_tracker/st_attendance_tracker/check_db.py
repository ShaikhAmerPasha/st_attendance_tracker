import frappe

def run():
    print("Error Log fields:")
    logs = frappe.db.get_all('Error Log', limit=1, fields=['*'])
    if logs:
        print(logs[0].keys())
        # Print logs containing NetHours
        print("\nSearching logs...")
        all_logs = frappe.db.get_all('Error Log', order_by='creation desc', limit=100, fields=['name', 'method', 'error'])
        for l in all_logs:
            if "NetHours" in str(l.method) or "net" in str(l.error) or "net" in str(l.method) or "NetHours" in str(l.error):
                print(f"METHOD: {l.method}\nERROR:\n{l.error}\n---")
