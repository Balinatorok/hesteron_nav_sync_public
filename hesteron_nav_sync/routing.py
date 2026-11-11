"""
Where does a NAV invoice line go? — "Cél" (target) routing, frappe-free so it can be unit tested.

Background (from two months of real NAV data): most inbound invoices are NOT stock goods. A large
share are pure costs (bookkeeping, courier, travel, EPR fee, software ...), and a single courier
supplier can produce most of the lines. Forcing an Item
for "Számviteli szolgáltatás 2026 09 hó" or for every DPD tariff line is wrong.

So every line gets a target:

    Készletre           -> Purchase Receipt (needs an Item; the existing matching engine)
    Költség             -> straight onto the Purchase Invoice, no Item, expense account of its category
    Saját felhasználás  -> same as Költség, separate label so it can be reported / reversed
    Beszerzési járulék  -> onto the Purchase Invoice with its category account; the note tells the
                           user to spread it on the receipt with a Landed Cost Voucher

...and, for non-stock targets, an expense category (NAV Expense Category: label -> GL account,
filled in by the accountant).

Precedence (first hit wins):
    1. a manual choice already made on this row (kept across "Egyeztetés újra")
    2. a learned rule (supplier + NAV code, or supplier + exact description) carrying a target
    3. a category keyword found in the description (e.g. "kerekítés" -> Kerekítés)
    4. the supplier's default (Supplier card, "NAV import" section)
    5. NAV lineNatureIndicator = SERVICE -> Költség (category still to choose)
    6. Készletre

Any supplier may deviate on any line — the default is only a starting point.
"""

from __future__ import annotations

import re

TARGET_STOCK = "Készletre"
TARGET_EXPENSE = "Költség"
TARGET_OWN_USE = "Saját felhasználás"
TARGET_LANDED = "Beszerzési járulék"
NON_STOCK_TARGETS = (TARGET_EXPENSE, TARGET_OWN_USE, TARGET_LANDED)
ALL_TARGETS = (TARGET_STOCK,) + NON_STOCK_TARGETS

SOURCE_MANUAL = "kézi"
SOURCE_RULE = "tanult szabály"
SOURCE_KEYWORD = "kulcsszó"
SOURCE_SUPPLIER = "szállító alapértelmezés"
SOURCE_NAV_SERVICE = "NAV: szolgáltatás"
SOURCE_DEFAULT = "alapértelmezés"

# Order matters: the first carrier whose pattern appears in the description wins.
CARRIERS = (
	("GLS", r"\bGLS\b"),
	("DPD", r"\bDPD\b"),
	("Foxpost", r"\bFOXPOST\b"),
	("Packeta", r"\bPACKETA\b|\bZASILKOVNA\b"),
	("Express One", r"\bEXPRESS\s*ONE\b"),
	("Sprinter", r"\bSPRINTER\b"),
	("UPS", r"\bUPS\b"),
	("DHL", r"\bDHL\b"),
	("FedEx", r"\bFEDEX\b"),
	# Magyar Posta last: "posta" also appears inside "Postán maradó" which IS MPL, fine
	("Posta (MPL)", r"\bMPL\b|\bMAGYAR\s+POSTA\b|\bPOSTA\b|\bPOSTÁN\b"),
)
OTHER_CARRIER = "Egyéb"


def detect_carrier(description: str | None) -> str:
	text = (description or "").upper()
	for label, pattern in CARRIERS:
		if re.search(pattern, text):
			return label
	return OTHER_CARRIER


def _norm(text: str | None) -> str:
	return re.sub(r"\s+", " ", (text or "").strip().lower())


def keyword_category(description: str | None, categories: list[dict]) -> str | None:
	"""A category whose keyword list (comma separated) appears in the description."""
	desc = _norm(description)
	if not desc:
		return None
	for cat in categories:
		for kw in (cat.get("keywords") or "").split(","):
			kw = _norm(kw)
			if kw and kw in desc:
				return cat["name"]
	return None


def rule_target(line: dict, rules: list[dict]) -> tuple[str | None, str | None, str | None]:
	"""(target, category, item) from a learned rule, exact code first, then exact description."""
	codes = [c.strip().lower() for c in (line.get("own_codes") or []) if c]
	desc = _norm(line.get("description"))
	for rule in rules:
		if rule.get("supplier_code") and rule["supplier_code"].strip().lower() in codes:
			return rule.get("target"), rule.get("expense_category"), rule.get("item")
	for rule in rules:
		if rule.get("description_pattern") and _norm(rule["description_pattern"]) == desc:
			return rule.get("target"), rule.get("expense_category"), rule.get("item")
	return None, None, None


def resolve_target(
	line: dict,
	*,
	rules: list[dict] | None = None,
	categories: list[dict] | None = None,
	supplier_default_target: str | None = None,
	supplier_default_category: str | None = None,
	manual: dict | None = None,
) -> dict:
	"""
	Decide target + category for one parsed NAV line.

	Returns {"target", "expense_category", "target_source", "item_hint"} where item_hint is an item
	from a learned stock rule (the matching engine would find it anyway; passed for completeness).
	"""
	categories = categories or []
	cat_by_name = {c["name"]: c for c in categories}

	def category_target(cat_name, fallback):
		cat = cat_by_name.get(cat_name)
		return (cat.get("target") if cat and cat.get("target") in NON_STOCK_TARGETS else None) or fallback

	if manual and manual.get("target"):
		return {"target": manual["target"], "expense_category": manual.get("expense_category"), "target_source": SOURCE_MANUAL, "item_hint": None}

	target, category, item = rule_target(line, rules or [])
	if target or category:
		if category and not target:
			target = category_target(category, TARGET_EXPENSE)
		return {"target": target, "expense_category": category if target in NON_STOCK_TARGETS else None, "target_source": SOURCE_RULE, "item_hint": item}

	kw_cat = keyword_category(line.get("description"), categories)
	if kw_cat:
		return {"target": category_target(kw_cat, TARGET_EXPENSE), "expense_category": kw_cat, "target_source": SOURCE_KEYWORD, "item_hint": None}

	if supplier_default_target in NON_STOCK_TARGETS:
		return {"target": supplier_default_target, "expense_category": supplier_default_category, "target_source": SOURCE_SUPPLIER, "item_hint": None}
	if supplier_default_target == TARGET_STOCK:
		return {"target": TARGET_STOCK, "expense_category": None, "target_source": SOURCE_SUPPLIER, "item_hint": None}

	if line.get("nature") == "SERVICE":
		return {"target": TARGET_EXPENSE, "expense_category": supplier_default_category, "target_source": SOURCE_NAV_SERVICE, "item_hint": None}

	return {"target": TARGET_STOCK, "expense_category": None, "target_source": SOURCE_DEFAULT, "item_hint": None}


def aggregate_expense_rows(rows: list[dict], categories: list[dict]) -> tuple[list[dict], list[str]]:
	"""
	Collapse non-stock import rows into Purchase Invoice rows: one per (category, target, carrier
	group, sign-dependent account). Returns (pi_rows, problems).

	Each input row: {"line_number", "target", "expense_category", "group_label", "net_amount",
	                 "nav_description"}.
	Each output row: {"item_name", "description", "amount", "expense_account", "line_numbers"}.
	"""
	cat_by_name = {c["name"]: c for c in categories}
	buckets: dict[tuple, dict] = {}
	order: list[tuple] = []
	problems: list[str] = []

	for r in rows:
		cat_name = r.get("expense_category")
		if not cat_name:
			problems.append(f"#{r.get('line_number')} {(r.get('nav_description') or '')[:40]} — nincs költségkategória")
			continue
		cat = cat_by_name.get(cat_name) or {}
		amount = float(r.get("net_amount") or 0)
		account = cat.get("negative_account") if amount < 0 and cat.get("negative_account") else cat.get("expense_account")
		if not account:
			problems.append(f"'{cat_name}' kategóriához nincs főkönyvi számla megadva")
			continue
		group = r.get("group_label") if cat.get("group_by_carrier") else None
		key = (cat_name, r.get("target"), group, account)
		if key not in buckets:
			label = cat_name + (f" — {group}" if group else "")
			if r.get("target") == TARGET_OWN_USE:
				label += " (saját felhasználás)"
			buckets[key] = {"item_name": label, "descriptions": [], "amount": 0.0, "expense_account": account, "line_numbers": [], "target": r.get("target")}
			order.append(key)
		b = buckets[key]
		b["amount"] += amount
		b["line_numbers"].append(r.get("line_number"))
		desc = (r.get("nav_description") or "").strip()
		if desc and desc not in b["descriptions"]:
			b["descriptions"].append(desc)

	out = []
	for key in order:
		b = buckets[key]
		shown = b["descriptions"][:12]
		more = len(b["descriptions"]) - len(shown)
		description = "\n".join(shown) + (f"\n… és még {more} tétel" if more > 0 else "")
		if b["target"] == TARGET_LANDED:
			description += "\n[Beszerzési járulék — Landed Cost Voucherrel oszd rá a bevételezésre]"
		out.append({
			"item_name": b["item_name"][:140],
			"description": description,
			"amount": round(b["amount"], 2),
			"expense_account": b["expense_account"],
			"line_numbers": b["line_numbers"],
		})
	# de-duplicate the "no account" message per category
	seen, uniq = set(), []
	for p in problems:
		if p not in seen:
			seen.add(p)
			uniq.append(p)
	return out, uniq
