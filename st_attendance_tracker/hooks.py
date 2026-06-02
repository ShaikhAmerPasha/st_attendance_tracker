app_name = "st_attendance_tracker"
app_title = "ST Attendance Tracker"
app_publisher = "StandardTouch e-Solutions"
app_description = "Daily Attendance & Task Management for ERPNext v15"
app_email = "tech@standardtouch.com"
app_license = "MIT"

scheduler_events = {
    "cron": {
        "30 11 * * 1-6": [
            "st_attendance_tracker.tasks.send_morning_missing_report"
        ],
        "0 22 * * 1-6": [
            "st_attendance_tracker.tasks.send_eod_missing_report"
        ],
    }
}

website_route_rules = [
    {"from_route": "/daily-checkin", "to_route": "daily_checkin"},
]
