"""
Parses a NAV Online Számla v3.0 `InvoiceData` XML (the base64-decoded payload of queryInvoiceData)
into a plain dict.

Written against the official XSDs (invoiceData.xsd / invoiceBase.xsd, nav-gov-hu/Online-Invoice)
and validated against every file in the official `sample/Data sample/` folder — see
tests/test_xml_parser.py. Element lookups are namespace-agnostic (the data/base/common prefixes vary
between producers) but the *structure* follows the schema exactly:

    InvoiceData
      invoiceNumber, invoiceIssueDate, completenessIndicator
      invoiceMain
        invoice                      (or batchInvoice[] for batch modifications — flagged, not parsed)
          invoiceReference?          (originalInvoiceNumber, modifyWithoutMaster, modificationIndex)
          invoiceHead
            supplierInfo             (supplierTaxNumber/taxpayerId ... , supplierName, supplierAddress)
            customerInfo?            (customerVatStatus, customerVatData/customerTaxNumber/taxpayerId ...)
            invoiceDetail            (invoiceCategory, invoiceDeliveryDate, currencyCode, exchangeRate, ...)
          invoiceLines?
            line[]                   (lineNumber, productCodes?, lineNatureIndicator?, lineDescription?,
                                      quantity?, unitOfMeasure?, unitOfMeasureOwn?, unitPrice?, unitPriceHUF?,
                                      lineAmountsNormal? | lineAmountsSimplified?, lineModificationReference?)
          invoiceSummary             (summaryNormal | summarySimplified, summaryGrossData?)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional


# --------------------------------------------------------------------------- helpers

def _local(tag: str) -> str:
	return tag.split("}")[-1]


def _child(el: Optional[ET.Element], name: str) -> Optional[ET.Element]:
	"""Direct child by local name (never `or`-chain Elements: leaf Elements are falsy!)."""
	if el is None:
		return None
	for c in el:
		if _local(c.tag) == name:
			return c
	return None


def _path(el: Optional[ET.Element], *names: str) -> Optional[ET.Element]:
	cur = el
	for n in names:
		cur = _child(cur, n)
		if cur is None:
			return None
	return cur


def _text(el: Optional[ET.Element], *names: str) -> Optional[str]:
	target = _path(el, *names) if names else el
	if target is None or target.text is None:
		return None
	value = target.text.strip()
	return value or None


def _bool(el: Optional[ET.Element], *names: str) -> Optional[bool]:
	v = _text(el, *names)
	return None if v is None else v.lower() == "true"


def _children(el: Optional[ET.Element], name: str) -> list[ET.Element]:
	if el is None:
		return []
	return [c for c in el if _local(c.tag) == name]


# --------------------------------------------------------------------------- public API

def parse_invoice_xml(xml_bytes: bytes) -> dict:
	root = ET.fromstring(xml_bytes)
	if _local(root.tag) != "InvoiceData":
		raise ValueError(f"Expected InvoiceData root element, got {_local(root.tag)}")

	main = _child(root, "invoiceMain")
	invoice = _child(main, "invoice")
	batch = _children(main, "batchInvoice")

	result = {
		"invoice_number": _text(root, "invoiceNumber"),
		"issue_date": _text(root, "invoiceIssueDate"),
		"completeness_indicator": _bool(root, "completenessIndicator"),
		"is_batch": bool(batch),
		"batch_size": len(batch),
	}

	if invoice is None:
		# Batch modification document (several invoices modified by one document). Rare for a
		# buyer's inbound feed; we don't try to receive goods from it — surface it for review.
		first = _child(batch[0], "invoice") if batch else None
		first_head = _child(first, "invoiceHead")
		result.update({
			"supplier": _parse_supplier(_child(first_head, "supplierInfo")) if first_head is not None else {},
			"customer": {},
			"lines": [],
			"summary": {},
			"reference": None,
			"invoice_category": _text(_child(first_head, "invoiceDetail"), "invoiceCategory") if first_head is not None else None,
			"delivery_date": None,
			"currency_code": _text(_child(first_head, "invoiceDetail"), "currencyCode") if first_head is not None else "HUF",
			"exchange_rate": 1.0,
			"payment_method": None,
			"payment_date": None,
			"order_numbers": [],
		})
		return result

	head = _child(invoice, "invoiceHead")
	detail = _child(head, "invoiceDetail")
	reference = _child(invoice, "invoiceReference")

	result.update({
		"reference": {
			"original_invoice_number": _text(reference, "originalInvoiceNumber"),
			"modify_without_master": _bool(reference, "modifyWithoutMaster"),
			"modification_index": _text(reference, "modificationIndex"),
		} if reference is not None else None,
		"invoice_category": _text(detail, "invoiceCategory"),           # NORMAL | SIMPLIFIED | AGGREGATE
		"delivery_date": _text(detail, "invoiceDeliveryDate"),
		"delivery_period_start": _text(detail, "invoiceDeliveryPeriodStart"),
		"delivery_period_end": _text(detail, "invoiceDeliveryPeriodEnd"),
		"currency_code": _text(detail, "currencyCode") or "HUF",
		"exchange_rate": _float(_text(detail, "exchangeRate"), default=1.0),
		"payment_method": _text(detail, "paymentMethod"),
		"payment_date": _text(detail, "paymentDate"),
		"invoice_appearance": _text(detail, "invoiceAppearance"),
		"small_business_indicator": _bool(detail, "smallBusinessIndicator"),
		"cash_accounting_indicator": _bool(detail, "cashAccountingIndicator"),
		"order_numbers": [
			t.text.strip() for t in _children(_path(detail, "conventionalInvoiceInfo", "orderNumbers"), "orderNumber")
			if t.text
		],
		"supplier": _parse_supplier(_child(head, "supplierInfo")),
		"customer": _parse_customer(_child(head, "customerInfo")),
		"lines": [_parse_line(line) for line in _children(_child(invoice, "invoiceLines"), "line")],
		"merged_item_indicator": _bool(_child(invoice, "invoiceLines"), "mergedItemIndicator"),
		"summary": _parse_summary(_child(invoice, "invoiceSummary")),
	})
	return result


# --------------------------------------------------------------------------- parties

def _parse_tax_number(el: Optional[ET.Element]) -> dict:
	"""base:TaxNumberType — taxpayerId (8 digits) + optional vatCode + countyCode."""
	return {
		"taxpayer_id": _text(el, "taxpayerId"),
		"vat_code": _text(el, "vatCode"),
		"county_code": _text(el, "countyCode"),
	}


def _parse_address(el: Optional[ET.Element]) -> dict:
	detailed = _child(el, "detailedAddress")
	simple = _child(el, "simpleAddress")
	src = detailed if detailed is not None else simple
	street = " ".join(
		p for p in (_text(src, "streetName"), _text(src, "publicPlaceCategory"), _text(src, "number")) if p
	) or _text(src, "additionalAddressDetail")
	return {
		"country_code": _text(src, "countryCode"),
		"postal_code": _text(src, "postalCode"),
		"city": _text(src, "city"),
		"street": street,
	}


def _parse_supplier(el: Optional[ET.Element]) -> dict:
	tax = _parse_tax_number(_child(el, "supplierTaxNumber"))
	return {
		"tax_number": tax["taxpayer_id"],           # 8-digit törzsszám — the matching key
		"tax_number_full": _format_full_tax_number(tax),
		"community_vat_number": _text(el, "communityVatNumber"),
		"name": _text(el, "supplierName"),
		"address": _parse_address(_child(el, "supplierAddress")),
		"bank_account_number": _text(el, "supplierBankAccountNumber"),
		"individual_exemption": _bool(el, "individualExemption"),   # AAM supplier
	}


def _parse_customer(el: Optional[ET.Element]) -> dict:
	vat_data = _child(el, "customerVatData")
	tax = _parse_tax_number(_child(vat_data, "customerTaxNumber"))
	return {
		"vat_status": _text(el, "customerVatStatus"),
		"tax_number": tax["taxpayer_id"],
		"tax_number_full": _format_full_tax_number(tax),
		"name": _text(el, "customerName"),
		"address": _parse_address(_child(el, "customerAddress")),
	}


def _format_full_tax_number(tax: dict) -> Optional[str]:
	if not tax.get("taxpayer_id"):
		return None
	parts = [tax["taxpayer_id"]]
	if tax.get("vat_code"):
		parts.append(tax["vat_code"])
	if tax.get("county_code"):
		parts.append(tax["county_code"])
	return "-".join(parts)


# --------------------------------------------------------------------------- lines

def _parse_line(line: ET.Element) -> dict:
	product_codes = []
	for pc in _children(_child(line, "productCodes"), "productCode"):
		category = _text(pc, "productCodeCategory")
		value = _text(pc, "productCodeValue") if category not in ("OWN", "OTHER") else _text(pc, "productCodeOwnValue")
		if value is None:
			value = _text(pc, "productCodeValue") or _text(pc, "productCodeOwnValue")
		product_codes.append({"category": category, "value": value})

	normal = _child(line, "lineAmountsNormal")
	simplified = _child(line, "lineAmountsSimplified")
	vat = _parse_vat_rate(_child(normal, "lineVatRate")) if normal is not None else _parse_vat_rate(_child(simplified, "lineVatRate"))

	mod_ref = _child(line, "lineModificationReference")
	discount = _child(line, "lineDiscountData")

	return {
		"line_number": _int(_text(line, "lineNumber")),
		"nature": _text(line, "lineNatureIndicator"),          # PRODUCT | SERVICE | OTHER | None
		"description": _text(line, "lineDescription"),
		"product_codes": product_codes,
		"vtsz": next((p["value"] for p in product_codes if p["category"] in ("VTSZ", "KN")), None),
		"own_codes": [p["value"] for p in product_codes if p["category"] in ("OWN", "OTHER") and p["value"]],
		"quantity": _float(_text(line, "quantity")),
		"uom": _text(line, "unitOfMeasure"),                    # NAV enum, e.g. PIECE / KILOGRAM / OWN
		"uom_own": _text(line, "unitOfMeasureOwn"),
		"unit_price": _float(_text(line, "unitPrice")),
		"unit_price_huf": _float(_text(line, "unitPriceHUF")),
		"net_amount": _float(_text(normal, "lineNetAmountData", "lineNetAmount")),
		"net_amount_huf": _float(_text(normal, "lineNetAmountData", "lineNetAmountHUF")),
		"vat_amount": _float(_text(normal, "lineVatData", "lineVatAmount")),
		"vat_amount_huf": _float(_text(normal, "lineVatData", "lineVatAmountHUF")),
		"gross_amount": _float(_text(normal, "lineGrossAmountData", "lineGrossAmountNormal"))
		if normal is not None else _float(_text(simplified, "lineGrossAmountSimplified")),
		"gross_amount_huf": _float(_text(normal, "lineGrossAmountData", "lineGrossAmountNormalHUF"))
		if normal is not None else _float(_text(simplified, "lineGrossAmountSimplifiedHUF")),
		"is_simplified": simplified is not None,
		"vat": vat,
		"discount": {
			"description": _text(discount, "discountDescription"),
			"value": _float(_text(discount, "discountValue")),
			"rate": _float(_text(discount, "discountRate")),
		} if discount is not None else None,
		"modification": {
			"line_number_reference": _int(_text(mod_ref, "lineNumberReference")),
			"operation": _text(mod_ref, "lineOperation"),        # CREATE | MODIFY
		} if mod_ref is not None else None,
		"is_advance": _child(line, "advanceData") is not None,
		"obligated_for_product_fee": _bool(line, "obligatedForProductFee"),
	}


def _parse_vat_rate(el: Optional[ET.Element]) -> dict:
	"""
	VatRateType is a choice: exactly one of vatPercentage / vatContent / vatExemption /
	vatOutOfScope / vatDomesticReverseCharge / marginSchemeIndicator / vatAmountMismatch / noVatCharge.
	We return a normalised {kind, percentage, case, reason} so mapping.py can decide the ERPNext
	Tax Category without re-walking the XML.
	"""
	if el is None:
		return {"kind": None, "percentage": None, "case": None, "reason": None}
	pct = _text(el, "vatPercentage")
	if pct is not None:
		return {"kind": "percentage", "percentage": _float(pct), "case": None, "reason": None}
	content = _text(el, "vatContent")
	if content is not None:  # simplified invoices: VAT content of the gross price
		return {"kind": "content", "percentage": _float(content), "case": None, "reason": None}
	for kind in ("vatExemption", "vatOutOfScope"):
		reason_el = _child(el, kind)
		if reason_el is not None:
			return {"kind": kind, "percentage": 0.0, "case": _text(reason_el, "case"), "reason": _text(reason_el, "reason")}
	if _bool(el, "vatDomesticReverseCharge"):
		return {"kind": "vatDomesticReverseCharge", "percentage": 0.0, "case": "FAD", "reason": None}
	if _text(el, "marginSchemeIndicator") is not None:
		return {"kind": "marginScheme", "percentage": None, "case": _text(el, "marginSchemeIndicator"), "reason": None}
	if _bool(el, "noVatCharge"):
		return {"kind": "noVatCharge", "percentage": 0.0, "case": None, "reason": None}
	mismatch = _child(el, "vatAmountMismatch")
	if mismatch is not None:
		return {"kind": "vatAmountMismatch", "percentage": _float(_text(mismatch, "vatRate")), "case": _text(mismatch, "case"), "reason": None}
	return {"kind": None, "percentage": None, "case": None, "reason": None}


# --------------------------------------------------------------------------- summary

def _parse_summary(el: Optional[ET.Element]) -> dict:
	normal = _child(el, "summaryNormal")
	simplified_list = _children(el, "summarySimplified")
	gross = _child(el, "summaryGrossData")
	by_rate = []
	for row in _children(normal, "summaryByVatRate"):
		by_rate.append({
			"vat": _parse_vat_rate(_child(row, "vatRate")),
			"net": _float(_text(row, "vatRateNetData", "vatRateNetAmount")),
			"net_huf": _float(_text(row, "vatRateNetData", "vatRateNetAmountHUF")),
			"vat_amount": _float(_text(row, "vatRateVatData", "vatRateVatAmount")),
			"vat_amount_huf": _float(_text(row, "vatRateVatData", "vatRateVatAmountHUF")),
		})
	return {
		"net": _float(_text(normal, "invoiceNetAmount")),
		"net_huf": _float(_text(normal, "invoiceNetAmountHUF")),
		"vat": _float(_text(normal, "invoiceVatAmount")),
		"vat_huf": _float(_text(normal, "invoiceVatAmountHUF")),
		"gross": _float(_text(gross, "invoiceGrossAmount")),
		"gross_huf": _float(_text(gross, "invoiceGrossAmountHUF")),
		"by_vat_rate": by_rate,
		"is_simplified": bool(simplified_list),
	}


# --------------------------------------------------------------------------- scalars

def _float(value: Optional[str], default: Optional[float] = None) -> Optional[float]:
	if value is None:
		return default
	try:
		return float(value)
	except ValueError:
		return default


def _int(value: Optional[str]) -> Optional[int]:
	try:
		return int(value) if value is not None else None
	except ValueError:
		return None
