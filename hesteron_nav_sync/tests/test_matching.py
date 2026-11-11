import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fake_frappe import install_fake  # noqa: E402

TABLES = {
	"Supplier": [
		{"name": "ComParts Line Kft.", "tax_id": "12345678-2-41", "disabled": 0},
		{"name": "Megaram Kft.", "tax_id": "HU87654321", "disabled": 0},
	],
	"Item": [
		{"name": "AACBF", "item_name": "Vention USB-C kábel 1m fekete", "description": "USB-C to USB-C cable 1m black", "item_group": "Kábelek", "brand": "Vention", "stock_uom": "db", "has_serial_no": 0, "has_batch_no": 0, "is_stock_item": 1, "customs_tariff_number": "85444290", "custom_helykód": "2/074", "disabled": 0},
		{"name": "BGWH0", "item_name": "Vention HDMI kábel 2m", "description": None, "item_group": "Kábelek", "brand": "Vention", "stock_uom": "db", "has_serial_no": 0, "has_batch_no": 0, "is_stock_item": 1, "customs_tariff_number": "85444290", "custom_helykód": "2/091", "disabled": 0},
		{"name": "1205050215", "item_name": "SKG G7 Pro nyakmasszírozó", "description": None, "item_group": "Masszírozók", "brand": "SKG", "stock_uom": "db", "has_serial_no": 1, "has_batch_no": 0, "is_stock_item": 1, "customs_tariff_number": "90191010", "custom_helykód": "1/004", "disabled": 0},
		{"name": "EPS-T0711", "item_name": "Epson T0711 fekete tintapatron", "description": None, "item_group": "Tintapatronok", "brand": "Epson", "stock_uom": "db", "has_serial_no": 0, "has_batch_no": 0, "is_stock_item": 1, "customs_tariff_number": None, "custom_helykód": None, "disabled": 0},
		{"name": "SZALL", "item_name": "Szállítási díj", "description": None, "item_group": "Szolgáltatások", "brand": None, "stock_uom": "db", "has_serial_no": 0, "has_batch_no": 0, "is_stock_item": 0, "customs_tariff_number": None, "custom_helykód": None, "disabled": 0},
	],
	"Item Supplier": [{"parent": "EPS-T0711", "supplier": "ComParts Line Kft.", "supplier_part_no": "C13T07114012"}],
	"Item Barcode": [{"parent": "AACBF", "barcode": "6922794712345"}],
	"NAV Item Mapping Rule": [{"supplier": "ComParts Line Kft.", "supplier_code": None, "description_pattern": "HDMI kabel 2 meter Vention", "item": "BGWH0"}],
	"Purchase Order": [{"name": "PUR-ORD-2026-00007", "supplier": "ComParts Line Kft.", "docstatus": 1, "per_received": 0}],
	"Purchase Order Item": [{"name": "poi1", "parent": "PUR-ORD-2026-00007", "docstatus": 1, "item_code": "1205050215", "item_name": "SKG G7 Pro", "qty": 10, "received_qty": 0, "rate": 12000, "uom": "db"}],
	"Purchase Receipt Item": [{"supplier": "ComParts Line Kft.", "item_code": "AACBF"}],
	"Purchase Invoice Item": [],
	"UOM": [{"name": "db"}, {"name": "kg"}],
	"Item Group": [{"name": "Szolgáltatások"}, {"name": "Kábelek"}],
	"Brand": [{"name": "Vention"}, {"name": "SKG"}, {"name": "Epson"}, {"name": "Kodak"}, {"name": "HP"}],
}

install_fake(TABLES)

from hesteron_nav_sync.matching import MatchContext, find_supplier, match_line  # noqa: E402
from hesteron_nav_sync.mapping import resolve_tax_category, vat_label  # noqa: E402


def line(desc, qty=1, price=1000, codes=None, uom="PIECE", nature="PRODUCT", vtsz=None, **kw):
	d = {"line_number": 1, "description": desc, "quantity": qty, "unit_price": price, "net_amount": qty * price, "own_codes": codes or [], "uom": uom, "uom_own": None, "nature": nature, "vtsz": vtsz, "vat": {"kind": "percentage", "percentage": 0.27}}
	d.update(kw)
	return d


def ctx():
	return MatchContext("ComParts Line Kft.", settings=None)


def test_supplier_by_tax_number_variants():
	assert find_supplier("12345678") == "ComParts Line Kft."
	assert find_supplier("87654321") == "Megaram Kft."
	assert find_supplier("00000000") is None


def test_rule_wins():
	r = match_line(line("HDMI kabel 2 meter Vention"), ctx())
	assert r["item_code"] == "BGWH0" and r["match_method"] == "rule" and r["match_status"] == "Egyeztetve" and r["helykod"] == "2/091"


def test_supplier_part_no_from_nav_product_code():
	r = match_line(line("Tintapatron fekete", codes=["C13T07114012"]), ctx())
	assert r["item_code"] == "EPS-T0711" and r["match_method"] == "supplier_code" and r["match_confidence"] == 95


def test_barcode_in_description():
	r = match_line(line("Kábel USB-C 6922794712345"), ctx())
	assert r["item_code"] == "AACBF" and r["match_method"] == "barcode"


def test_item_code_in_description_and_po_link_and_serial_note():
	r = match_line(line("SKG nyakmasszírozó 1205050215", qty=10, price=12000), ctx())
	assert r["item_code"] == "1205050215"
	assert r["purchase_order"] == "PUR-ORD-2026-00007" and r["purchase_order_item"] == "poi1"
	assert r["has_serial_no"] == 1 and "10 db sorozatszámot" in r["match_note"]
	assert r["match_status"] == "Egyeztetve"


def test_po_line_alone_identifies_item():
	# no code anywhere, but exactly one open PO line with same qty+rate
	r = match_line(line("Masszírozó készülék", qty=10, price=12000), ctx())
	assert r["item_code"] == "1205050215" and r["match_method"] == "po_line" and r["match_confidence"] == 90


def test_fuzzy_is_only_a_suggestion():
	r = match_line(line("Vention USB-C kabel 1 m fekete"), ctx())
	assert r["item_code"] == "AACBF" and r["match_method"] == "name_fuzzy"
	assert r["match_status"] in ("Javaslat", "Egyeztetve") and 60 <= r["match_confidence"] <= 100


def test_unknown_item_gets_suggestions():
	r = match_line(line("Teljesen ismeretlen termék XYZ", vtsz="85444290"), ctx())
	assert r["item_code"] is None and r["match_status"] == "Új cikk"
	# brand is never guessed from neighbours — only taken from the description
	assert r["suggested_item_group"] == "Kábelek" and r["suggested_brand"] is None and r["uom"] == "db"
	assert "Javasolt rögzítés" in r["match_note"]


def test_brand_comes_from_description_not_supplier_history():
	# ComParts' history is Epson, but the line says KODAK
	r = match_line(line("KODAK Ultra Premium papír A4 fényes 100 lap"), ctx())
	assert r["item_code"] is None and r["suggested_brand"] == "Kodak"
	r2 = match_line(line("Vention HDMI 2.1 adapter ÚJ"), ctx())
	assert r2["suggested_brand"] == "Vention" or r2["item_code"]
	r3 = match_line(line("Ismeretlen nyomtatópapír A3"), ctx())
	assert r3["suggested_brand"] is None


def test_service_and_negative_and_advance_lines():
	s = match_line(line("Szállítási díj", nature="SERVICE"), ctx())
	assert "Szolgáltatás-sor" in s["match_note"]
	n = match_line(line("Árengedmény", qty=1, price=-300), ctx())
	assert n["match_status"] == "Kihagy" and n["match_method"] == "negative"
	a = match_line(line("Előleg", is_advance=True), ctx())
	assert a["match_status"] == "Kihagy"


def test_vat_mapping():
	assert resolve_tax_category({"kind": "percentage", "percentage": 0.27}) == ("Belföldi 27%", False)
	assert resolve_tax_category({"kind": "vatExemption", "percentage": 0, "case": "TAM"}) == ("TAM", False)
	assert resolve_tax_category({"kind": "vatExemption", "percentage": 0, "case": "AAM"}) == (None, True)
	assert vat_label({"kind": "percentage", "percentage": 0.27}) == "27%" and vat_label({"kind": "vatExemption", "case": "TAM"}) == "TAM"
