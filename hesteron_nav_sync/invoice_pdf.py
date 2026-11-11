"""
Supplier invoice PDFs arriving by e-mail -> attached to the matching NAV import (and its Purchase Invoice).

Goal: invoice PDFs that arrive by e-mail should be attached to the invoice automatically.
The invoices arrive at a shared mailbox; an ERPNext Email Account pulls that mailbox, so every received
mail becomes a Communication and every attachment a File attached to it. We hook File.after_insert:

  PDF attached to a received Communication
    -> find the NAV Inbound Invoice Import whose invoice number appears in the subject / file name / PDF text
    -> attach a copy of the File to that import, and to its Purchase Invoice if one already exists.

When the PDF arrives before the NAV data (NAV sync is hourly), nothing is lost: the hourly job retries
unmatched PDFs of the last 10 days. When the Purchase Invoice is created later from the import, the
PDFs are copied onto it (copy_to_purchase_invoice, called from create_purchase_invoice).

Matching is deliberately strict — a wrong attachment is worse than none:
  * the invoice number must appear as a whole token (not inside a longer number/word);
  * if it is found only in the PDF body (not subject / file name), the supplier must be confirmed too
    (tax number in the PDF, sender's e-mail domain = supplier's, or supplier name in the PDF);
  * exactly one import may match, otherwise we skip and log.
"""

from __future__ import annotations

import io
import re

import frappe


def _since(days):
	from frappe.utils import add_days, nowdate

	return add_days(nowdate(), -days)

IMPORT_DT = "NAV Inbound Invoice Import"
RETRY_DAYS = 10
LOOKBACK_DAYS = 180  # NAV imports considered as candidates (by invoice date)


# ---------------------------------------------------------------------------- hooks

def on_file_insert(doc, method=None):
	"""doc_events hook: File.after_insert."""
	if doc.attached_to_doctype != "Communication" or not _is_pdf(doc):
		return
	frappe.enqueue(
		"hesteron_nav_sync.invoice_pdf.process_file",
		file_name=doc.name,
		queue="short",
		enqueue_after_commit=True,
	)


def process_file(file_name: str) -> str | None:
	"""Try to place one e-mailed PDF. Returns the import name it was attached to, if any."""
	try:
		f = frappe.get_doc("File", file_name)
	except frappe.DoesNotExistError:
		return None
	if f.attached_to_doctype != "Communication" or not _is_pdf(f):
		return None
	if _already_placed(f):
		return None

	comm = frappe.db.get_value(
		"Communication", f.attached_to_name,
		["subject", "sender", "sent_or_received", "communication_medium"], as_dict=True,
	)
	if not comm or comm.sent_or_received != "Received":
		return None

	header_text = " ".join(x for x in (comm.subject, f.file_name) if x)
	pdf_text = _pdf_text(f)
	target = find_import(header_text, pdf_text, comm.sender)
	if not target:
		return None

	attach_copy(f, IMPORT_DT, target)
	pi = frappe.db.get_value(IMPORT_DT, target, "linked_purchase_invoice")
	if pi:
		attach_copy(f, "Purchase Invoice", pi)
	frappe.get_doc(IMPORT_DT, target).add_comment(
		"Info", f"Számlakép automatikusan csatolva az e-mailből: {frappe.utils.escape_html(f.file_name)} ({frappe.utils.escape_html(comm.sender or '')})"
	)
	return target


def retry_unmatched(days: int = RETRY_DAYS, limit: int = 150) -> int:
	"""Hourly: e-mailed PDFs that could not be placed yet (NAV data may have arrived since)."""
	files = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Communication",
			"creation": [">=", _since(days)],
			"file_name": ["like", "%.pdf"],
		},
		pluck="name",
		order_by="creation desc",
		limit=limit,
	)
	placed = 0
	for name in files:
		try:
			if process_file(name):
				placed += 1
		except Exception:
			frappe.log_error(title="NAV számlakép: csatolási hiba", message=frappe.get_traceback())
	return placed


def copy_to_purchase_invoice(import_name: str, purchase_invoice: str) -> int:
	"""Copy the PDFs already attached to the NAV import onto the newly created Purchase Invoice."""
	n = 0
	for f in frappe.get_all(
		"File",
		filters={"attached_to_doctype": IMPORT_DT, "attached_to_name": import_name},
		fields=["name"],
	):
		fd = frappe.get_doc("File", f.name)
		if _is_pdf(fd) and attach_copy(fd, "Purchase Invoice", purchase_invoice):
			n += 1
	return n


# ---------------------------------------------------------------------------- matching

def norm(text: str | None) -> str:
	return re.sub(r"\s+", " ", (text or "")).upper()


def contains_token(haystack: str, needle: str) -> bool:
	"""needle (already upper) as a whole token: not glued to letters/digits on either side."""
	if not needle or len(needle) < 4:
		return False
	return re.search(rf"(?<![A-Z0-9]){re.escape(needle)}(?![A-Z0-9])", haystack) is not None


def _digits(v):
	return re.sub(r"\D", "", v or "")


def supplier_confirmed(pdf_text_upper: str, sender: str | None, imp: dict) -> bool:
	taxnum = _digits(imp.get("supplier_tax_number"))[:8]
	# 8-digit törzsszám as its own number in the PDF (12345678-2-41 and HU12345678 both qualify)
	if len(taxnum) == 8 and re.search(rf"(?<!\d){taxnum}(?!\d)", pdf_text_upper):
		return True
	domain = (sender or "").split("@")[-1].strip(" >").lower() if sender and "@" in sender else ""
	if domain and imp.get("supplier_email_domains") and domain in imp["supplier_email_domains"]:
		return True
	name = norm(imp.get("supplier_name"))
	core = re.sub(r"\b(KFT|ZRT|BT|NYRT|KFT\.|ZRT\.)\b\.?", "", name).strip(" .,")
	if len(core) >= 4 and core in pdf_text_upper:
		return True
	return False


def pick_import(header_text: str, pdf_text: str, sender: str | None, candidates: list[dict]) -> str | None:
	"""Pure matching logic (unit-tested). candidates: dicts with name, nav_invoice_number, supplier_* ."""
	head = norm(header_text)
	body = norm(pdf_text)
	strong, weak = [], []
	for imp in candidates:
		num = norm(imp.get("nav_invoice_number")).strip()
		if not num:
			continue
		if contains_token(head, num):
			strong.append(imp["name"])
		elif contains_token(body, num) and supplier_confirmed(body, sender, imp):
			weak.append(imp["name"])
	strong, weak = sorted(set(strong)), sorted(set(weak))
	if len(strong) == 1:
		return strong[0]
	if not strong and len(weak) == 1:
		return weak[0]
	return None


def find_import(header_text: str, pdf_text: str, sender: str | None) -> str | None:
	rows = frappe.get_all(
		IMPORT_DT,
		filters={"invoice_issue_date": [">=", _since(LOOKBACK_DAYS)]},
		fields=["name", "nav_invoice_number", "supplier", "supplier_name", "supplier_tax_number"],
		limit=5000,
	)
	domains = _supplier_email_domains({r.supplier for r in rows if r.supplier})
	cands = []
	for r in rows:
		d = dict(r)
		d["supplier_email_domains"] = domains.get(r.supplier, set())
		cands.append(d)
	hit = pick_import(header_text, pdf_text, sender, cands)
	if not hit:
		return None
	return hit


def _supplier_email_domains(suppliers: set) -> dict:
	out = {}
	if not suppliers:
		return out
	generic = {"gmail.com", "freemail.hu", "citromail.hu", "hotmail.com", "outlook.com", "yahoo.com", "t-online.hu"}
	for s in frappe.get_all("Supplier", filters={"name": ["in", list(suppliers)]}, fields=["name", "email_id"]):
		if s.email_id and "@" in s.email_id:
			out.setdefault(s.name, set()).add(s.email_id.split("@")[-1].lower())
	for c in frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Supplier", "link_name": ["in", list(suppliers)], "parenttype": "Contact"},
		fields=["parent", "link_name"],
	):
		email = frappe.db.get_value("Contact", c.parent, "email_id")
		if email and "@" in email:
			out.setdefault(c.link_name, set()).add(email.split("@")[-1].lower())
	return {k: {d for d in v if d not in generic} for k, v in out.items()}


# ---------------------------------------------------------------------------- files

def _is_pdf(f) -> bool:
	return (f.file_name or f.file_url or "").lower().endswith(".pdf")


def _already_placed(f) -> bool:
	return bool(frappe.db.exists("File", {"file_url": f.file_url, "attached_to_doctype": IMPORT_DT}))


def attach_copy(f, doctype: str, name: str) -> bool:
	if frappe.db.exists("File", {"file_url": f.file_url, "attached_to_doctype": doctype, "attached_to_name": name}):
		return False
	frappe.get_doc({
		"doctype": "File",
		"file_url": f.file_url,
		"file_name": f.file_name,
		"is_private": f.is_private,
		"attached_to_doctype": doctype,
		"attached_to_name": name,
	}).insert(ignore_permissions=True)
	return True


def _pdf_text(f, max_pages: int = 4) -> str:
	try:
		content = f.get_content()
	except Exception:
		return ""
	if isinstance(content, str):
		content = content.encode("latin-1", "ignore")
	try:
		try:
			from pypdf import PdfReader
		except ImportError:
			from PyPDF2 import PdfReader  # older benches
		reader = PdfReader(io.BytesIO(content))
		return "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])
	except Exception:
		return ""
