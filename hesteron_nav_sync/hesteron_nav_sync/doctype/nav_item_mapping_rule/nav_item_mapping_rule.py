import frappe
from frappe import _
from frappe.model.document import Document


class NAVItemMappingRule(Document):
	def validate(self):
		if not (self.supplier_code or self.description_pattern):
			frappe.throw(_("Adj meg szállítói kódot vagy megnevezés-mintát."))
		self.target = self.target or "Készletre"
		if self.target == "Készletre":
			if not self.item:
				frappe.throw(_("Készletre célú szabályhoz cikk kell."))
			self.expense_category = None
		else:
			if not self.expense_category:
				frappe.throw(_("Nem készlet célú szabályhoz költségkategória kell."))
			self.item = None
