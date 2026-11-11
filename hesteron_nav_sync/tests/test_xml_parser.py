import sys, os, glob
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from hesteron_nav_sync.nav_client.xml_parser import parse_invoice_xml

SAMPLES = os.environ.get("NAV_SAMPLES_DIR", "")

def test_official_samples_parse():
    files = glob.glob(os.path.join(SAMPLES, "*.xml")) if SAMPLES else []
    for f in files:
        r = parse_invoice_xml(open(f, "rb").read())
        assert r["invoice_number"]
        assert r["supplier"].get("tax_number") or r["is_batch"]

def test_domestic_product_sale_fixture():
    fx = os.path.join(os.path.dirname(__file__), "fixtures", "belfoldi_termekertekesites.xml")
    r = parse_invoice_xml(open(fx, "rb").read())
    assert r["supplier"]["tax_number"] == "99999999"
    assert r["supplier"]["tax_number_full"] == "99999999-2-41"
    assert r["customer"]["tax_number"] == "99887764"
    assert r["currency_code"] == "HUF" and r["exchange_rate"] == 1.0
    l1 = r["lines"][0]
    assert l1["vtsz"] == "020312340" and l1["uom"] == "KILOGRAM" and l1["quantity"] == 1500.0 and l1["unit_price"] == 400.0
    assert l1["vat"]["kind"] == "percentage" and l1["vat"]["percentage"] == 0.05
    assert r["lines"][2]["unit_price"] == -300.0  # discount line
    assert r["summary"]["gross_huf"] == 6157040.0
