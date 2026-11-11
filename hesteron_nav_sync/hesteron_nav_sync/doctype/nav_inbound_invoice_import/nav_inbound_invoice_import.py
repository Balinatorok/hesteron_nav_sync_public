import frappe
from frappe import _
from frappe.model.document import Document

from hesteron_nav_sync.matching import (
	STATUS_MATCHED, STATUS_NEEDS_CATEGORY, STATUS_NEW, STATUS_SKIP, STATUS_SUGGESTED,
)
from hesteron_nav_sync.routing import NON_STOCK_TARGETS, TARGET_STOCK, detect_carrier


class NAVInboundInvoiceImport(Document):
	def validate(self):
		self.title = f"{self.supplier_name or self.supplier_tax_number or '?'} – {self.nav_invoice_number}"
		self._ensure_unique_per_supplier()
		self._sync_line_status_with_item()
		if self.status not in ("Bevételezés létrehozva", "Számla létrehozva", "Módosító számla", "Kihagyva", "Hiba"):
			self.status = self.derive_status()

	def _ensure_unique_per_supplier(self):
		"""Invoice numbers are only unique per supplier — two suppliers may both issue '2026/000123'."""
		dup = frappe.db.get_value(
			"NAV Inbound Invoice Import",
			{"nav_invoice_number": self.nav_invoice_number, "supplier_tax_number": self.supplier_tax_number, "name": ["!=", self.name]},
			"name",
		)
		if dup:
			frappe.throw(_("Ez a NAV számla ({0}, szállító {1}) már importálva van: {2}").format(self.nav_invoice_number, self.supplier_tax_number, dup))

	def _sync_line_status_with_item(self):
		"""If the user picks an item / target / category on a row, keep match_status consistent."""
		for row in self.lines:
			if row.match_status == STATUS_SKIP:
				continue
			row.target = row.target or TARGET_STOCK
			if row.target in NON_STOCK_TARGETS:
				# a cost line needs a category, not an item
				row.item_code = None
				row.match_method = "category"
				row.match_status = STATUS_MATCHED if row.expense_category else STATUS_NEEDS_CATEGORY
				row.match_confidence = 100 if row.expense_category else 0
				if row.expense_category and frappe.db.get_value("NAV Expense Category", row.expense_category, "group_by_carrier"):
					row.group_label = row.group_label or detect_carrier(row.nav_description)
				else:
					row.group_label = None
				continue
			if row.item_code:
				# a manually picked / newly created item: keep the serial/batch flags right, so the
				# summary warns "Sorozatszámos sor" before the receipt is made (missed on 26-5081642)
				flags = frappe.get_cached_value("Item", row.item_code, ["has_serial_no", "has_batch_no", "is_stock_item"], as_dict=True)
				if flags:
					row.has_serial_no = int(bool(flags.has_serial_no))
					row.has_batch_no = int(bool(flags.has_batch_no))
					row.is_stock_item = int(bool(flags.is_stock_item))
			# stock line
			row.expense_category = None
			row.group_label = None
			if row.match_method == "category":  # switched back from cost to stock
				row.match_method = "manual" if row.item_code else "none"
				row.match_status = STATUS_MATCHED if row.item_code else STATUS_NEW
				row.match_confidence = 100 if row.item_code else 0
			if row.match_status == STATUS_NEEDS_CATEGORY:
				row.match_status = STATUS_MATCHED if row.item_code else STATUS_NEW
			if row.item_code and row.match_status in (STATUS_NEW, None, ""):
				row.match_status = STATUS_MATCHED
				row.match_method = row.match_method if row.match_method not in ("none", None, "") else "manual"
				row.match_confidence = 100
			if row.item_code and row.match_method == "manual":
				row.match_confidence = 100
			if not row.item_code and row.match_status in (STATUS_MATCHED, STATUS_SUGGESTED):
				row.match_status = STATUS_NEW
			if row.item_code:
				item = frappe.db.get_value("Item", row.item_code, ["has_serial_no", "has_batch_no", "is_stock_item", "stock_uom"], as_dict=True)
				if item:
					row.has_serial_no, row.has_batch_no, row.is_stock_item = item.has_serial_no, item.has_batch_no, item.is_stock_item
					row.uom = row.uom or item.stock_uom
					if frappe.get_meta("Item").has_field("custom_helykód"):
						row.helykod = frappe.db.get_value("Item", row.item_code, "custom_helykód")

	def derive_status(self) -> str:
		if self.is_modification:
			return "Módosító számla"
		active = [r for r in self.lines if r.match_status != STATUS_SKIP]
		if not active:
			return "Kihagyva"

		def ready(r):
			if (r.target or TARGET_STOCK) in NON_STOCK_TARGETS:
				return bool(r.expense_category)
			return r.match_status == STATUS_MATCHED and bool(r.item_code)

		if not all(ready(r) for r in active):
			return "Egyeztetésre vár"
		has_stock = any((r.target or TARGET_STOCK) == TARGET_STOCK for r in active)
		return "Bevételezhető" if has_stock else "Rögzíthető"

	@frappe.whitelist()
	def create_supplier(self, target=None, expense_category=None):
		"""One click for an unknown supplier: create it from the NAV data (name, tax number,
		address from queryTaxpayer when reachable), optionally set its NAV default, then re-match
		this and every other open invoice from the same tax number."""
		if self.supplier:
			return {"supplier": self.supplier, "created": False}
		if not self.supplier_tax_number:
			frappe.throw(_("Ennél a számlánál nincs szállítói adószám."))

		from hesteron_nav_sync.matching import find_supplier

		existing = find_supplier(self.supplier_tax_number)
		name = existing or _create_supplier_from_nav(self, target, expense_category)

		from hesteron_nav_sync.sync import apply_matching

		others = frappe.get_all(
			"NAV Inbound Invoice Import",
			filters={"supplier_tax_number": self.supplier_tax_number, "name": ["!=", self.name],
				"status": ["in", ["Új", "Egyeztetésre vár", "Bevételezhető", "Rögzíthető", "Hiba"]]},
			pluck="name",
		)
		for other_name in others:
			other = frappe.get_doc("NAV Inbound Invoice Import", other_name)
			apply_matching(other)
			other.save()
		apply_matching(self)
		self.save()
		return {"supplier": name, "created": not existing, "also_updated": len(others), "status": self.status}

	@frappe.whitelist()
	def rerun_matching(self):
		from hesteron_nav_sync.sync import apply_matching

		apply_matching(self)
		self.save()
		return self.status

	@frappe.whitelist()
	def set_supplier_default(self, target=None, expense_category=None):
		"""Set the supplier's NAV default from this invoice (one click for e.g. every courier invoice),
		then re-run matching on this invoice and on its other still-open invoices."""
		if not self.supplier:
			frappe.throw(_("Előbb válaszd ki (vagy hozd létre) a Szállítót."))
		supplier = frappe.get_doc("Supplier", self.supplier)
		supplier.nav_default_target = target or None
		supplier.nav_expense_category = expense_category if target and target != TARGET_STOCK else None
		supplier.save()  # respects Supplier write permission

		from hesteron_nav_sync.sync import apply_matching

		others = frappe.get_all(
			"NAV Inbound Invoice Import",
			filters={"name": ["!=", self.name], "status": ["in", ["Új", "Egyeztetésre vár", "Bevételezhető", "Rögzíthető"]]},
			or_filters={"supplier": self.supplier, "supplier_tax_number": self.supplier_tax_number or "~"},
			pluck="name",
		)
		for name in others:
			other = frappe.get_doc("NAV Inbound Invoice Import", name)
			apply_matching(other)
			other.save()
		apply_matching(self)
		self.save()
		return {"status": self.status, "also_updated": len(others)}

	@frappe.whitelist()
	def confirm_all_suggestions(self):
		for row in self.lines:
			if row.match_status == STATUS_SUGGESTED and row.item_code:
				row.match_status = STATUS_MATCHED
				row.match_confidence = 100
		self.save()
		return self.status

	@frappe.whitelist()
	def create_purchase_receipt(self):
		from hesteron_nav_sync.receipt import create_purchase_receipt

		return create_purchase_receipt(self)

	@frappe.whitelist()
	def create_purchase_invoice(self):
		from hesteron_nav_sync.receipt import create_purchase_invoice

		pi = create_purchase_invoice(self)
		if pi:
			# e-mailed invoice PDF(s) already on the import -> onto the Purchase Invoice too
			from hesteron_nav_sync.invoice_pdf import copy_to_purchase_invoice

			copy_to_purchase_invoice(self.name, pi)
		return pi


def _create_supplier_from_nav(doc, target=None, expense_category=None):
	settings = frappe.get_single("NAV Connection Settings")
	info = None
	try:
		info = settings.get_client().query_taxpayer(doc.supplier_tax_number)
	except Exception:
		info = None  # address is a nice-to-have; the tax number is what matching needs

	tax_id = doc.supplier_tax_number
	if info and info.get("vat_code") and info.get("county_code"):
		tax_id = f"{info.get('tax_number') or doc.supplier_tax_number}-{info['vat_code']}-{info['county_code']}"

	group = frappe.db.get_single_value("Buying Settings", "supplier_group") or frappe.db.get_value(
		"Supplier Group", {"is_group": 0}, "name"
	)
	sup = frappe.get_doc({
		"doctype": "Supplier",
		"supplier_name": (doc.supplier_name or (info or {}).get("name") or tax_id).strip(),
		"supplier_type": "Company",
		"supplier_group": "Services" if target and target != TARGET_STOCK and frappe.db.exists("Supplier Group", "Services") else group,
		"country": "Hungary",
		"tax_id": tax_id,
	})
	if frappe.db.exists("Tax Category", "Belföldi 27%"):
		sup.tax_category = "Belföldi 27%"
	if sup.meta.has_field("nav_default_target") and target:
		sup.nav_default_target = target
		if target != TARGET_STOCK and expense_category:
			sup.nav_expense_category = expense_category
	sup.insert()

	if info and info.get("city"):
		frappe.get_doc({
			"doctype": "Address",
			"address_title": sup.supplier_name,
			"address_type": "Billing",
			"address_line1": info.get("street") or info.get("city"),
			"city": info.get("city"),
			"pincode": info.get("postal_code"),
			"country": "Hungary",
			"is_primary_address": 1,
			"links": [{"link_doctype": "Supplier", "link_name": sup.name}],
		}).insert(ignore_permissions=True)
	return sup.name
