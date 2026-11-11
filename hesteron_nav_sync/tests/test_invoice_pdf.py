import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fake_frappe import install_fake  # noqa: E402

install_fake({})

from hesteron_nav_sync.invoice_pdf import contains_token, pick_import  # noqa: E402

CANDS = [
	{"name": "imp1", "nav_invoice_number": "26-7001237", "supplier_name": "Minta Papír Kft", "supplier_tax_number": "12345678-2-41", "supplier_email_domains": {"mintapapir.hu"}},
	{"name": "imp2", "nav_invoice_number": "26-7001236", "supplier_name": "Minta Papír Kft", "supplier_tax_number": "12345678-2-41", "supplier_email_domains": {"mintapapir.hu"}},
	{"name": "imp3", "nav_invoice_number": "28100423", "supplier_name": "Példa Szállítmányozó Kft.", "supplier_tax_number": "87654321-2-43", "supplier_email_domains": set()},
	{"name": "imp4", "nav_invoice_number": "E-FUTAR-2026-27958", "supplier_name": "Futár Példa Kft.", "supplier_tax_number": "11112222-2-13", "supplier_email_domains": {"futarpelda.hu"}},
]


def test_token_boundaries():
	assert contains_token("SZÁMLA 26-7001237 MELLÉKELVE", "26-7001237")
	assert not contains_token("126-70012370", "26-7001237")
	assert not contains_token("ABC", "ABC")  # too short


def test_number_in_subject_is_enough():
	assert pick_import("Számla 26-7001237", "", "x@valami.hu", CANDS) == "imp1"


def test_number_in_file_name():
	assert pick_import("Új számla E-FUTAR-2026-27958.pdf", "", None, CANDS) == "imp4"


def test_number_only_in_pdf_needs_supplier_confirmation():
	body = "Számlaszám: 28100423 Fizetendő 12 000 Ft"
	assert pick_import("Invoice", body, "noreply@random.com", CANDS) is None
	body2 = body + " Eladó adószáma: 87654321-2-43"
	assert pick_import("Invoice", body2, "noreply@random.com", CANDS) == "imp3"


def test_sender_domain_confirms():
	assert pick_import("Számlája elkészült", "Sorszám: 26-7001236", "szamla@mintapapir.hu", CANDS) == "imp2"


def test_two_numbers_in_subject_is_ambiguous():
	assert pick_import("26-7001236, 26-7001237", "", None, CANDS) is None


def test_nothing():
	assert pick_import("Hírlevél", "Akciók", "news@shop.hu", CANDS) is None
