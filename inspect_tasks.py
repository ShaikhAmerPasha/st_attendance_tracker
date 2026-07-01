import frappe
import json

def run():
    # Find employee linked to the active session user or ateeq@standardtouch.com
    emp_user = frappe.session.user
    if emp_user == "Administrator" or emp_user == "Guest":
        emp_user = "ateeq@standardtouch.com"
    
    emp = frappe.db.get_value("Employee", {"user_id": emp_user}, ["name", "employee_name"], as_dict=True)
    if not emp:
        print("Employee not found for user:", emp_user)
        emps = frappe.get_all("Employee", fields=["name", "employee_name", "user_id"])
        print("All employees:", emps)
        return

    print(f"Found employee: {emp.employee_name} ({emp.name})")

    # Get all tasks for this employee
    tasks = frappe.get_all("Daily Task",
        filters={"employee": emp.name},
        fields=["name", "description", "status", "task_date", "rolled_over_from", "origin_date", "project_name", "creation"],
        order_by="task_date desc, creation desc"
    )

    print("\n--- Daily Tasks ---")
    for t in tasks:
        print(f"Date: {t.task_date} | Name: {t.name} | Status: {t.status} | Desc: {t.description} | Project: {t.project_name} | Rolled From: {t.rolled_over_from} | Origin: {t.origin_date}")

    # Get all daily task logs
    logs = frappe.get_all("Daily Task Log",
        filters={"employee": emp.name},
        fields=["name", "date", "log_type", "login_time", "logout_time", "net_hours", "docstatus"],
        order_by="date desc, log_type asc"
    )
    print("\n--- Daily Task Logs ---")
    for l in logs:
        print(f"Date: {l.date} | Log Type: {l.log_type} | Login: {l.login_time} | Logout: {l.logout_time} | Net: {l.net_hours} | Docstatus: {l.docstatus}")

if __name__ == "__main__":
    run()
