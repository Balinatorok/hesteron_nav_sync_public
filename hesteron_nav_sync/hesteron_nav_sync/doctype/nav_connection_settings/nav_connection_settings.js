frappe.ui.form.on("NAV Connection Settings", {
	refresh(frm) {
		if (frm.doc.pdf_mail_enabled) {
			frm.add_custom_button(__("Számlakép-levelek beolvasása most"), () => {
				frm.call("fetch_invoice_pdfs_now").then((r) => {
					frappe.msgprint(__("Kész: {0}", [JSON.stringify(r.message)]));
					frm.reload_doc();
				});
			});
		}
		frm.add_custom_button(__("Kapcsolat tesztelése"), () => {
			frm.call("test_connection").then((r) => frappe.msgprint(r.message));
		});
		frm.add_custom_button(__("Szinkron futtatása most"), () => {
			frappe.confirm(__("Lekérdezem a NAV-tól az új bejövő számlákat?"), () => {
				frm.call("run_sync_now").then((r) => {
					frappe.msgprint(__("Kész: {0}", [JSON.stringify(r.message)]));
					frm.reload_doc();
				});
			});
		});
	},
});
