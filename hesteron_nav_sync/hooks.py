from . import __version__ as app_version

app_name = "hesteron_nav_sync"
app_title = "NAV Online Számla Sync"
app_publisher = "NAV Sync"
app_description = "NAV Online Számla bejövő számlák -> cikk-egyeztetés -> bevételezés (Purchase Receipt) ERPNextben."
app_icon = "octicon octicon-repo-pull"
app_color = "green"
app_email = ""
app_license = "mit"

after_install = "hesteron_nav_sync.install.after_install"
after_migrate = "hesteron_nav_sync.install.after_migrate"

scheduler_events = {
	"cron": {
		# hourly at :07 — keep it off the top of the hour so it doesn't queue behind every other app
		"7 * * * *": ["hesteron_nav_sync.tasks.sync_inbound_invoices"],
		# every 10 minutes: supplier invoice PDFs from the Outlook folder (Microsoft Graph)
		"*/10 * * * *": ["hesteron_nav_sync.tasks.fetch_invoice_pdf_mails"],
	}
}

# This app never submits a Purchase Receipt / Invoice by itself. The only doc_event: e-mailed
# supplier invoice PDFs (File attached to a received Communication) are placed on the NAV import.
doc_events = {
	"File": {
		"after_insert": "hesteron_nav_sync.invoice_pdf.on_file_insert",
	},
}
