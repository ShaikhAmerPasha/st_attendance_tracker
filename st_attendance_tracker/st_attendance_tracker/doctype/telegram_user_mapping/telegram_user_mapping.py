# Copyright (c) 2026, StandardTouch e-Solutions and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class TelegramUserMapping(Document):
	# DocType controller — no custom logic needed yet; the whitelisted
	# API method in api.py handles all creation/update logic.
	pass
