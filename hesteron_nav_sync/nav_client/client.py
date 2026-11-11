"""
NAV Online Számla (Online Invoice) API v3.0 client — read-only operations.

Implements exactly the operations this app needs:
    tokenExchange        — credential smoke test (also proves the exchange key is right)
    queryInvoiceDigest   — list invoices reported against our tax number (INBOUND = we are the buyer)
    queryInvoiceData     — fetch one invoice's full XML
    queryTaxpayer        — registered name/address for a tax number (new supplier pre-fill)

`manageInvoice` / `manageAnnulment` (reporting our own invoices) are intentionally NOT implemented.
The technical user this runs with only needs "Számla lekérdezési jogosultság".

Everything below was written against the OFFICIAL sources, not from memory:
  - src/schemas/nav/gov/hu/OSA/invoiceApi.xsd  (nav-gov-hu/Online-Invoice, master)
  - sample/API sample/*.xml                    (request envelopes incl. a signature test vector)
  - docs/API docs/en/EN_Online Invoice System 3.0 Interface Specification (2026.02.12.).pdf
The signature algorithm is unit-tested against the sample tokenExchange.xml test vector
(tests/test_client.py), so if that test passes, auth is byte-for-byte right.

Facts from the spec that earlier drafts of this file got wrong (kept here so nobody "fixes" them back):
  * The `common` namespace is http://schemas.nav.gov.hu/NTCA/1.0/common — NOT .../OSA/3.0/common.
  * A `<software>` block is REQUIRED on every request, query operations included (BasicOnlineInvoiceRequestType).
  * Header timestamp carries milliseconds: 2019-09-11T10:55:31.440Z. The signature uses YYYYMMDDhhmmss
    (no separators, no fraction, UTC).
  * requestSignature (non-manage ops) = UPPER(SHA3-512(requestId + YYYYMMDDhhmmss + signKey)).
  * passwordHash = UPPER(SHA-512(password)).
  * The exchange key (cserekulcs) is only used to AES-128-ECB-decrypt the tokenExchange token.
    queryInvoiceData's `invoiceData` is plain base64 (gzip only if compressedContentIndicator=true) —
    it is NOT encrypted.
  * insDate digest queries: dateTimeFrom..dateTimeTo may span at most 35 days (spec §"queryInvoiceDigest").
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import logging
import random
import string
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

try:
	from Crypto.Cipher import AES  # pycryptodome
except ImportError:  # pragma: no cover
	AES = None

log = logging.getLogger("hesteron_nav_sync.nav_client")

NS_API = "http://schemas.nav.gov.hu/OSA/3.0/api"
NS_COMMON = "http://schemas.nav.gov.hu/NTCA/1.0/common"
NS_BASE = "http://schemas.nav.gov.hu/OSA/3.0/base"
NS_DATA = "http://schemas.nav.gov.hu/OSA/3.0/data"

ET.register_namespace("", NS_API)          # api elements serialise as the default namespace, like NAV's samples
ET.register_namespace("common", NS_COMMON)
ET.register_namespace("base", NS_BASE)
ET.register_namespace("data", NS_DATA)

API = "{%s}" % NS_API
COMMON = "{%s}" % NS_COMMON

MAX_DIGEST_WINDOW = timedelta(days=35)


class NavApiError(Exception):
	def __init__(self, message: str, error_code: Optional[str] = None, raw_response: Optional[str] = None):
		super().__init__(message)
		self.error_code = error_code
		self.raw_response = raw_response


@dataclass
class NavCredentials:
	login: str
	password: str
	sign_key: str
	exchange_key: str
	tax_number: str  # 8-digit törzsszám


@dataclass
class SoftwareInfo:
	"""
	Mandatory on every request. `software_id` is exactly 18 chars matching [0-9A-Z\\-]{18}; NAV's
	recommendation is ISO country code + developer tax number + a unique suffix, and the same
	installed instance must always send the same id. Example: "HU" + 8-digit tax
	number + "NAVSYNC1" = 18 chars.
	"""

	software_id: str
	software_name: str = "ERPNextNavSync"
	software_operation: str = "LOCAL_SOFTWARE"
	software_main_version: str = "1.0"
	software_dev_name: str = ""
	software_dev_contact: str = ""
	software_dev_country_code: str = "HU"
	software_dev_tax_number: Optional[str] = None

	def validate(self):
		import re

		if not re.fullmatch(r"[0-9A-Z\-]{18}", self.software_id or ""):
			raise ValueError(
				f"software_id must be exactly 18 chars of [0-9A-Z-], got {self.software_id!r}. "
				f"Suggested: 'HU' + 8-digit tax number + 8-char suffix, e.g. HU12345678NAVSYNC1"
			)


class NavInvoiceClient:
	def __init__(self, credentials: NavCredentials, software: SoftwareInfo, environment: str = "production", timeout: int = 60):
		self.credentials = credentials
		self.software = software
		self.software.validate()
		self.timeout = timeout
		if environment == "production":
			self.base_url = "https://api.onlineszamla.nav.gov.hu/invoiceService/v3"
		elif environment == "test":
			self.base_url = "https://api-test.onlineszamla.nav.gov.hu/invoiceService/v3"
		else:
			raise ValueError("environment must be 'production' or 'test'")

	# ------------------------------------------------------------------ auth primitives

	@staticmethod
	def password_hash(password: str) -> str:
		return hashlib.sha512(password.encode("utf-8")).hexdigest().upper()

	@staticmethod
	def generate_request_id() -> str:
		"""[+a-zA-Z0-9_]{1,30}, unique per request."""
		suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
		return f"HNS{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')[:17]}{suffix}"[:30]

	@staticmethod
	def header_timestamp(now: Optional[datetime] = None) -> str:
		now = now or datetime.now(timezone.utc)
		return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

	@staticmethod
	def signature_timestamp(header_timestamp: str) -> str:
		"""'2019-09-11T10:55:31.440Z' -> '20190911105531'."""
		return header_timestamp[:19].replace("-", "").replace("T", "").replace(":", "")

	def request_signature(self, request_id: str, header_timestamp: str) -> str:
		payload = f"{request_id}{self.signature_timestamp(header_timestamp)}{self.credentials.sign_key}"
		return hashlib.sha3_512(payload.encode("utf-8")).hexdigest().upper()

	# ------------------------------------------------------------------ envelope

	def _build_request(self, root_tag: str) -> ET.Element:
		request_id = self.generate_request_id()
		timestamp = self.header_timestamp()

		root = ET.Element(API + root_tag)

		header = ET.SubElement(root, COMMON + "header")
		ET.SubElement(header, COMMON + "requestId").text = request_id
		ET.SubElement(header, COMMON + "timestamp").text = timestamp
		ET.SubElement(header, COMMON + "requestVersion").text = "3.0"
		ET.SubElement(header, COMMON + "headerVersion").text = "1.0"

		user = ET.SubElement(root, COMMON + "user")
		ET.SubElement(user, COMMON + "login").text = self.credentials.login
		pwd = ET.SubElement(user, COMMON + "passwordHash", cryptoType="SHA-512")
		pwd.text = self.password_hash(self.credentials.password)
		ET.SubElement(user, COMMON + "taxNumber").text = self.credentials.tax_number
		sig = ET.SubElement(user, COMMON + "requestSignature", cryptoType="SHA3-512")
		sig.text = self.request_signature(request_id, timestamp)

		sw = ET.SubElement(root, API + "software")
		ET.SubElement(sw, API + "softwareId").text = self.software.software_id
		ET.SubElement(sw, API + "softwareName").text = self.software.software_name
		ET.SubElement(sw, API + "softwareOperation").text = self.software.software_operation
		ET.SubElement(sw, API + "softwareMainVersion").text = self.software.software_main_version
		ET.SubElement(sw, API + "softwareDevName").text = self.software.software_dev_name
		ET.SubElement(sw, API + "softwareDevContact").text = self.software.software_dev_contact
		ET.SubElement(sw, API + "softwareDevCountryCode").text = self.software.software_dev_country_code
		if self.software.software_dev_tax_number:
			ET.SubElement(sw, API + "softwareDevTaxNumber").text = self.software.software_dev_tax_number

		return root

	def _post(self, endpoint: str, root: ET.Element) -> ET.Element:
		body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
		url = f"{self.base_url}/{endpoint}"
		try:
			resp = requests.post(url, data=body, headers={"Content-Type": "application/xml", "Accept": "application/xml"}, timeout=self.timeout)
		except requests.RequestException as exc:
			raise NavApiError(f"Transport error calling {endpoint}: {exc}") from exc

		try:
			parsed = ET.fromstring(resp.content)
		except ET.ParseError as exc:
			raise NavApiError(f"Unparseable NAV response for {endpoint} (HTTP {resp.status_code}): {exc}", raw_response=resp.text) from exc

		func_code = _find_text(parsed, "funcCode")
		if _local(parsed.tag) in ("GeneralErrorResponse", "GeneralExceptionResponse") or func_code == "ERROR" or not resp.ok:
			code = _find_text(parsed, "errorCode")
			message = _find_text(parsed, "message")
			raise NavApiError(f"NAV {endpoint} failed (HTTP {resp.status_code}): {code} — {message}", error_code=code, raw_response=resp.text)
		return parsed

	# ------------------------------------------------------------------ operations

	def token_exchange(self) -> dict:
		"""Returns {"token": <decrypted token>, "valid_from": ..., "valid_to": ...}. Proves login,
		password, signing key AND exchange key are all correct — run this first on a new setup."""
		parsed = self._post("tokenExchange", self._build_request("TokenExchangeRequest"))
		encoded = _find_text(parsed, "encodedExchangeToken")
		if not encoded:
			raise NavApiError("tokenExchange returned no encodedExchangeToken", raw_response=ET.tostring(parsed, encoding="unicode"))
		return {
			"token": self.decrypt_exchange_token(encoded),
			"valid_from": _find_text(parsed, "tokenValidityFrom"),
			"valid_to": _find_text(parsed, "tokenValidityTo"),
		}

	def decrypt_exchange_token(self, encoded_token: str) -> str:
		"""AES-128-ECB with the technical user's exchange key (spec §1.4 / tokenExchange notes)."""
		if AES is None:
			raise NavApiError("pycryptodome is not installed (needed for tokenExchange decoding)")
		key = self.credentials.exchange_key.encode("utf-8")
		if len(key) != 16:
			raise NavApiError(f"Exchange key must be 16 characters for AES-128, got {len(key)} — check the value copied from the NAV portal")
		raw = base64.b64decode(encoded_token)
		decrypted = AES.new(key, AES.MODE_ECB).decrypt(raw)
		pad = decrypted[-1] if decrypted else 0
		if 0 < pad <= 16 and decrypted[-pad:] == bytes([pad]) * pad:
			decrypted = decrypted[:-pad]
		return decrypted.decode("utf-8", errors="strict")

	def query_invoice_digest(self, date_from: datetime, date_to: datetime, direction: str = "INBOUND", page: int = 1) -> dict:
		"""
		One page of digests whose insDate (when NAV received the report) falls in [date_from, date_to].
		Returns {"invoices": [dict...], "current_page": int, "available_page_count": int}.
		"""
		if direction not in ("INBOUND", "OUTBOUND"):
			raise ValueError("direction must be INBOUND or OUTBOUND")
		date_from = _utc(date_from)
		date_to = _utc(date_to)
		if date_to - date_from > MAX_DIGEST_WINDOW:
			raise ValueError("insDate window may span at most 35 days — chunk the range (sync.py does this)")

		root = self._build_request("QueryInvoiceDigestRequest")
		ET.SubElement(root, API + "page").text = str(page)
		ET.SubElement(root, API + "invoiceDirection").text = direction
		params = ET.SubElement(root, API + "invoiceQueryParams")
		mandatory = ET.SubElement(params, API + "mandatoryQueryParams")
		ins = ET.SubElement(mandatory, API + "insDate")
		ET.SubElement(ins, API + "dateTimeFrom").text = self.header_timestamp(date_from)
		ET.SubElement(ins, API + "dateTimeTo").text = self.header_timestamp(date_to)

		parsed = self._post("queryInvoiceDigest", root)
		invoices = []
		for digest in parsed.iter():
			if _local(digest.tag) == "invoiceDigest":
				invoices.append({_local(c.tag): (c.text or "").strip() for c in digest})
		return {
			"invoices": invoices,
			"current_page": int(_find_text(parsed, "currentPage") or page),
			"available_page_count": int(_find_text(parsed, "availablePage") or 1),
		}

	def query_invoice_data(self, invoice_number: str, direction: str = "INBOUND", supplier_tax_number: Optional[str] = None, batch_index: Optional[int] = None) -> Optional[dict]:
		"""
		Full invoice XML for one invoice. Returns None when NAV has no (single, valid) hit — the
		response is then an empty business response with funcCode OK. supplier_tax_number is the
		8-digit taxpayerId; for INBOUND lookups always pass it (invoice numbers are only unique
		per supplier).
		"""
		root = self._build_request("QueryInvoiceDataRequest")
		q = ET.SubElement(root, API + "invoiceNumberQuery")
		ET.SubElement(q, API + "invoiceNumber").text = invoice_number
		ET.SubElement(q, API + "invoiceDirection").text = direction
		if batch_index:
			ET.SubElement(q, API + "batchIndex").text = str(batch_index)
		if supplier_tax_number:
			ET.SubElement(q, API + "supplierTaxNumber").text = supplier_tax_number[:8]

		parsed = self._post("queryInvoiceData", root)
		result_el = next((el for el in parsed.iter() if _local(el.tag) == "invoiceDataResult"), None)
		if result_el is None:
			return None
		encoded = _find_text(result_el, "invoiceData")
		compressed = (_find_text(result_el, "compressedContentIndicator") or "false").lower() == "true"
		return {
			"invoice_number": invoice_number,
			"invoice_xml": self.decode_invoice_data(encoded, compressed),
			"ins_date": _find_text(result_el, "insDate"),
			"source": _find_text(result_el, "source"),
			"transaction_id": _find_text(result_el, "transactionId"),
			"original_request_version": _find_text(result_el, "originalRequestVersion"),
		}

	@staticmethod
	def decode_invoice_data(base64_payload: str, compressed: bool) -> bytes:
		raw = base64.b64decode(base64_payload)
		if compressed:
			with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
				raw = gz.read()
		return raw

	def query_taxpayer(self, tax_number: str) -> Optional[dict]:
		root = self._build_request("QueryTaxpayerRequest")
		ET.SubElement(root, API + "taxNumber").text = tax_number[:8]
		parsed = self._post("queryTaxpayer", root)
		if (_find_text(parsed, "taxpayerValidity") or "false").lower() != "true":
			return None
		data = next((el for el in parsed.iter() if _local(el.tag) == "taxpayerData"), None)
		address = next((el for el in parsed.iter() if _local(el.tag) == "taxpayerAddress"), None)
		street = " ".join(p for p in (_find_text(address, "streetName"), _find_text(address, "publicPlaceCategory"), _find_text(address, "number")) if p) if address is not None else None
		return {
			"name": _find_text(data, "taxpayerName"),
			"short_name": _find_text(data, "taxpayerShortName"),
			"tax_number": _find_text(data, "taxpayerId") or tax_number[:8],
			"vat_code": _find_text(data, "vatCode"),
			"county_code": _find_text(data, "countyCode"),
			"incorporation": _find_text(data, "incorporation"),
			"postal_code": _find_text(address, "postalCode") if address is not None else None,
			"city": _find_text(address, "city") if address is not None else None,
			"street": street,
		}


# ---------------------------------------------------------------------- module helpers

def _local(tag: str) -> str:
	return tag.split("}")[-1]


def _find_text(root: Optional[ET.Element], local_name: str) -> Optional[str]:
	if root is None:
		return None
	for el in root.iter():
		if _local(el.tag) == local_name:
			return (el.text or "").strip() or None
	return None


def _utc(dt: datetime) -> datetime:
	return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
