app_name = "st_attendance_tracker"
app_title = "ST Attendance Tracker"
app_publisher = "StandardTouch e-Solutions"
app_description = "Daily Attendance & Task Management for ERPNext v15"
app_email = "tech@standardtouch.com"
app_license = "MIT"

# ── Custom fields on Employee doctype ─────────────────────────────────────────
# Automatically created on bench migrate
after_migrate = ["st_attendance_tracker.setup.create_custom_fields"]

# ── Scheduled Jobs ─────────────────────────────────────────────────────────────
scheduler_events = {
    "cron": {
        # 10:30 AM IST Mon-Sat
        "30 10 * * 1-6": ["st_attendance_tracker.tasks.send_employee_checkin_reminder"],
        # 11:30 AM IST Mon-Sat
        "30 11 * * 1-6": ["st_attendance_tracker.tasks.send_morning_combined_report"],
        # 10:00 PM IST Mon-Sat (22:00)
        "0 22 * * 1-6":  ["st_attendance_tracker.tasks.send_eod_missing_report"],
    }
}


# ── Web routes ─────────────────────────────────────────────────────────────────
website_route_rules = [
    {"from_route": "/daily-checkin",        "to_route": "daily_checkin"},
    {"from_route": "/team-dashboard",       "to_route": "team_dashboard"},
    {"from_route": "/management-dashboard", "to_route": "management_dashboard"},
    {"from_route": "/my-history",           "to_route": "my_history"},
]