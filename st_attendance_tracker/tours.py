"""
Whitelisted API backing the driver.js-based product tours on the www/
portal pages. Tracks which tour ids a user has already completed or
dismissed (ST Tour Seen) so a tour only auto-plays once per user — a manual
"Take a tour" button always replays it regardless of seen state.
"""
import frappe

# New tour ids must be added here before a page can mark/query them, so a
# stray/typo'd id from the client can't silently accumulate in the DB.
KNOWN_TOUR_IDS = {
    "task_backlog_v1",
    "daily_checkin_v1",
    "team_dashboard_v1",
    "management_dashboard_v1",
    "my_history_v1",
    "recurring_tasks_v1",
    "additional_work_v1",
}


def _seen_ids(user):
    tours_seen = frappe.db.get_value("ST Tour Seen", user, "tours_seen") or ""
    return set(filter(None, tours_seen.split(",")))


@frappe.whitelist()
def has_seen_tour(tour_id):
    if tour_id not in KNOWN_TOUR_IDS:
        frappe.throw(f"Unknown tour id: {tour_id}")
    return {"seen": tour_id in _seen_ids(frappe.session.user)}


@frappe.whitelist()
def mark_tour_seen(tour_id):
    if tour_id not in KNOWN_TOUR_IDS:
        frappe.throw(f"Unknown tour id: {tour_id}")

    user = frappe.session.user
    seen_ids = _seen_ids(user)
    seen_ids.add(tour_id)
    tours_seen = ",".join(sorted(seen_ids))

    if frappe.db.exists("ST Tour Seen", user):
        frappe.db.set_value("ST Tour Seen", user, "tours_seen", tours_seen)
    else:
        frappe.get_doc({
            "doctype": "ST Tour Seen",
            "user": user,
            "tours_seen": tours_seen,
        }).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"success": True}
