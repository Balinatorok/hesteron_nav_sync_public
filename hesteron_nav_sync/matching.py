"""
Item / supplier / purchase-order matching for NAV inbound invoice lines.

Goal: find the company's own items for the NAV lines, so receiving takes as few clicks as
possible — so the engine tries several keys, in order of reliability, and
attaches a confidence + explanation to every line. The UI then only asks a human about the lines
where the machine is not sure.

Tiers (first hit wins; confidence in parentheses):
  100  rule          NAV Item Mapping Rule learned from an earlier confirmed receipt (supplier + code
                     or exact description)
   95  supplier_code Item Supplier.supplier_part_no == NAV product code (OWN/OTHER category) or a
                     code token found in the description
   95  barcode       Item Barcode.barcode == NAV product code / EAN-looking token in the description
   90  po_line       an open Purchase Order line of this supplier with the same item (or the only open
                     line whose qty+rate equals the NAV line) — the receipt is then created AGAINST the PO
   85  item_code     an existing item_code appears verbatim in the description (short codes
                     like AACBF / BGWH0, or long numeric codes like 1205050215)
   60-84 name_fuzzy  difflib similarity of the description to item names, supplier's past items first
    0  none          -> "Új cikk?" with a suggested Item Group / Brand / UOM for one-click creation

Confidence >= 90 -> "Egyeztetve" (no click needed), 60-89 -> "Javaslat" (one click to confirm),
< 60 -> "Új cikk" (create or pick). Anything with lineNatureIndicator=SERVICE, advance/discount
lines and negative quantities are never auto-received into stock.
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

import frappe
from frappe.utils import flt

from hesteron_nav_sync.mapping import resolve_uom

AUTO_ACCEPT = 90
SUGGEST = 60

STATUS_MATCHED = "Egyeztetve"
STATUS_SUGGESTED = "Javaslat"
STATUS_NEW = "Új cikk"
STATUS_SKIP = "Kihagy"
STATUS_NEEDS_CATEGORY = "Kategória kell"  # non-stock line (cost) whose expense category is not chosen yet


# ---------------------------------------------------------------------------- supplier

def _digits(value: str | None) -> str:
	return re.sub(r"\D", "", value or "")


def find_supplier(taxpayer_id: str | None, community_vat_number: str | None = None) -> str | None:
	"""Match on the 8-digit törzsszám, tolerant of how tax_id was typed (12345678-2-41, HU12345678, ...)."""
	if not taxpayer_id:
		return None
	taxpayer_id = _digits(taxpayer_id)[:8]
	for row in frappe.get_all("Supplier", filters={"disabled": 0}, fields=["name", "tax_id"]):
		if _digits(row.tax_id)[:8] == taxpayer_id and _digits(row.tax_id):
			return row.name
	if community_vat_number:
		hit = frappe.db.get_value("Supplier", {"tax_id": community_vat_number, "disabled": 0}, "name")
		if hit:
			return hit
	return None


# ---------------------------------------------------------------------------- context per invoice

class MatchContext:
	"""Loads the masters once per invoice so per-line matching is cheap."""

	def __init__(self, supplier: str | None, settings):
		self.supplier = supplier
		self.settings = settings
		self.item_meta_has_helykod = frappe.get_meta("Item").has_field("custom_helykód")
		fields = ["name", "item_name", "description", "item_group", "brand", "stock_uom", "has_serial_no", "has_batch_no", "is_stock_item", "customs_tariff_number"]
		if self.item_meta_has_helykod:
			fields.append("custom_helykód as helykod")
		self.items = {r.name: r for r in frappe.get_all("Item", filters={"disabled": 0}, fields=fields)}
		self.item_codes_by_len = sorted(self.items.keys(), key=len, reverse=True)

		# only stock rules (with an item) take part in item matching; target/category rules are read by routing
		self.rules = [r for r in frappe.get_all("NAV Item Mapping Rule", filters={"supplier": supplier}, fields=["supplier_code", "description_pattern", "item"]) if r.item] if supplier else []
		self.supplier_part_nos = {}
		if supplier:
			for r in frappe.get_all("Item Supplier", filters={"supplier": supplier}, fields=["parent", "supplier_part_no"]):
				if r.supplier_part_no:
					self.supplier_part_nos[r.supplier_part_no.strip().lower()] = r.parent
		self.barcodes = {r.barcode.strip(): r.parent for r in frappe.get_all("Item Barcode", fields=["parent", "barcode"]) if r.barcode}
		# brand names, longest first, so "HP Indigo" wins over "HP"
		self.brands = sorted((b for b in frappe.get_all("Brand", pluck="name") if b and len(b.strip()) >= 2), key=len, reverse=True)

		self.open_po_lines = self._load_open_po_lines(supplier) if supplier else []
		self.supplier_history = self._load_supplier_history(supplier) if supplier else Counter()

	def _load_open_po_lines(self, supplier):
		pos = frappe.get_all("Purchase Order", filters={"supplier": supplier, "docstatus": 1, "per_received": ["<", 100]}, pluck="name")
		if not pos:
			return []
		return frappe.get_all(
			"Purchase Order Item",
			filters={"parent": ["in", pos], "docstatus": 1},
			fields=["name", "parent", "item_code", "item_name", "qty", "received_qty", "rate", "uom"],
		)

	def _load_supplier_history(self, supplier):
		"""Items previously received/invoiced from this supplier, most frequent first."""
		counter = Counter()
		for dt in ("Purchase Receipt Item", "Purchase Invoice Item"):
			parent_dt = dt.replace(" Item", "")
			rows = frappe.db.sql(
				f"""select i.item_code, count(*) c from `tab{dt}` i join `tab{parent_dt}` p on p.name = i.parent
				    where p.supplier = %s and p.docstatus = 1 group by i.item_code""",
				(supplier,),
				as_dict=True,
			)
			for r in rows:
				counter[r.item_code] += r.c
		for code in self.supplier_part_nos.values():
			counter[code] += 1
		return counter

	def item(self, code):
		return self.items.get(code)


# ---------------------------------------------------------------------------- per-line matching

def base_row(line: dict) -> dict:
	"""The NAV facts of a line, common to stock and non-stock rows."""
	codes = [c for c in (line.get("own_codes") or []) if c]
	return {
		"line_number": line.get("line_number"),
		"nav_description": (line.get("description") or "").strip(),
		"nav_product_code": ", ".join(codes) if codes else None,
		"nav_vtsz": line.get("vtsz"),
		"nature": line.get("nature"),
		"qty": flt(line.get("quantity")),
		"nav_uom": line.get("uom_own") if line.get("uom") == "OWN" else line.get("uom"),
		"rate": flt(line.get("unit_price")),
		"net_amount": flt(line.get("net_amount")),
		"vat_label": None,  # filled by caller via mapping.vat_label
		"item_code": None,
		"match_status": STATUS_NEW,
		"match_method": "none",
		"match_confidence": 0,
	}


def match_line(line: dict, ctx: MatchContext) -> dict:
	"""
	Returns a dict for a `NAV Inbound Invoice Import Line` row (stock target). Never raises on a bad
	line — the row just ends up as "Új cikk" with an explanation.
	"""
	desc = (line.get("description") or "").strip()
	qty = flt(line.get("quantity"))
	codes = [c for c in (line.get("own_codes") or []) if c]
	notes = []

	row = base_row(line)

	# lines that must never become stock receipts
	if line.get("is_advance"):
		row.update(match_status=STATUS_SKIP, match_method="advance", match_note="Előlegsor — nem bevételezhető.")
		return row
	if line.get("nature") == "SERVICE":
		notes.append("Szolgáltatás-sor (lineNatureIndicator=SERVICE) — nem készletcikk; számlán költségként rögzítendő.")
	if qty < 0 or flt(line.get("unit_price")) < 0 or flt(line.get("net_amount")) < 0:
		row.update(match_status=STATUS_SKIP, match_method="negative", match_note="Negatív mennyiség/ár (engedmény- vagy helyesbítő sor) — kézi döntést igényel.")
		return row
	if line.get("modification"):
		notes.append(f"Módosító sor ({line['modification'].get('operation')}, eredeti sor: {line['modification'].get('line_number_reference')}).")

	item_code, method, confidence, why = _match_item(desc, codes, line, ctx)
	po = _match_po_line(item_code, desc, qty, flt(line.get("unit_price")), ctx)
	if po and not item_code:
		item_code, method, confidence, why = po["item_code"], "po_line", 90, f"Nyitott beszerzési rendelés sora: {po['parent']}"
	elif po:
		confidence = max(confidence, 90)
		why += f" · nyitott PO: {po['parent']}"

	if item_code:
		item = ctx.item(item_code)
		row.update(
			item_code=item_code,
			match_method=method,
			match_confidence=confidence,
			match_status=STATUS_MATCHED if confidence >= AUTO_ACCEPT else STATUS_SUGGESTED,
			uom=resolve_uom(line.get("uom"), line.get("uom_own"), item.stock_uom if item else None),
			has_serial_no=int(bool(item and item.has_serial_no)),
			has_batch_no=int(bool(item and item.has_batch_no)),
			is_stock_item=int(bool(item and item.is_stock_item)),
			helykod=(item.get("helykod") if item else None),
			purchase_order=po["parent"] if po else None,
			purchase_order_item=po["name"] if po else None,
		)
		if item and item.has_serial_no:
			notes.append(f"Sorozatszámos cikk — bevételezéskor {int(qty)} db sorozatszámot kell beolvasni/rögzíteni.")
		if item and item.has_batch_no:
			notes.append("Sarzsos cikk — sarzsszám kell a bevételezéshez.")
		if item and item.get("helykod"):
			notes.append(f"Helykód: {item['helykod']}")
		if line.get("nature") == "SERVICE" and item and item.is_stock_item:
			notes.append("FIGYELEM: a NAV szolgáltatásként jelöli, de készletcikkhez egyezett — ellenőrizd.")
		notes.insert(0, why)
	else:
		row.update(uom=resolve_uom(line.get("uom"), line.get("uom_own"), None))
		suggestion = _suggest_for_new_item(line, ctx)
		row.update(suggestion)
		notes.append(
			"Nincs egyező cikk. Javasolt rögzítés: "
			+ ", ".join(f"{k}: {v}" for k, v in (("cikkcsoport", suggestion.get("suggested_item_group")), ("márka", suggestion.get("suggested_brand")), ("ME", row.get("uom"))) if v)
		)
		alternatives = _fuzzy_candidates(desc, ctx, limit=3)
		if alternatives:
			notes.append("Hasonló meglévő cikkek: " + "; ".join(f"{code} ({int(score * 100)}%)" for code, score in alternatives))

	row["match_note"] = "\n".join(n for n in notes if n)
	return row


def _match_item(desc: str, codes: list[str], line: dict, ctx: MatchContext):
	"""Returns (item_code, method, confidence, explanation)."""
	# 1. learned rules
	for rule in ctx.rules:
		if rule.supplier_code and codes and rule.supplier_code.strip().lower() in [c.lower() for c in codes]:
			return rule.item, "rule", 100, f"Tanult szabály (szállítói kód {rule.supplier_code})"
		if rule.description_pattern and desc and rule.description_pattern.strip().lower() == desc.lower():
			return rule.item, "rule", 100, "Tanult szabály (megnevezés)"
	for rule in ctx.rules:  # contains-match as a weaker rule form
		if rule.description_pattern and desc and rule.description_pattern.strip().lower() in desc.lower():
			return rule.item, "rule", 95, f"Tanult szabály (megnevezés tartalmazza: '{rule.description_pattern}')"

	# 2. supplier part number / barcode from the NAV product codes
	for code in codes:
		key = code.strip().lower()
		if key in ctx.supplier_part_nos:
			return ctx.supplier_part_nos[key], "supplier_code", 95, f"Szállítói cikkszám egyezik ({code})"
		if code.strip() in ctx.barcodes:
			return ctx.barcodes[code.strip()], "barcode", 95, f"Vonalkód egyezik ({code})"
		if code.strip() in ctx.items:
			return code.strip(), "item_code", 95, f"A NAV-kód a saját cikkszámunk ({code})"

	# 3. codes hiding in the free-text description
	tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-_/\.]{2,}", desc)
	for tok in tokens:
		key = tok.strip(".,;:").lower()
		if key in ctx.supplier_part_nos:
			return ctx.supplier_part_nos[key], "supplier_code", 95, f"Szállítói cikkszám a megnevezésben ({tok})"
		if key.isdigit() and len(key) in (8, 12, 13, 14) and key in ctx.barcodes:
			return ctx.barcodes[key], "barcode", 95, f"EAN a megnevezésben ({tok})"
	upper_desc = desc.upper()
	for code in ctx.item_codes_by_len:
		if len(code) >= 4 and re.search(rf"(?<![A-Z0-9]){re.escape(code.upper())}(?![A-Z0-9])", upper_desc):
			return code, "item_code", 85, f"Cikkszám a megnevezésben ({code})"

	# 4. fuzzy name match
	for code, score in _fuzzy_candidates(desc, ctx, limit=1):
		if score >= 0.60:
			conf = int(60 + (score - 0.60) / 0.40 * 29)  # 0.60 -> 60, 1.0 -> 89
			return code, "name_fuzzy", conf, f"Hasonló megnevezés: {ctx.item(code).item_name} ({int(score * 100)}%)"
	return None, "none", 0, ""


def _normalise(text: str) -> str:
	text = (text or "").lower()
	text = re.sub(r"[^a-z0-9áéíóöőúüű ]+", " ", text)
	return re.sub(r"\s+", " ", text).strip()


def _fuzzy_candidates(desc: str, ctx: MatchContext, limit: int = 3) -> list[tuple[str, float]]:
	if not desc:
		return []
	needle = _normalise(desc)
	scored = []
	for code, item in ctx.items.items():
		best = 0.0
		for field in (item.item_name, item.description):
			if field:
				best = max(best, SequenceMatcher(None, needle, _normalise(field)).ratio())
		if best > 0.4:
			# nudge items this supplier has sold us before
			best += 0.05 if ctx.supplier_history.get(code) else 0
			scored.append((code, min(best, 1.0)))
	scored.sort(key=lambda t: t[1], reverse=True)
	return scored[:limit]


def _match_po_line(item_code, desc, qty, rate, ctx: MatchContext):
	open_lines = [l for l in ctx.open_po_lines if flt(l.qty) - flt(l.received_qty) > 0]
	if not open_lines:
		return None
	if item_code:
		same = [l for l in open_lines if l.item_code == item_code]
		if same:
			# prefer the one whose remaining qty matches
			same.sort(key=lambda l: abs((flt(l.qty) - flt(l.received_qty)) - qty))
			return same[0]
		return None
	# no item yet: a single open line with identical qty & rate is a strong hint
	exact = [l for l in open_lines if abs((flt(l.qty) - flt(l.received_qty)) - qty) < 1e-6 and abs(flt(l.rate) - rate) < 0.01]
	return exact[0] if len(exact) == 1 else None


def brand_in_text(text: str | None, brands) -> str | None:
	"""The brand whose name appears as a whole word in the NAV description (e.g. 'KODAK ...' -> Kodak)."""
	if not text:
		return None
	upper = text.upper()
	hits = []
	for brand in brands:
		m = re.search(rf"(?<![A-Z0-9]){re.escape(brand.strip().upper())}(?![A-Z0-9])", upper)
		if m:
			hits.append((m.start(), -len(brand), brand))
	return min(hits)[2] if hits else None


def _suggest_for_new_item(line: dict, ctx: MatchContext) -> dict:
	"""
	Where should a brand-new item go? Item group: same VTSZ neighbours first, then the supplier's habits.
	Brand: ONLY if the brand name is written in the NAV description — a guess from neighbours/supplier
	history gave wrong brands (a Kodak line became Epson), so no guessing.
	"""
	groups = Counter()
	if line.get("vtsz"):
		for item in ctx.items.values():
			if item.customs_tariff_number and str(item.customs_tariff_number).startswith(line["vtsz"][:4]):
				groups[item.item_group] += 2
	for code, count in ctx.supplier_history.items():
		item = ctx.item(code)
		if item:
			groups[item.item_group] += count
	suggested_group = groups.most_common(1)[0][0] if groups else None
	suggested_brand = brand_in_text(line.get("description"), getattr(ctx, "brands", []))
	if line.get("nature") == "SERVICE":
		suggested_group = _first_existing_group(["Szolgáltatások", "Services", suggested_group])
	return {"suggested_item_group": suggested_group, "suggested_brand": suggested_brand}


def _first_existing_group(candidates):
	for c in candidates:
		if c and frappe.db.exists("Item Group", c):
			return c
	return None
