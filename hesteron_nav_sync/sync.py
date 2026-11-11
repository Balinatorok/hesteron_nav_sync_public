"""
NAV queryInvoiceDigest -> queryInvoiceData -> parse -> match -> `NAV Inbound Invoice Import` staging doc.

Nothing here creates stock or accounting documents. That happens in receipt.py, on a human's click,
from the staging doc — the whole point of the staging layer is that the user sees *what the machine
matched and how sure it is* before anything touches the ledger.

Manual run:  bench --site <site> execute hesteron_nav_sync.sync.run_sync
Scheduler:   hooks.py -> hesteron_nav_sync.tasks.sync_inbound_invoices (hourly)

Cursor semantics: `NAV Connection Settings.last_sync_insdate` advances to `now` only after every
digest window was fetched successfully. Per-invoice processing errors are recorded on the staging
row (status "Hiba") and do not block the cursor — one broken invoice must not wedge the sync.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import frappe
from frappe.utils import get_datetime
from frappe.utils.file_manager import save_file

from hesteron_nav_sync.mapping import vat_label
from hesteron_nav_sync.matching import (
	STATUS_MATCHED, STATUS_NEEDS_CATEGORY, MatchContext, base_row, find_supplier, match_line,
)
from hesteron_nav_sync.nav_client.xml_parser import parse_invoice_xml
from hesteron_nav_sync.routing import (
	NON_STOCK_TARGETS, SOURCE_KEYWORD, SOURCE_MANUAL, SOURCE_RULE, SOURCE_SUPPLIER, TARGET_STOCK,
	detect_carrier, resolve_target,
)

WINDOW = timedelta(days=30)  # NAV allows 35; stay clear of clock-skew edge cases


def run_sync(manual: bool = False) -> dict:
	settings = frappe.get_single("NAV Connection Settings")
	if not settings.enabled and not manual:
		return {"skipped": "disabled"}

	client = settings.get_client()
	now = datetime.now(timezone.utc)
	if settings.last_sync_insdate:
		date_from = get_datetime(settings.last_sync_insdate).replace(tzinfo=timezone.utc)
	elif settings.sync_from_date:
		date_from = get_datetime(settings.sync_from_date).replace(tzinfo=timezone.utc)
	else:
		date_from = now - timedelta(days=7)

	digests = []
	try:
		window_from = date_from
		while window_from < now:
			window_to = min(window_from + WINDOW, now)
			page = 1
			while True:
				result = client.query_invoice_digest(window_from, window_to, "INBOUND", page)
				digests.extend(result["invoices"])
				if page >= result["available_page_count"]:
					break
				page += 1
			window_from = window_to
	except Exception:
		frappe.log_error(title="NAV sync: queryInvoiceDigest failed", message=frappe.get_traceback())
		if manual:
			raise
		return {"error": "digest"}

	created, skipped, failed = 0, 0, 0
	for digest in digests:
		number = digest.get("invoiceNumber")
		supplier_tax = (digest.get("supplierTaxNumber") or "")[:8]
		if not number:
			continue
		if frappe.db.exists("NAV Inbound Invoice Import", {"nav_invoice_number": number, "supplier_tax_number": supplier_tax}):
			skipped += 1
			continue
		try:
			import_invoice(client, settings, digest)
			created += 1
		except Exception:
			failed += 1
			frappe.log_error(title=f"NAV sync: failed importing {number}", message=frappe.get_traceback())

	settings.db_set("last_sync_insdate", now.strftime("%Y-%m-%d %H:%M:%S"))
	frappe.db.commit()
	return {"found": len(digests), "created": created, "skipped": skipped, "failed": failed}


def import_invoice(client, settings, digest: dict):
	number = digest["invoiceNumber"]
	supplier_tax = (digest.get("supplierTaxNumber") or "")[:8]

	doc = frappe.new_doc("NAV Inbound Invoice Import")
	doc.nav_invoice_number = number
	doc.supplier_tax_number = supplier_tax
	doc.supplier_name = digest.get("supplierName")
	doc.ins_date = _nav_datetime(digest.get("insDate"))
	doc.invoice_category = digest.get("invoiceCategory")
	doc.status = "Új"

	try:
		data = client.query_invoice_data(number, "INBOUND", supplier_tax_number=supplier_tax)
		if data is None:
			doc.status = "Hiba"
			doc.error_message = "A NAV a kivonatban visszaadta, de a részletes lekérdezés üres választ adott (queryInvoiceData)."
			doc.insert(ignore_permissions=True)
			return doc

		parsed = parse_invoice_xml(data["invoice_xml"])
		fill_header(doc, parsed, digest)
		doc.invoice_json = json.dumps(parsed, indent=1, ensure_ascii=False)
		apply_matching(doc, parsed)
		doc.insert(ignore_permissions=True)
		file_doc = save_file(fname=f"NAV_{_safe_filename(number)}.xml", content=data["invoice_xml"], dt=doc.doctype, dn=doc.name, is_private=1)
		doc.db_set("raw_xml", file_doc.file_url)
		_notify_if_needed(doc, settings)
		return doc
	except Exception:
		doc.status = "Hiba"
		doc.error_message = frappe.get_traceback()
		if doc.is_new():
			doc.insert(ignore_permissions=True)
		else:
			doc.save(ignore_permissions=True)
		raise


def fill_header(doc, parsed: dict, digest: dict | None = None):
	sup = parsed.get("supplier") or {}
	summary = parsed.get("summary") or {}
	doc.supplier_name = sup.get("name") or doc.supplier_name
	doc.supplier_tax_number = (sup.get("tax_number") or doc.supplier_tax_number or "")[:8]
	doc.invoice_issue_date = parsed.get("issue_date")
	doc.delivery_date = parsed.get("delivery_date")
	doc.currency = parsed.get("currency_code") or "HUF"
	doc.exchange_rate = parsed.get("exchange_rate") or 1.0
	doc.net_total = summary.get("net")
	doc.vat_total = summary.get("vat")
	doc.grand_total = summary.get("gross") or ((summary.get("net") or 0) + (summary.get("vat") or 0))
	doc.invoice_category = parsed.get("invoice_category") or doc.invoice_category
	ref = parsed.get("reference")
	op = (digest or {}).get("invoiceOperation")
	doc.is_modification = int(bool(ref) or op in ("MODIFY", "STORNO") or parsed.get("is_batch"))
	doc.original_invoice_number = (ref or {}).get("original_invoice_number")
	doc.supplier = doc.supplier or find_supplier(sup.get("tax_number"), sup.get("community_vat_number"))


def apply_matching(doc, parsed: dict | None = None):
	"""(Re)build the lines table from the stored JSON; keeps manual item picks the user already made."""
	parsed = parsed or json.loads(doc.invoice_json or "{}")
	settings = frappe.get_single("NAV Connection Settings")
	if not doc.supplier:
		doc.supplier = find_supplier((parsed.get("supplier") or {}).get("tax_number"), (parsed.get("supplier") or {}).get("community_vat_number"))

	manual = {row.line_number: row.item_code for row in doc.lines if row.item_code and row.match_method == "manual"}
	manual_targets = {
		row.line_number: {"target": row.target, "expense_category": row.expense_category}
		for row in doc.lines if row.get("target_source") == SOURCE_MANUAL and row.get("target")
	}
	skipped = {row.line_number for row in doc.lines if row.match_status == "Kihagy" and row.match_method not in ("advance", "negative")}
	doc.set("lines", [])

	ctx = MatchContext(doc.supplier, settings)
	routing_ctx = _routing_context(doc.supplier)
	item_default_wh = _item_default_warehouses(settings.company)
	for line in parsed.get("lines", []):
		decision = resolve_target(
			line,
			rules=routing_ctx["rules"],
			categories=routing_ctx["categories"],
			supplier_default_target=routing_ctx["default_target"],
			supplier_default_category=routing_ctx["default_category"],
			manual=manual_targets.get(line.get("line_number")),
		)
		if decision["target"] in NON_STOCK_TARGETS and not line.get("is_advance"):
			row = _non_stock_row(line, decision, routing_ctx)
		else:
			row = match_line(line, ctx)
			row.update(target=TARGET_STOCK, target_source=decision["target_source"])
		row["vat_label"] = vat_label(line.get("vat"))
		if row["line_number"] in manual and row["target"] == TARGET_STOCK:
			row.update(item_code=manual[row["line_number"]], match_status="Egyeztetve", match_method="manual", match_confidence=100)
		if row["line_number"] in skipped:
			row["match_status"] = "Kihagy"
		if row.get("item_code"):
			row["warehouse"] = item_default_wh.get(row["item_code"]) or settings.default_warehouse
		doc.append("lines", row)

	if not doc.supplier:
		doc.error_message = (
			f"Ismeretlen szállító (adószám {doc.supplier_tax_number}, név: {doc.supplier_name}). "
			f"Hozd létre a Szállítót ezzel az adószámmal a Tax ID mezőben, majd 'Egyeztetés újra'."
		)
	elif doc.status == "Hiba" or (doc.error_message or "").startswith("Ismeretlen szállító"):
		doc.error_message = None
	doc.status = doc.derive_status() if hasattr(doc, "derive_status") else doc.status


def _routing_context(supplier) -> dict:
	"""Everything resolve_target needs, loaded once per invoice."""
	categories = frappe.get_all(
		"NAV Expense Category",
		fields=["name", "target", "expense_account", "negative_account", "keywords", "group_by_carrier"],
	)
	rules = []
	default_target = default_category = None
	if supplier:
		rules = frappe.get_all(
			"NAV Item Mapping Rule",
			filters={"supplier": supplier},
			fields=["supplier_code", "description_pattern", "item", "target", "expense_category"],
		)
		# a stock rule without explicit target is still a stock rule
		for r in rules:
			r["target"] = r.get("target") or (TARGET_STOCK if r.get("item") else None)
		if frappe.get_meta("Supplier").has_field("nav_default_target"):
			default_target, default_category = frappe.db.get_value("Supplier", supplier, ["nav_default_target", "nav_expense_category"]) or (None, None)
	return {
		"categories": categories,
		"category_by_name": {c.name: c for c in categories},
		"rules": rules,
		"default_target": default_target or None,
		"default_category": default_category or None,
	}


def _non_stock_row(line: dict, decision: dict, routing_ctx: dict) -> dict:
	row = base_row(line)
	cat_name = decision.get("expense_category")
	cat = routing_ctx["category_by_name"].get(cat_name) if cat_name else None
	row.update(
		target=decision["target"],
		expense_category=cat_name,
		target_source=decision["target_source"],
		group_label=detect_carrier(row["nav_description"]) if cat and cat.group_by_carrier else None,
		match_method="category",
		match_status=STATUS_MATCHED if cat_name else STATUS_NEEDS_CATEGORY,
		match_confidence=100 if cat_name else 0,
	)
	why = {
		SOURCE_MANUAL: "Kézzel beállítva",
		SOURCE_RULE: "Tanult szabály",
		SOURCE_KEYWORD: "Kulcsszó a megnevezésben",
		SOURCE_SUPPLIER: "A szállító alapértelmezése",
	}.get(decision["target_source"], decision["target_source"])
	note = f"{decision['target']} — {why}."
	if not cat_name:
		note += " Válassz költségkategóriát."
	elif cat and not cat.expense_account:
		note += f" A '{cat_name}' kategóriához még nincs főkönyvi számla (könyvelő adja meg)."
	if row["net_amount"] < 0:
		note += " Negatív sor (engedmény/kerekítés) — a kategóriájával együtt csökkenti az összeget."
	row["match_note"] = note
	return row


def _item_default_warehouses(company) -> dict:
	rows = frappe.get_all("Item Default", filters={"company": company, "default_warehouse": ["is", "set"]}, fields=["parent", "default_warehouse"])
	return {r.parent: r.default_warehouse for r in rows}


def _notify_if_needed(doc, settings):
	if settings.notify_email and doc.status in ("Egyeztetésre vár", "Hiba", "Módosító számla"):
		try:
			frappe.sendmail(
				recipients=[settings.notify_email],
				subject=f"NAV számla egyeztetésre vár: {doc.supplier_name} {doc.nav_invoice_number}",
				message=f"Állapot: {doc.status}<br>{frappe.utils.get_link_to_form(doc.doctype, doc.name)}<br><pre>{doc.error_message or ''}</pre>",
			)
		except Exception:
			frappe.log_error(title="NAV sync: notification failed", message=frappe.get_traceback())


def _nav_datetime(value):
	"""'2026-09-10T10:00:00.123Z' -> '2026-09-10 10:00:00' (Frappe Datetime format)."""
	if not value:
		return None
	try:
		return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
	except ValueError:
		return None


def _safe_filename(number: str) -> str:
	return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in number)
