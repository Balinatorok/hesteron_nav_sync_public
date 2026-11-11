"""Run: python3 -m pytest tests/ (no Frappe needed). Uses NAV's official sample as a test vector."""
import sys, os, base64
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from hesteron_nav_sync.nav_client.client import NavInvoiceClient, NavCredentials, SoftwareInfo, NavApiError

CREDS = NavCredentials(login="lwilsmn0uqdxe6u", password="x", sign_key="ac-ac3a-7f661bff7d342N43CYX4U9FG", exchange_key="0123456789ABCDEF", tax_number="11111111")
SW = SoftwareInfo(software_id="HU11111111NAVSYNC1")

def client():
    return NavInvoiceClient(CREDS, SW, environment="test")

def test_signature_matches_official_sample():
    # sample/API sample/tokenExchange.xml in nav-gov-hu/Online-Invoice
    c = client()
    sig = c.request_signature("RID896801578348", "2019-09-11T10:55:31.440Z")
    assert sig == "B4B5E0F197BFFD3DF69BCC98D3BE775F65FD5445EEF95C9D6B6C59425F2B81C4F6DA1FD563B0C7E7D98AF1E1725E5C63C2803B5D3A93D1C02ED354AC92F2CC94"

def test_timestamp_formats():
    ts = NavInvoiceClient.header_timestamp(datetime(2026, 9, 21, 10, 5, 7, 123456, tzinfo=timezone.utc))
    assert ts == "2026-09-21T10:05:07.123Z"
    assert NavInvoiceClient.signature_timestamp(ts) == "20260921100507"

def test_envelope_structure_matches_schema():
    root = client()._build_request("TokenExchangeRequest")
    xml = ET.tostring(root, encoding="unicode")
    ET.fromstring(xml)  # well-formed
    assert 'xmlns="http://schemas.nav.gov.hu/OSA/3.0/api"' in xml
    assert 'xmlns:common="http://schemas.nav.gov.hu/NTCA/1.0/common"' in xml
    tags = [c.tag.split("}")[-1] for c in root]
    assert tags == ["header", "user", "software"]
    assert [c.tag.split("}")[-1] for c in root[2]] == ["softwareId", "softwareName", "softwareOperation", "softwareMainVersion", "softwareDevName", "softwareDevContact", "softwareDevCountryCode"]

def test_digest_request_structure():
    c = client()
    captured = {}
    def fake_post(endpoint, root):
        captured["xml"] = ET.tostring(root, encoding="unicode")
        return ET.fromstring('<QueryInvoiceDigestResponse xmlns="http://schemas.nav.gov.hu/OSA/3.0/api"><result><funcCode>OK</funcCode></result><invoiceDigestResult><currentPage>1</currentPage><availablePage>1</availablePage><invoiceDigest><invoiceNumber>A/1</invoiceNumber><invoiceOperation>CREATE</invoiceOperation><invoiceCategory>NORMAL</invoiceCategory><supplierTaxNumber>99999999</supplierTaxNumber><supplierName>X</supplierName><insDate>2026-09-20T10:00:00Z</insDate></invoiceDigest></invoiceDigestResult></QueryInvoiceDigestResponse>')
    c._post = fake_post
    r = c.query_invoice_digest(datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 21, tzinfo=timezone.utc))
    x = captured["xml"]
    assert x.index("<page>") < x.index("<invoiceDirection>INBOUND") < x.index("<invoiceQueryParams>") < x.index("<mandatoryQueryParams><insDate><dateTimeFrom>2026-09-01T00:00:00.000Z</dateTimeFrom><dateTimeTo>")
    assert r["invoices"][0]["supplierTaxNumber"] == "99999999" and r["available_page_count"] == 1

def test_digest_window_limit():
    import pytest
    with pytest.raises(ValueError):
        client().query_invoice_digest(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 3, 1, tzinfo=timezone.utc))

def test_invoice_data_decode_and_empty_result():
    c = client()
    import gzip
    payload = base64.b64encode(gzip.compress(b"<InvoiceData/>")).decode()
    def fake_post(endpoint, root):
        x = ET.tostring(root, encoding="unicode")
        assert "<invoiceNumberQuery><invoiceNumber>A/1</invoiceNumber><invoiceDirection>INBOUND</invoiceDirection><supplierTaxNumber>99999999</supplierTaxNumber></invoiceNumberQuery>" in x
        return ET.fromstring(f'<QueryInvoiceDataResponse xmlns="http://schemas.nav.gov.hu/OSA/3.0/api"><result><funcCode>OK</funcCode></result><invoiceDataResult><invoiceData>{payload}</invoiceData><auditData><insDate>2026-09-20T10:00:00Z</insDate></auditData><compressedContentIndicator>true</compressedContentIndicator></invoiceDataResult></QueryInvoiceDataResponse>')
    c._post = fake_post
    r = c.query_invoice_data("A/1", "INBOUND", "99999999")
    assert r["invoice_xml"] == b"<InvoiceData/>"
    c._post = lambda e, r: ET.fromstring('<QueryInvoiceDataResponse xmlns="http://schemas.nav.gov.hu/OSA/3.0/api"><result><funcCode>OK</funcCode></result></QueryInvoiceDataResponse>')
    assert c.query_invoice_data("A/2", "INBOUND", "99999999") is None

def test_exchange_token_roundtrip():
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    token = b"ABCD1234TOKEN"
    enc = base64.b64encode(AES.new(b"0123456789ABCDEF", AES.MODE_ECB).encrypt(pad(token, 16))).decode()
    assert client().decrypt_exchange_token(enc) == "ABCD1234TOKEN"

def test_error_response_raises():
    c = client()
    class R:  # minimal fake requests response
        ok = False; status_code = 400
        content = b'<GeneralErrorResponse xmlns="http://schemas.nav.gov.hu/OSA/3.0/api"><result><funcCode>ERROR</funcCode><errorCode>INVALID_REQUEST_SIGNATURE</errorCode><message>bad sig</message></result></GeneralErrorResponse>'
        text = content.decode()
    import hesteron_nav_sync.nav_client.client as mod
    orig = mod.requests.post
    mod.requests.post = lambda *a, **k: R()
    try:
        import pytest
        with pytest.raises(NavApiError) as ei:
            c.token_exchange()
        assert ei.value.error_code == "INVALID_REQUEST_SIGNATURE"
    finally:
        mod.requests.post = orig
