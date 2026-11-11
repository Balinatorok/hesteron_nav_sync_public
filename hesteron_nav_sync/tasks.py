"""Scheduler entry points (see hooks.py)."""

import frappe

from hesteron_nav_sync.sync import run_sync


def sync_inbound_invoices():
	try:
		run_sync()
	except Exception:
		frappe.log_error(title="NAV sync: unhandled error", message=frappe.get_traceback())
	try:
		from hesteron_nav_sync.invoice_pdf import retry_unmatched

		retry_unmatched()
	except Exception:
		frappe.log_error(title="NAV számlakép: újrapróbálás hiba", message=frappe.get_traceback())


def fetch_invoice_pdf_mails():
	try:
		from hesteron_nav_sync.graph_mail import fetch_invoice_pdfs

		fetch_invoice_pdfs()
	except Exception:
		frappe.log_error(title="NAV számlakép (Graph): unhandled error", message=frappe.get_traceback())
