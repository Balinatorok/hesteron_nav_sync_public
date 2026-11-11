"""
MNB (Magyar Nemzeti Bank) official exchange rates.

Foreign-currency purchase invoices are booked at the MNB rate of the invoice's
issue date (számla kelte). MNB publishes no rate on weekends/holidays — then the last rate published
before that day applies.

The SOAP service is public: https://www.mnb.hu/arfolyamok.asmx (GetExchangeRates).
Rates are cached in ERPNext's own `Currency Exchange` doctype so each day is fetched once.
"""

from __future__ import annotations

import datetime as _dt
import re
from xml.sax.saxutils import escape

# the service answers on plain http (https returns 404 for the .asmx endpoint)
MNB_URLS = ("http://www.mnb.hu/arfolyamok.asmx", "https://www.mnb.hu/arfolyamok.asmx")
SOAP_ACTION = "http://www.mnb.hu/webservices/MNBArfolyamServiceSoap/GetExchangeRates"


def build_request(currency: str, start: _dt.date, end: _dt.date) -> str:
	return (
		'<?xml version="1.0" encoding="utf-8"?>'
		'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:web="http://www.mnb.hu/webservices/">'
		"<soap:Body><web:GetExchangeRates>"
		f"<web:startDate>{start.isoformat()}</web:startDate>"
		f"<web:endDate>{end.isoformat()}</web:endDate>"
		f"<web:currencyNames>{escape(currency.upper())}</web:currencyNames>"
		"</web:GetExchangeRates></soap:Body></soap:Envelope>"
	)


_DAY = re.compile(r'<Day date="(\d{4}-\d{2}-\d{2})">(.*?)</Day>', re.S)
_RATE = re.compile(r'<Rate unit="(\d+)" curr="([A-Z]{3})">([\d.,]+)</Rate>')


def parse_rates(soap_response: str, currency: str) -> dict[_dt.date, float]:
	"""{day: HUF per 1 unit} from a GetExchangeRates SOAP response (the inner XML arrives escaped)."""
	text = soap_response.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
	out = {}
	for day, body in _DAY.findall(text):
		for unit, curr, value in _RATE.findall(body):
			if curr == currency.upper():
				out[_dt.date.fromisoformat(day)] = float(value.replace(",", ".")) / int(unit)
	return out


def rate_for_day(rates: dict[_dt.date, float], day: _dt.date) -> tuple[_dt.date, float] | None:
	"""The rate valid on `day`: that day's, or the last one published before it."""
	candidates = [d for d in rates if d <= day]
	if not candidates:
		return None
	best = max(candidates)
	return best, rates[best]


def get_mnb_rate(currency: str, day, company_currency: str = "HUF") -> float | None:
	"""HUF per 1 `currency` on `day` (MNB). Cached in Currency Exchange. None if unavailable."""
	import frappe
	from frappe.utils import getdate

	currency = (currency or "").upper()
	if not currency or currency == company_currency:
		return 1.0
	day = getdate(day)

	cached = frappe.db.get_value(
		"Currency Exchange",
		{"from_currency": currency, "to_currency": company_currency, "date": day, "for_buying": 1},
		"exchange_rate",
	)
	if cached:
		return float(cached)

	try:
		import requests

		body = build_request(currency, day - _dt.timedelta(days=10), day).encode("utf-8")
		headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": SOAP_ACTION}
		found, last_error = None, None
		for url in MNB_URLS:
			try:
				resp = requests.post(url, data=body, headers=headers, timeout=15)
				resp.raise_for_status()
				found = rate_for_day(parse_rates(resp.text, currency), day)
				if found:
					break
			except Exception as e:  # try the next address
				last_error = e
		if not found and last_error:
			raise last_error
	except Exception:
		frappe.log_error(frappe.get_traceback(), "MNB árfolyam lekérés")
		return None
	if not found:
		return None

	_, rate = found
	try:
		frappe.get_doc({
			"doctype": "Currency Exchange",
			"date": day,
			"from_currency": currency,
			"to_currency": company_currency,
			"exchange_rate": rate,
			"for_buying": 1,
			"for_selling": 1,
		}).insert(ignore_permissions=True)
	except Exception:
		pass  # a parallel request cached it already
	return rate
