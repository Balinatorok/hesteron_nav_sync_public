import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from hesteron_nav_sync.mnb import build_request, parse_rates, rate_for_day

SAMPLE = (
	'<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
	"<soap:Body><GetExchangeRatesResponse xmlns=\"http://www.mnb.hu/webservices/\"><GetExchangeRatesResult>"
	"&lt;MNBExchangeRates&gt;&lt;Day date=\"2026-09-25\"&gt;&lt;Rate unit=\"1\" curr=\"EUR\"&gt;391,85&lt;/Rate&gt;&lt;/Day&gt;"
	"&lt;Day date=\"2026-09-24\"&gt;&lt;Rate unit=\"1\" curr=\"EUR\"&gt;392,10&lt;/Rate&gt;&lt;/Day&gt;&lt;/MNBExchangeRates&gt;"
	"</GetExchangeRatesResult></GetExchangeRatesResponse></soap:Body></soap:Envelope>"
)


def test_parse_and_weekend_fallback():
	rates = parse_rates(SAMPLE, "EUR")
	assert rates[dt.date(2026, 9, 25)] == 391.85
	# Saturday 2026-09-26: no MNB rate -> Friday's applies
	assert rate_for_day(rates, dt.date(2026, 9, 26)) == (dt.date(2026, 9, 25), 391.85)
	assert rate_for_day(rates, dt.date(2026, 9, 24)) == (dt.date(2026, 9, 24), 392.10)
	assert rate_for_day(rates, dt.date(2026, 9, 1)) is None


def test_unit_and_request():
	jpy = SAMPLE.replace('unit="1" curr="EUR"&gt;391,85', 'unit="100" curr="JPY"&gt;245,50')
	assert parse_rates(jpy, "JPY") == {dt.date(2026, 9, 25): 2.455}
	req = build_request("eur", dt.date(2026, 9, 16), dt.date(2026, 9, 26))
	assert "<web:currencyNames>EUR</web:currencyNames>" in req and "2026-09-16" in req
