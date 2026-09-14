"""
Whitelisted API for the "What's New" notification bell shown on the portal
pages. Surfaces recent ST Announcement records to the current user, filtered
by audience tier, and tracks per-user last-seen time (ST Announcement Seen)
so the bell badge/panel only flag what's actually new to them — not a
per-announcement read log, just "seen up to this timestamp", the same
convention most bell-icon notification centers use.
"""
import frappe
from frappe.utils import now_datetime

_AUDIENCE_ROLES = {
    "Everyone": {"Employee", "Team Lead", "HR Manager", "Management"},
    "Team Leaders & above": {"Team Lead", "HR Manager", "Management"},
    "HR Manager & Management": {"HR Manager", "Management"},
}


def _visible_audiences(user_roles):
    roles = set(user_roles)
    return [tier for tier, tier_roles in _AUDIENCE_ROLES.items() if tier_roles & roles]


@frappe.whitelist()
def get_announcements():
    tiers = _visible_audiences(frappe.get_roles(frappe.session.user))
    if not tiers:
        return {"items": [], "unread_count": 0}

    items = frappe.get_all(
        "ST Announcement",
        filters={"audience": ["in", tiers], "is_published": 1},
        fields=["name", "title", "description", "category", "link", "audience", "publish_date"],
        order_by="publish_date desc",
        limit_page_length=20,
    )

    last_seen = frappe.db.get_value("ST Announcement Seen", frappe.session.user, "last_seen")
    unread_count = 0
    for item in items:
        item["is_new"] = bool(not last_seen or item.publish_date > last_seen)
        if item["is_new"]:
            unread_count += 1

    return {"items": items, "unread_count": unread_count}


@frappe.whitelist()
def mark_announcements_seen():
    now = now_datetime()
    if frappe.db.exists("ST Announcement Seen", frappe.session.user):
        frappe.db.set_value("ST Announcement Seen", frappe.session.user, "last_seen", now)
    else:
        frappe.get_doc({
            "doctype": "ST Announcement Seen",
            "user": frappe.session.user,
            "last_seen": now,
        }).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"success": True}
