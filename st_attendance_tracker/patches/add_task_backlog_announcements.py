import frappe


def execute():
    """Seed What's New announcements for features shipped after
    setup_whats_new_announcements ran: Team Leader task assignment and the
    Task Backlog page. New shipped features don't announce themselves —
    each one needs its own ST Announcement record, same as every prior
    feature."""
    if not frappe.db.table_exists("ST Announcement"):
        return

    seed = [
        {
            "title": "Team Leaders can assign tasks directly",
            "category": "Feature",
            "audience": "Team Leaders & above",
            "description": (
                "Assign a task to any team member straight from the Team Dashboard — "
                "it shows up on their Check-In tagged \"Assigned by\" your name, "
                "so it's never ambiguous where a task came from."
            ),
            "publish_date": "2026-09-14 15:00:00",
        },
        {
            "title": "Task Backlog: capture work without a date",
            "category": "Feature",
            "audience": "Everyone",
            "description": (
                "New \"Task Backlog\" page for tasks with no target day, so they never "
                "inflate today's pending count. Pull a card into Today (drag it, or tap "
                "the arrow) whenever you're actually ready to work on it — Team Leaders "
                "can push tasks into a team member's backlog too, and any existing task "
                "on your Check-In can be moved back to the backlog."
            ),
            "publish_date": "2026-09-14 15:00:00",
        },
    ]
    for row in seed:
        if frappe.db.exists("ST Announcement", {"title": row["title"]}):
            continue
        frappe.get_doc({"doctype": "ST Announcement", "is_published": 1, **row}).insert(ignore_permissions=True)

    frappe.db.commit()
