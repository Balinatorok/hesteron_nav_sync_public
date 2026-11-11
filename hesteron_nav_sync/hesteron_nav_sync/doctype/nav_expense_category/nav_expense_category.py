import frappe
from frappe import _
from frappe.model.document import Document


class NAVExpenseCategory(Document):
	def validate(self):
		for field in ("expense_account", "negative_account"):
			account = self.get(field)
			if account and frappe.db.get_value("Account", account, "is_group"):
				frappe.throw(_("{0}: csoportszámla nem választható, csak könyvelhető számla.").format(account))
