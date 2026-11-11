"""
Supplier invoice PDFs from an Outlook folder via Microsoft Graph -> NAV Inbound Invoice Import.

Why Graph and not an ERPNext Email Account (IMAP): on the original Microsoft 365 tenant the IMAP+OAuth
login is refused by Exchange ("User is authenticated but not connected" / "Login failed") although
the token, the scopes and the mailbox settings are all correct (checked 2026-10-02). Graph access
to the same mailbox works, so the app reads the folder itself:

  NAV Connection Settings (Számlakép e-mailből):
    pdf_mail_connected_app  Connected App with scopes offline_access + https://graph.microsoft.com/Mail.ReadWrite
    pdf_mail_user           the ERPNext user whose mailbox is read (must Authorize the Connected App once)
    pdf_mail_folder         child folder of Inbox, e.g. "ERP Szamlak" (an Outlook rule fills it)
    pdf_mail_delete_after   delete the mail once its PDF is attached (so the folder does not pile up)

Every 10 minutes (hooks.py): list the folder's messages with attachments, take the PDF attachments,
run the same strict matching as invoice_pdf.pick_import (invoice number in subject / file name, or
in the PDF text with the supplier confirmed), attach the PDF to the NAV import (and its Purchase
Invoice), then delete the message. Unmatched mails stay in the folder and are retried next time —
the NAV data usually arrives within the hour.
"""

from __future__ import annotations

import base64
import io
import re

import frappe
import requests
from frappe.utils import now_datetime

from hesteron_nav_sync.invoice_pdf import IMPORT_DT, _supplier_email_domains, attach_copy, pick_import

GRAPH = "https://graph.microsoft.com/v1.0"
TIMEOUT = 60
MAX_MESSAGES = 50


def _settings():
	return frappe.get_single("NAV Connection Settings")


def _bearer(settings) -> str | None:
	app = frappe.get_doc("Connected App", settings.pdf_mail_connected_app)
	token = app.get_active_token(settings.pdf_mail_user)
	if not token:
		return None
	return token.get_password("access_token")


class Graph:
	def __init__(self, bearer: str):
		self.h = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}

	def get(self, path: str, **params):
		url = path if path.startswith("http") else GRAPH + path
		r = requests.get(url, headers=self.h, params=params or None, timeout=TIMEOUT)
		r.raise_for_status()
		return r.json()

	def delete(self, path: str):
		r = requests.delete(GRAPH + path, headers=self.h, timeout=TIMEOUT)
		if r.status_code not in (204, 404):
			r.raise_for_status()

	def folder_id(self, name: str) -> str | None:
		name = (name or "").strip()
		for page in self._pages("/me/mailFolders/inbox/childFolders", {"$top": 100, "$select": "id,displayName"}):
			for f in page:
				if (f.get("displayName") or "").strip().lower() == name.lower():
					return f["id"]
		return None

	def messages(self, folder_id: str):
		# Graph rejects $filter + $orderby on different properties (400), so no $orderby here:
		# we sort by receivedDateTime ourselves.
		params = {
			"$filter": "hasAttachments eq true",
			"$select": "id,subject,from,sender,receivedDateTime,hasAttachments",
			"$top": MAX_MESSAGES,
		}
		out = []
		for page in self._pages(f"/me/mailFolders/{folder_id}/messages", params):
			out.extend(page)
			if len(out) >= MAX_MESSAGES:
				break
		out.sort(key=lambda m: m.get("receivedDateTime") or "")
		return out[:MAX_MESSAGES]

	def pdf_attachments(self, message_id: str):
		data = self.get(f"/me/messages/{message_id}/attachments", **{"$top": 50})
		for a in data.get("value", []):
			if a.get("@odata.type") != "#microsoft.graph.fileAttachment":
				continue
			name = a.get("name") or ""
			ctype = (a.get("contentType") or "").lower()
			if not (name.lower().endswith(".pdf") or ctype == "application/pdf"):
				continue
			raw = a.get("contentBytes")
			if not raw:
				raw = self.get(f"/me/messages/{message_id}/attachments/{a['id']}").get("contentBytes")
			if raw:
				yield name if name.lower().endswith(".pdf") else name + ".pdf", base64.b64decode(raw)

	def _pages(self, path, params):
		data = self.get(path, **params)
		while True:
			yield data.get("value", [])
			nxt = data.get("@odata.nextLink")
			if not nxt:
				break
			data = self.get(nxt)


# ---------------------------------------------------------------------------- job

def fetch_invoice_pdfs(settings=None) -> dict:
	"""Scheduler entry point (every 10 minutes) and the 'Számlakép-levelek beolvasása most' button."""
	settings = settings or _settings()
	if not settings.get("pdf_mail_enabled"):
		return {"skipped": "disabled"}
	if not (settings.pdf_mail_connected_app and settings.pdf_mail_user):
		return {"error": "Connected App vagy felhasználó nincs beállítva"}

	result = {"seen": 0, "attached": 0, "deleted": 0, "unmatched": 0, "errors": 0}
	try:
		bearer = _bearer(settings)
		if not bearer:
			raise frappe.ValidationError(
				f"Nincs érvényes Microsoft-belépés a(z) {settings.pdf_mail_user} felhasználónak — "
				f"a Connected App-on ({settings.pdf_mail_connected_app}) kell egyszer bejelentkezni (Authorize)."
			)
		g = Graph(bearer)
		folder = g.folder_id(settings.pdf_mail_folder or "ERP Szamlak")
		if not folder:
			raise frappe.ValidationError(f"Nincs ilyen mappa a Beérkezett üzenetek alatt: {settings.pdf_mail_folder}")

		candidates = _candidates()
		for msg in g.messages(folder):
			result["seen"] += 1
			try:
				placed = _process_message(g, msg, candidates)
			except Exception:
				result["errors"] += 1
				frappe.log_error(title="NAV számlakép (Graph): levél feldolgozási hiba", message=frappe.get_traceback())
				continue
			if placed:
				result["attached"] += placed
				if settings.get("pdf_mail_delete_after"):
					g.delete(f"/me/messages/{msg['id']}")
					result["deleted"] += 1
			else:
				result["unmatched"] += 1
	except Exception as e:
		result["error"] = str(e)[:500]
		frappe.log_error(title="NAV számlakép (Graph): hiba", message=frappe.get_traceback())

	frappe.db.set_single_value("NAV Connection Settings", {
		"pdf_mail_last_run": now_datetime(),
		"pdf_mail_last_result": frappe.as_json(result),
	})
	frappe.db.commit()
	return result


def _candidates():
	from hesteron_nav_sync.invoice_pdf import _since, LOOKBACK_DAYS

	rows = frappe.get_all(
		IMPORT_DT,
		filters={"invoice_issue_date": [">=", _since(LOOKBACK_DAYS)]},
		fields=["name", "nav_invoice_number", "supplier", "supplier_name", "supplier_tax_number"],
		limit=5000,
	)
	domains = _supplier_email_domains({r.supplier for r in rows if r.supplier})
	out = []
	for r in rows:
		d = dict(r)
		d["supplier_email_domains"] = domains.get(r.supplier, set())
		out.append(d)
	return out


def _process_message(g: Graph, msg: dict, candidates) -> int:
	"""Attach every PDF of the mail that matches exactly one NAV import. Returns the number attached."""
	sender = ((msg.get("from") or msg.get("sender") or {}).get("emailAddress") or {}).get("address")
	subject = msg.get("subject") or ""
	attached = 0
	pdfs = list(g.pdf_attachments(msg["id"]))
	if not pdfs:
		return 0
	for file_name, content in pdfs:
		target = pick_import(f"{subject} {file_name}", pdf_text(content), sender, candidates)
		if not target:
			continue
		f = _save_pdf(file_name, content, IMPORT_DT, target)
		if f is None:  # already there
			attached += 1
			continue
		pi = frappe.db.get_value(IMPORT_DT, target, "linked_purchase_invoice")
		if pi:
			attach_copy(f, "Purchase Invoice", pi)
		frappe.get_doc(IMPORT_DT, target).add_comment(
			"Info",
			"Számlakép automatikusan csatolva az e-mailből: {} ({})".format(
				frappe.utils.escape_html(file_name), frappe.utils.escape_html(sender or "")
			),
		)
		attached += 1
	# only delete when EVERY pdf of the mail found its place (a mail may carry several invoices)
	return attached if attached == len(pdfs) else 0


def _save_pdf(file_name: str, content: bytes, doctype: str, name: str):
	safe = re.sub(r"[^\w.\-]+", "_", file_name)[:140] or "szamla.pdf"
	if frappe.db.exists("File", {"attached_to_doctype": doctype, "attached_to_name": name, "file_name": safe}):
		return None
	f = frappe.get_doc({
		"doctype": "File",
		"file_name": safe,
		"content": content,
		"is_private": 1,
		"attached_to_doctype": doctype,
		"attached_to_name": name,
	})
	f.insert(ignore_permissions=True)
	return f


def pdf_text(content: bytes, max_pages: int = 4) -> str:
	try:
		try:
			from pypdf import PdfReader
		except ImportError:
			from PyPDF2 import PdfReader
		reader = PdfReader(io.BytesIO(content))
		return "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])
	except Exception:
		return ""
