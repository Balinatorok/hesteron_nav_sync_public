"""
Turns a matched `NAV Inbound Invoice Import` into ERPNext documents — on a click, never automatically.

    create_purchase_receipt(doc)  -> draft Purchase Receipt (stock document; the real receipt
                                     step, the Landed Cost Voucher hangs off this)
    create_purchase_invoice(doc)  -> draft Purchase Invoice made FROM the submitted Purchase Receipt
                                     (ERPNext's own make_purchase_invoice, so the two stay linked)

Serialised items: the receipt row gets `use_serial_batch_fields = 1` and an empty `serial_no`
field. NAV invoices never carry serial numbers, so they must be scanned/typed on the draft receipt
before Submit — the row's match_note already says how many. Nothing here tries to invent them.

Learning: every confirmed row that was not already matched by a rule becomes a
`NAV Item Mapping Rule` (supplier + NAV code, or supplier + exact description), so the next invoice
from the same supplier matches at 100 % without a click.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from hesteron_nav_sync.mapping import resolve_tax_category
from hesteron_nav_sync.matching import STATUS_MATCHED, STATUS_SKIP
from hesteron_nav_sync.routing import NON_STOCK_TARGETS, SOURCE_MANUAL, TARGET_STOCK, aggregate_expense_rows


def create_purchase_receipt(doc) -> str:
	settings = frappe.get_single("NAV Connection Settings")
	if doc.linked_purchase_receipt:
		frappe.throw(_("Ehhez a számlához már készült bevételezés: {0}").format(doc.linked_purchase_receipt))
	if doc.is_modification:
		frappe.throw(_("Módosító/érvénytelenítő számlából nem készül automatikusan bevételezés — kezeld kézzel (Purchase Return / Debit Note)."))
	if not doc.supplier:
		frappe.throw(_("Előbb válaszd ki (vagy hozd létre) a Szállítót."))

	rows = _stock_rows(doc)
	if not rows:
		frappe.throw(_("Ezen a számlán nincs készletre menő sor — bevételezés helyett a 'Beszerzési számla' gombbal rögzítsd."))
	problems = [f"#{r.line_number} {r.nav_description[:40]}" for r in rows if not r.item_code or r.match_status != STATUS_MATCHED]
	if problems:
		frappe.throw(_("Előbb egyeztesd ezeket a sorokat (cikk + 'Egyeztetve'):<br>{0}").format("<br>".join(problems)))

	company_currency = frappe.get_cached_value("Company", settings.company, "default_currency")

	pr = frappe.new_doc("Purchase Receipt")
	pr.company = settings.company
	pr.supplier = doc.supplier
	pr.currency = doc.currency or company_currency
	# the rate printed on the NAV invoice, so ERPNext and the accountant's books agree to the forint
	pr.conversion_rate = _conversion_rate(doc, pr.currency, company_currency) or 1.0
	pr.set_posting_time = 1
	pr.posting_date = doc.delivery_date or doc.invoice_issue_date or nowdate()
	pr.supplier_delivery_note = doc.nav_invoice_number
	pr.set_warehouse = settings.default_warehouse
	pr.ignore_pricing_rule = 1  # the NAV invoice price IS the price; never let a pricing rule rewrite it
	pr.remarks = f"NAV Online Számla import: {doc.supplier_name} {doc.nav_invoice_number}"

	for r in rows:
		item = frappe.get_cached_value("Item", r.item_code, ["is_stock_item", "has_serial_no", "has_batch_no", "stock_uom"], as_dict=True)
		pr_row = pr.append("items", {
			"item_code": r.item_code,
			"qty": flt(r.qty),
			"uom": r.uom or item.stock_uom,
			"rate": flt(r.rate),
			"warehouse": r.warehouse or settings.default_warehouse,
			"cost_center": settings.default_cost_center,
			"purchase_order": r.purchase_order,
			"purchase_order_item": r.purchase_order_item,
			"supplier_part_no": (r.nav_product_code or "").split(",")[0].strip() or None,
		})
		if item.has_serial_no or item.has_batch_no:
			pr_row.use_serial_batch_fields = 1
		if not item.is_stock_item:
			pr_row.warehouse = None

	pr.flags.ignore_permissions = True
	pr.insert()

	doc.linked_purchase_receipt = pr.name
	doc.status = "Bevételezés létrehozva"
	doc.save(ignore_permissions=True)

	if doc.learn_mappings:
		learn_mapping_rules(doc)

	serial_rows = [r for r in rows if r.has_serial_no]
	msg = _("Bevételezés piszkozat létrehozva: {0}").format(frappe.utils.get_link_to_form("Purchase Receipt", pr.name))
	if serial_rows:
		msg += "<br>" + _("Sorozatszámot kell rögzíteni {0} soron, mielőtt beküldöd.").format(len(serial_rows))
	frappe.msgprint(msg, alert=False)
	return pr.name


def create_purchase_invoice(doc) -> str:
	"""
	Stock lines present  -> made FROM the submitted Purchase Receipt, cost lines appended.
	Only cost lines      -> a Purchase Invoice straight away, no receipt, no Items.
	Cost lines are collapsed per category (and per carrier where the category asks for it).
	"""
	settings = frappe.get_single("NAV Connection Settings")
	if doc.linked_purchase_invoice:
		frappe.throw(_("Már készült beszerzési számla: {0}").format(doc.linked_purchase_invoice))
	if doc.is_modification:
		frappe.throw(_("Módosító/érvénytelenítő számlából nem készül automatikusan beszerzési számla — kezeld kézzel (Debit Note)."))
	if not doc.supplier:
		frappe.throw(_("Előbb válaszd ki (vagy hozd létre) a Szállítót."))

	stock_rows = _stock_rows(doc)
	cost_rows = _cost_rows(doc)
	if not stock_rows and not cost_rows:
		frappe.throw(_("Nincs rögzíthető sor (minden sor 'Kihagy')."))

	categories = frappe.get_all("NAV Expense Category", fields=["name", "target", "expense_account", "negative_account", "group_by_carrier"])
	pi_cost_rows, problems = aggregate_expense_rows(
		[{
			"line_number": r.line_number,
			"target": r.target,
			"expense_category": r.expense_category,
			"group_label": r.group_label,
			"net_amount": flt(r.net_amount) or flt(r.qty) * flt(r.rate),
			"nav_description": r.nav_description,
		} for r in cost_rows],
		categories,
	)
	if problems:
		frappe.throw(_("A költségsorok még nem rögzíthetők:<br>{0}").format("<br>".join(problems)))

	company_currency = frappe.get_cached_value("Company", settings.company, "default_currency")

	if stock_rows:
		if not doc.linked_purchase_receipt:
			frappe.throw(_("Előbb hozd létre és küldd be a bevételezést a készletsorokból."))
		if frappe.db.get_value("Purchase Receipt", doc.linked_purchase_receipt, "docstatus") != 1:
			frappe.throw(_("A bevételezés ({0}) még nincs beküldve — a számla a beküldött bevételezésből készül.").format(doc.linked_purchase_receipt))
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

		pi = make_purchase_invoice(doc.linked_purchase_receipt)
	else:
		pi = frappe.new_doc("Purchase Invoice")
		pi.company = settings.company
		pi.supplier = doc.supplier
		pi.currency = doc.currency or company_currency

	# Accounting rule: MNB rate of the invoice's issue date; the NAV rate is kept for comparison
	pi.conversion_rate = _conversion_rate(doc, pi.currency, company_currency) or pi.conversion_rate or 1.0
	pi.bill_no = doc.nav_invoice_number
	pi.bill_date = doc.invoice_issue_date
	# posting (könyvelési) date = invoice issue date (számla kelte); every other date is recorded too
	pi.set_posting_time = 1
	pi.posting_date = doc.invoice_issue_date or doc.delivery_date or pi.posting_date or nowdate()
	if pi.meta.has_field("nav_delivery_date"):
		pi.nav_delivery_date = doc.delivery_date
	if pi.meta.has_field("nav_exchange_rate") and pi.currency != company_currency:
		pi.nav_exchange_rate = flt(doc.exchange_rate) or None
	if _payment_date(doc):
		# ERPNext rejects a due date before the posting/bill date (e.g. card-paid invoices)
		due = frappe.utils.getdate(_payment_date(doc))
		floor = max(frappe.utils.getdate(pi.posting_date), frappe.utils.getdate(pi.bill_date or pi.posting_date))
		pi.due_date = max(due, floor)
	pi.remarks = f"NAV Online Számla import: {doc.supplier_name} {doc.nav_invoice_number}"

	uom = _expense_uom()
	for r in pi_cost_rows:
		pi.append("items", {
			"item_name": r["item_name"],
			"description": r["description"],
			"qty": 1,
			"uom": uom,
			"stock_uom": uom,
			"conversion_factor": 1,
			"rate": r["amount"],
			"expense_account": r["expense_account"],
			"cost_center": settings.default_cost_center,
		})

	tax_category = _document_tax_category(doc)
	if tax_category:
		pi.tax_category = tax_category
		# also for invoices made from a receipt: the receipt usually carries no VAT rows,
		# so without this the invoice would go out with 0 VAT (seen live: 26-5081068)
		if not pi.get("taxes_and_charges") and not pi.get("taxes"):
			template = _purchase_tax_template(doc.supplier, pi.posting_date or nowdate(), settings.company, tax_category)
			if template:
				pi.taxes_and_charges = template

	pi.flags.ignore_permissions = True
	pi.insert()
	if pi.get("taxes_and_charges") and not pi.get("taxes"):
		# ERPNext keeps the template name on insert but drops the rows for a brand-new invoice —
		# copy the template's rows in and save once more so the VAT is really on the draft
		from erpnext.controllers.accounts_controller import get_taxes_and_charges

		for tax in get_taxes_and_charges("Purchase Taxes and Charges Template", pi.taxes_and_charges) or []:
			pi.append("taxes", tax)
		if pi.get("taxes"):
			pi.save()

	doc.linked_purchase_invoice = pi.name
	doc.status = "Számla létrehozva"
	doc.save(ignore_permissions=True)
	if doc.learn_mappings:
		learn_mapping_rules(doc)

	msg = _("Beszerzési számla piszkozat: {0}").format(frappe.utils.get_link_to_form("Purchase Invoice", pi.name))
	warnings = _compare_with_nav(doc, pi)
	if warnings:
		msg += "<br><br><b>" + _("Ellenőrizd:") + "</b><br>" + "<br>".join(warnings)
	frappe.msgprint(msg, indicator="orange" if warnings else "green")
	return pi.name


def _stock_rows(doc):
	return [r for r in doc.lines if r.match_status != STATUS_SKIP and (r.target or TARGET_STOCK) == TARGET_STOCK]


def _cost_rows(doc):
	return [r for r in doc.lines if r.match_status != STATUS_SKIP and (r.target or TARGET_STOCK) in NON_STOCK_TARGETS]


def _expense_uom():
	for uom in ("db", "Db", "Nos", "Unit"):
		if frappe.db.exists("UOM", uom):
			return uom
	return frappe.db.get_value("UOM", {}, "name")


VAT_ROUNDING_TOLERANCE = 10  # HUF


def _conversion_rate(doc, currency, company_currency):
	"""MNB rate on the invoice issue date; falls back to the rate printed on the NAV invoice."""
	if not currency or currency == company_currency:
		return 1.0
	from hesteron_nav_sync.mnb import get_mnb_rate

	day = doc.invoice_issue_date or doc.delivery_date or nowdate()
	rate = get_mnb_rate(currency, day, company_currency)
	if rate:
		nav = flt(doc.exchange_rate)
		if nav and abs(rate - nav) / rate > 0.005:
			frappe.msgprint(
				_("MNB árfolyam ({0}, {1}): {2} — a számlán szereplő: {3}. Az MNB-t használtam.").format(currency, day, rate, nav),
				indicator="blue",
			)
		return rate
	frappe.msgprint(_("Az MNB árfolyam nem érhető el — a számlán szereplő árfolyamot használtam, ellenőrizd."), indicator="orange")
	return flt(doc.exchange_rate) or None


def _compare_with_nav(doc, pi) -> list[str]:
	"""The NAV invoice is the truth: flag if the draft's net or VAT differ by more than 1 unit."""
	out = []
	if doc.net_total is not None and abs(flt(pi.net_total) - flt(doc.net_total)) > 1:
		out.append(_("Nettó eltér: piszkozat {0}, NAV {1}").format(flt(pi.net_total, 2), flt(doc.net_total, 2)))
	# a few forints of VAT rounding (NAV rounds per line) are accepted by the accountant — flag only real gaps
	if doc.vat_total is not None and abs(flt(pi.total_taxes_and_charges) - flt(doc.vat_total)) > VAT_ROUNDING_TOLERANCE:
		out.append(_("ÁFA eltér: piszkozat {0}, NAV {1} — nézd meg az adósablont (vegyes áfakulcs?)").format(flt(pi.total_taxes_and_charges, 2), flt(doc.vat_total, 2)))
	return out


def learn_mapping_rules(doc):
	for r in doc.lines:
		# cost lines: only learn what the user changed by hand (supplier defaults cover the rest)
		if (r.target or TARGET_STOCK) in NON_STOCK_TARGETS:
			if r.get("target_source") != SOURCE_MANUAL or not r.expense_category or r.match_status == STATUS_SKIP:
				continue
			code = (r.nav_product_code or "").split(",")[0].strip() or None
			filters = {"supplier": doc.supplier, "target": r.target, "expense_category": r.expense_category}
			if code:
				filters["supplier_code"] = code
			else:
				filters["description_pattern"] = r.nav_description
			if frappe.db.exists("NAV Item Mapping Rule", filters):
				continue
			frappe.get_doc({
				"doctype": "NAV Item Mapping Rule",
				"supplier": doc.supplier,
				"supplier_code": code,
				"description_pattern": None if code else r.nav_description,
				"target": r.target,
				"expense_category": r.expense_category,
				"learned_from": doc.name,
			}).insert(ignore_permissions=True)
			continue
		if not r.item_code or r.match_status != STATUS_MATCHED or r.match_method in ("rule",):
			continue
		code = (r.nav_product_code or "").split(",")[0].strip() or None
		filters = {"supplier": doc.supplier, "item": r.item_code}
		if code:
			filters["supplier_code"] = code
		else:
			filters["description_pattern"] = r.nav_description
		if frappe.db.exists("NAV Item Mapping Rule", filters):
			continue
		frappe.get_doc({
			"doctype": "NAV Item Mapping Rule",
			"supplier": doc.supplier,
			"supplier_code": code,
			"description_pattern": None if code else r.nav_description,
			"target": TARGET_STOCK,
			"item": r.item_code,
			"learned_from": doc.name,
		}).insert(ignore_permissions=True)


def _purchase_tax_template(supplier, posting_date, company, tax_category):
	"""Purchase VAT template for the invoice's tax category: ERPNext's own rules first
	(Tax Rule / supplier default), then the one enabled template tagged with that category."""
	from erpnext.accounts.party import set_taxes

	template = set_taxes(supplier, "Supplier", posting_date, company, tax_category=tax_category)
	if template:
		return template
	return frappe.db.get_value(
		"Purchase Taxes and Charges Template",
		{"tax_category": tax_category, "company": company, "disabled": 0},
		"name",
	)


def _document_tax_category(doc):
	import json

	parsed = json.loads(doc.invoice_json or "{}")
	categories = set()
	for line in parsed.get("lines", []):
		cat, _needs_review = resolve_tax_category(line.get("vat"))
		if cat:
			categories.add(cat)
	return next(iter(categories)) if len(categories) == 1 else None


def _payment_date(doc):
	import json

	return json.loads(doc.invoice_json or "{}").get("payment_date")
