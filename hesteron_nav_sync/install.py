"""after_install / after_migrate: custom fields this app relies on + the default expense categories."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# Default expense categories with suggested account numbers from the Hungarian standard chart of
# accounts — check them with your accountant. An account is only filled in if an account with that
# number exists in the company; a category without an account blocks the Purchase Invoice with a
# clear message. (name, target, group by carrier, keywords, account number)
# Only used when a category does not exist yet; existing ones are never touched.
DEFAULT_CATEGORIES = [
	("Könyvelés", "Költség", 0, "", "527"),
	("Megbízási díj", "Költség", 0, "", "5299"),
	("Szállítási költség", "Költség", 0, "", "815"),
	("Szállítási költség (nem továbbszámlázott)", "Költség", 0, "", "521"),
	("Fuvar (eladáshoz)", "Költség", 0, "", "815"),
	("Fuvar (beszerzéshez)", "Beszerzési járulék", 0, "", ""),
	("Szoftver, előfizetés", "Költség", 0, "", "5299"),
	("Utazás", "Költség", 0, "", ""),
	("Hirdetés, marketing", "Költség", 0, "", "524"),
	("Rezsi-átterhelés", "Költség", 0, "", "5292"),
	("EPR-díj", "Költség", 0, "epr díj", "5299"),
	("Csomagolóanyag", "Saját felhasználás", 0, "", "5121"),
	("Irodaszer, saját eszköz", "Saját felhasználás", 0, "", "5131"),
	("Kerekítés", "Költség", 0, "kerekítés, kerekites", "8699"),
]


def after_install():
	after_migrate()


def after_migrate():
	ensure_custom_fields()
	ensure_default_categories()


def ensure_custom_fields():
	fields = {}
	# Helykód (shelf/location code) on the receipt row — only if the Item has custom_helykód on this site.
	if frappe.get_meta("Item").has_field("custom_helykód"):
		fields["Purchase Receipt Item"] = [
			{
				"fieldname": "custom_helykod",
				"label": "Helykód",
				"fieldtype": "Data",
				"fetch_from": "item_code.custom_helykód",
				"read_only": 1,
				"in_list_view": 1,
				"insert_after": "warehouse",
			}
		]
	# Supplier card: where do this supplier's NAV invoices go by default? (any line can deviate)
	fields["Supplier"] = [
		{
			"fieldname": "nav_import_section",
			"label": "NAV import",
			"fieldtype": "Section Break",
			"insert_after": "tax_withholding_group",
			"collapsible": 0,
		},
		{
			"fieldname": "nav_default_target",
			"label": "NAV számlák alapértelmezett célja",
			"fieldtype": "Select",
			"options": "\nKészletre\nKöltség\nSaját felhasználás\nBeszerzési járulék",
			"insert_after": "nav_import_section",
			"description": "Üresen hagyva: árusorok készletre, a NAV szerint szolgáltatás sorok költségre. Soronként mindig átírható.",
		},
		{
			"fieldname": "nav_column_break",
			"fieldtype": "Column Break",
			"insert_after": "nav_default_target",
		},
		{
			"fieldname": "nav_expense_category",
			"label": "NAV alapértelmezett költségkategória",
			"fieldtype": "Link",
			"options": "NAV Expense Category",
			"insert_after": "nav_column_break",
			"depends_on": "eval:doc.nav_default_target && doc.nav_default_target != 'Készletre'",
		},
	]
	# Purchase Invoice: every date the accountant needs + the accountant's own review of the invoice
	fields["Purchase Invoice"] = [
		{
			"fieldname": "nav_delivery_date",
			"label": "Teljesítés dátuma",
			"fieldtype": "Date",
			"insert_after": "bill_date",
			"description": "A NAV-számla teljesítési dátuma. A könyvelés dátuma a számla kelte.",
		},
		{
			"fieldname": "nav_exchange_rate",
			"label": "Számlán szereplő árfolyam (NAV)",
			"fieldtype": "Float",
			"precision": "6",
			"read_only": 1,
			"insert_after": "nav_delivery_date",
			"depends_on": "eval:doc.nav_exchange_rate",
			"description": "A könyvelés az MNB (számla kelte napi) árfolyamával megy; ez csak összevetésre.",
		},
		{
			"fieldname": "nav_accountant_section",
			"label": "Könyvelői ellenőrzés",
			"fieldtype": "Section Break",
			"insert_after": "remarks",
			"collapsible": 1,
		},
		{
			"fieldname": "nav_accountant_checked",
			"label": "Könyvelő ellenőrizte",
			"fieldtype": "Check",
			"insert_after": "nav_accountant_section",
		},
		{
			"fieldname": "nav_vat_deductible",
			"label": "Áfa levonhatóság",
			"fieldtype": "Select",
			"options": "\nLevonható\nNem levonható\nRészben levonható",
			"insert_after": "nav_accountant_checked",
		},
		{
			"fieldname": "nav_accountant_col",
			"fieldtype": "Column Break",
			"insert_after": "nav_vat_deductible",
		},
		{
			"fieldname": "nav_vat_period",
			"label": "Áfa-időszak (ha eltér a teljesítéstől)",
			"fieldtype": "Data",
			"insert_after": "nav_accountant_col",
			"description": "Pl. 2026-09 — főleg külföldi számláknál.",
		},
		{
			"fieldname": "nav_accountant_note",
			"label": "Könyvelői megjegyzés",
			"fieldtype": "Small Text",
			"insert_after": "nav_vat_period",
		},
	]
	create_custom_fields(fields, ignore_validate=True, update=True)


def ensure_default_categories():
	"""Create missing default categories. Never touches an existing one (accounts are the accountant's)."""
	if not frappe.db.table_exists("NAV Expense Category"):
		return
	company = frappe.db.get_single_value("NAV Connection Settings", "company")
	for name, target, by_carrier, keywords, account_no in DEFAULT_CATEGORIES:
		if frappe.db.exists("NAV Expense Category", name):
			continue
		doc = frappe.get_doc({
			"doctype": "NAV Expense Category",
			"category_name": name,
			"target": target,
			"group_by_carrier": by_carrier,
			"keywords": keywords or None,
		})
		if account_no and company:
			doc.expense_account = _account_by_number(company, account_no)
		if name == "Kerekítés" and company:
			# rounding: against us -> 8699, in our favour -> 9699
			doc.expense_account = _account_by_number(company, "8699")
			doc.negative_account = _account_by_number(company, "9699")
		doc.insert(ignore_permissions=True)


def _account_by_number(company, number):
	return frappe.db.get_value("Account", {"company": company, "account_number": number, "is_group": 0}, "name")
