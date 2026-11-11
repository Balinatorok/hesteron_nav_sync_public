"""Target routing + expense aggregation. Descriptions are real lines from the 2026-07/09 NAV data."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from hesteron_nav_sync.routing import (  # noqa: E402
	SOURCE_KEYWORD, SOURCE_MANUAL, SOURCE_NAV_SERVICE, SOURCE_RULE, SOURCE_SUPPLIER,
	TARGET_EXPENSE, TARGET_LANDED, TARGET_OWN_USE, TARGET_STOCK,
	aggregate_expense_rows, detect_carrier, resolve_target,
)

CATS = [
	{"name": "Szállítási költség", "target": "Költség", "expense_account": "5291 - Szállítás", "group_by_carrier": 1},
	{"name": "Könyvelés", "target": "Költség", "expense_account": "5294 - Könyvelés"},
	{"name": "Kerekítés", "target": "Költség", "expense_account": "8699 - Kerekítés", "negative_account": "9699 - Kerekítés", "keywords": "kerekítés, kerekites"},
	{"name": "Irodaszer", "target": "Saját felhasználás", "expense_account": "5120 - Irodaszer"},
	{"name": "Fuvar (beszerzés)", "target": "Beszerzési járulék", "expense_account": "5299 - Fuvar"},
	{"name": "EPR-díj", "target": "Költség", "expense_account": None},
]


def test_carriers_from_real_courier_lines():
	cases = {
		"DPD házhozszállítás 10kg-ig Magyarország": "DPD",
		"MPL Postán maradó utánvét kezelés 50e Ft-ig": "Posta (MPL)",
		"MPL Csomagautomata csomagpont/automata 1kg-ig": "Posta (MPL)",
		"Foxpost Z-Pont csomagpont/automata 1kg-ig": "Foxpost",
		"GLS házhozszállítás 10kg-ig Magyarország": "GLS",
		"Foxpost Magyarország visszáru": "Foxpost",
		"DPD utánvét százalékos költség 1.8% 20e Ft-ig": "DPD",
		"Rugalmas csomag - Utánvét Ellenőr (Tier 2)": "Egyéb",
	}
	for desc, expected in cases.items():
		assert detect_carrier(desc) == expected, desc


def test_supplier_default_expense_wins_over_stock_default():
	r = resolve_target({"description": "Számviteli szolgáltatás 2026 09 hó"}, categories=CATS,
	                   supplier_default_target=TARGET_EXPENSE, supplier_default_category="Könyvelés")
	assert (r["target"], r["expense_category"], r["target_source"]) == (TARGET_EXPENSE, "Könyvelés", SOURCE_SUPPLIER)


def test_keyword_beats_supplier_default_rounding_line_on_stock_invoice():
	# stock supplier: Epson tinták + a "kerekítés" line — the supplier is a stock supplier, the line is not
	r = resolve_target({"description": "kerekítés"}, categories=CATS, supplier_default_target=TARGET_STOCK)
	assert (r["target"], r["expense_category"], r["target_source"]) == (TARGET_EXPENSE, "Kerekítés", SOURCE_KEYWORD)


def test_manual_choice_survives():
	r = resolve_target({"description": "HP Smart Tank 585"}, categories=CATS, supplier_default_target=TARGET_STOCK,
	                   manual={"target": TARGET_OWN_USE, "expense_category": "Irodaszer"})
	assert (r["target"], r["expense_category"], r["target_source"]) == (TARGET_OWN_USE, "Irodaszer", SOURCE_MANUAL)


def test_learned_rule_by_code_and_by_description():
	rules = [
		{"supplier_code": "2996C010AA", "target": TARGET_OWN_USE, "expense_category": "Irodaszer"},
		{"description_pattern": "Gépi fólia Standard 23 I T240001123", "target": TARGET_STOCK, "item": "FOLIA23"},
	]
	a = resolve_target({"description": "Canon Szkenner Lide400", "own_codes": ["2996C010AA"]}, rules=rules, categories=CATS, supplier_default_target=TARGET_STOCK)
	assert (a["target"], a["expense_category"], a["target_source"]) == (TARGET_OWN_USE, "Irodaszer", SOURCE_RULE)
	b = resolve_target({"description": "gépi fólia standard 23 i t240001123"}, rules=rules, categories=CATS)
	assert (b["target"], b["item_hint"]) == (TARGET_STOCK, "FOLIA23")


def test_nav_service_flag_without_supplier_default():
	r = resolve_target({"description": "Megbízási díj (szerződés szerint)", "nature": "SERVICE"}, categories=CATS)
	assert (r["target"], r["target_source"]) == (TARGET_EXPENSE, SOURCE_NAV_SERVICE)


def test_plain_goods_default_to_stock():
	r = resolve_target({"description": "TCL 50C6K LED Televízió"}, categories=CATS)
	assert r["target"] == TARGET_STOCK


def test_category_carries_its_own_target():
	# a learned rule that only names the category inherits the category's target
	rules = [{"supplier_code": "DTD", "expense_category": "Fuvar (beszerzés)"}]
	r = resolve_target({"description": "x", "own_codes": ["DTD"]}, rules=rules, categories=CATS)
	assert r["target"] == TARGET_LANDED


def test_aggregate_courier_by_carrier_and_rounding_sign():
	rows = [
		{"line_number": 1, "target": TARGET_EXPENSE, "expense_category": "Szállítási költség", "group_label": "DPD", "net_amount": 1560, "nav_description": "DPD házhozszállítás 10kg-ig"},
		{"line_number": 2, "target": TARGET_EXPENSE, "expense_category": "Szállítási költség", "group_label": "DPD", "net_amount": 1760, "nav_description": "DPD Csomagpont 1kg-ig"},
		{"line_number": 3, "target": TARGET_EXPENSE, "expense_category": "Szállítási költség", "group_label": "Posta (MPL)", "net_amount": 1160, "nav_description": "MPL házhozszállítás 1kg-ig"},
		{"line_number": 4, "target": TARGET_EXPENSE, "expense_category": "Kerekítés", "group_label": None, "net_amount": -2, "nav_description": "kerekítés"},
		{"line_number": 5, "target": TARGET_EXPENSE, "expense_category": "Kerekítés", "group_label": None, "net_amount": 3, "nav_description": "kerekítés"},
	]
	out, problems = aggregate_expense_rows(rows, CATS)
	assert problems == []
	by_name = {(o["item_name"], o["expense_account"]): o for o in out}
	assert by_name[("Szállítási költség — DPD", "5291 - Szállítás")]["amount"] == 3320
	assert by_name[("Szállítási költség — DPD", "5291 - Szállítás")]["line_numbers"] == [1, 2]
	assert by_name[("Szállítási költség — Posta (MPL)", "5291 - Szállítás")]["amount"] == 1160
	assert by_name[("Kerekítés", "9699 - Kerekítés")]["amount"] == -2   # javunkra -> 9699
	assert by_name[("Kerekítés", "8699 - Kerekítés")]["amount"] == 3    # terhünkre -> 8699


def test_aggregate_reports_missing_account_and_category():
	rows = [
		{"line_number": 1, "target": TARGET_EXPENSE, "expense_category": "EPR-díj", "net_amount": 100, "nav_description": "EPR díj KF kód 11042017"},
		{"line_number": 2, "target": TARGET_EXPENSE, "expense_category": "EPR-díj", "net_amount": 200, "nav_description": "EPR díj KF kód 11202017"},
		{"line_number": 3, "target": TARGET_EXPENSE, "expense_category": None, "net_amount": 50, "nav_description": "Parkolási díj"},
	]
	out, problems = aggregate_expense_rows(rows, CATS)
	assert out == []
	assert problems == ["'EPR-díj' kategóriához nincs főkönyvi számla megadva", "#3 Parkolási díj — nincs költségkategória"]
