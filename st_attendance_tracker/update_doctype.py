import json

path = "/home/ameer/Desktop/frappe-bench/apps/st_attendance_tracker/st_attendance_tracker/st_attendance_tracker/doctype/daily_task/daily_task.json"
with open(path, "r") as f:
    data = json.load(f)

for field in data["fields"]:
    if field["fieldname"] in ("estimated_time", "actual_time"):
        field["fieldtype"] = "Float"

with open(path, "w") as f:
    json.dump(data, f, indent=1, ensure_ascii=False)

print("Successfully updated daily_task.json")
