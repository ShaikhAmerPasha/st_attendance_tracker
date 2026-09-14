import frappe
from frappe.utils import now_datetime


def execute():
    """
    One-time setup for the "What's New" notification bell:
    1. Backfill ST Announcement Seen = now() for every existing user who
       holds one of the app's roles, so shipping this feature doesn't
       suddenly show them a pile of "New" badges for things they already
       know about (they were told directly, outside the app, before this
       shipped). Genuinely new users after this point start with no
       Seen record, so they correctly see the full history as new.
    2. Seed the announcements for what's actually shipped so far.
    """
    if not frappe.db.table_exists("ST Announcement Seen"):
        return

    users = frappe.get_all(
        "Has Role",
        filters={
            "parenttype": "User",
            "role": ["in", ["Employee", "Team Lead", "HR Manager", "Management"]],
        },
        pluck="parent",
    )
    now = now_datetime()
    for user in set(users):
        # Some "Has Role" rows are orphaned (a User deleted via raw SQL in
        # older test cleanup, leaving its child rows behind) — skip those.
        if not frappe.db.exists("User", user):
            continue
        if not frappe.db.exists("ST Announcement Seen", user):
            frappe.get_doc({
                "doctype": "ST Announcement Seen",
                "user": user,
                "last_seen": now,
            }).insert(ignore_permissions=True)

    seed = [
        {
            "title": "Save mid-day tasks instantly",
            "category": "Feature",
            "audience": "Everyone",
            "description": (
                "A \"Save\" button now appears in the footer whenever you add a task "
                "or project — save everything in one click instead of waiting for "
                "checkout, so your Team Leader sees it right away."
            ),
            "publish_date": "2026-09-10 18:00:00",
        },
        {
            "title": "Delete a whole project at once",
            "category": "Improvement",
            "audience": "Everyone",
            "description": (
                "A project's header now has its own delete — removes it and every "
                "task inside in a single action, instead of deleting task-by-task."
            ),
            "publish_date": "2026-09-10 18:00:00",
        },
        {
            "title": "Missed Checkin Checkout report",
            "category": "Report",
            "audience": "HR Manager & Management",
            "description": (
                "A new Desk report lists everyone who missed a check-in or checkout "
                "for a chosen date and department — search \"Missed Checkin Checkout\"."
            ),
            "publish_date": "2026-09-10 20:00:00",
        },
        {
            "title": "Checkout reminder moved to 9 PM",
            "category": "Improvement",
            "audience": "Everyone",
            "description": (
                "Moved earlier from 10:30 PM, and now points you to Additional Work "
                "if you're continuing past your regular hours."
            ),
            "publish_date": "2026-09-12 12:00:00",
        },
        {
            "title": "You're looking at it: What's New",
            "category": "Feature",
            "audience": "Everyone",
            "description": (
                "This bell shows what's changed since your last visit — no more "
                "finding out about a new feature by accident."
            ),
            "publish_date": "2026-09-14 09:00:00",
        },
    ]
    for row in seed:
        if frappe.db.exists("ST Announcement", {"title": row["title"]}):
            continue
        frappe.get_doc({"doctype": "ST Announcement", "is_published": 1, **row}).insert(ignore_permissions=True)

    frappe.db.commit()
