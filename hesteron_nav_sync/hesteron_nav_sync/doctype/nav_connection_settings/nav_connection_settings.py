import frappe
from frappe import _
from frappe.model.document import Document


class NAVConnectionSettings(Document):
	def validate(self):
		if self.tax_number:
			self.tax_number = "".join(ch for ch in self.tax_number if ch.isdigit())[:8]
		if self.software_id:
			from hesteron_nav_sync.nav_client.client import SoftwareInfo

			SoftwareInfo(software_id=self.software_id).validate()
		elif self.tax_number:
			self.software_id = f"HU{self.tax_number}NAVSYNC1"

	def get_credentials(self):
		from hesteron_nav_sync.nav_client.client import NavCredentials

		return NavCredentials(
			login=self.technical_user_login,
			password=self.get_password("technical_user_password"),
			sign_key=self.get_password("sign_key"),
			exchange_key=self.get_password("exchange_key"),
			tax_number=self.tax_number,
		)

	def get_software_info(self):
		from hesteron_nav_sync.nav_client.client import SoftwareInfo

		return SoftwareInfo(
			software_id=self.software_id,
			software_name=self.software_name or "HesteronNavSync",
			software_main_version=self.software_main_version or "1.0",
			software_dev_name=self.software_dev_name or "Hesteron Kft.",
			software_dev_contact=self.software_dev_contact or "info@hesteron.hu",
		)

	def get_client(self):
		from hesteron_nav_sync.nav_client.client import NavInvoiceClient

		return NavInvoiceClient(
			credentials=self.get_credentials(),
			software=self.get_software_info(),
			environment="production" if self.environment == "Production" else "test",
		)

	@frappe.whitelist()
	def test_connection(self):
		"""Button on the settings form: proves login + password + signing key with a read-only call.
		(tokenExchange needs the invoice-*issuing* permission, which a query-only technical user
		rightly does not have -> it answered FORBIDDEN on the live site although syncing works.)"""
		info = self.get_client().query_taxpayer(self.tax_number)
		name = (info or {}).get("name") or self.tax_number
		return _("Sikeres kapcsolat a NAV-val ({0}).").format(name)

	@frappe.whitelist()
	def run_sync_now(self):
		from hesteron_nav_sync.sync import run_sync

		return run_sync(manual=True)

	@frappe.whitelist()
	def fetch_invoice_pdfs_now(self):
		"""Button: read the invoice-PDF mail folder (Microsoft Graph) right now."""
		from hesteron_nav_sync.graph_mail import fetch_invoice_pdfs

		return fetch_invoice_pdfs(self)
