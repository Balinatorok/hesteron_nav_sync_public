// Form behaviour for NAV Inbound Invoice Import — the "review & record" screen.
//
// Every line has a "Cél": Készletre (-> Purchase Receipt, needs an Item) or a cost target
// (Költség / Saját felhasználás / Beszerzési járulék -> straight onto the Purchase Invoice with an
// expense category, no Item). The common cases are one click:
//   all stock lines matched   -> "Bevételezés"
//   only cost lines           -> "Beszerzési számla"
//   a supplier that is always a cost (courier, bookkeeper) -> "Szállító alapértelmezése" once.

const COST_TARGETS = ["Költség", "Saját felhasználás", "Beszerzési járulék"];
const is_cost = (r) => COST_TARGETS.includes(r.target);
const active = (frm) => (frm.doc.lines || []).filter((r) => r.match_status !== "Kihagy");

frappe.ui.form.on("NAV Inbound Invoice Import", {
	refresh(frm) {
		frm.disable_save_on_submit = false;
		render_summary(frm);

		if (frm.is_new()) return;

		frm.add_custom_button(__("Egyeztetés újra"), () => {
			frm.call("rerun_matching").then(() => frm.reload_doc());
		}, __("Műveletek"));

		const suggested = (frm.doc.lines || []).filter((r) => r.match_status === "Javaslat" && r.item_code);
		if (suggested.length) {
			frm.add_custom_button(__("Javaslatok elfogadása ({0})", [suggested.length]), () => {
				frm.call("confirm_all_suggestions").then(() => frm.reload_doc());
			}, __("Műveletek"));
		}

		if (frm.doc.supplier) {
			frm.add_custom_button(__("Szállító alapértelmezése…"), () => supplier_default_dialog(frm), __("Műveletek"));
		}

		const has_stock = active(frm).some((r) => !is_cost(r));
		const has_cost = active(frm).some(is_cost);
		const guard = () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Előbb mentsd el a változtatásokat."));
				return false;
			}
			return true;
		};

		if (has_stock && !frm.doc.linked_purchase_receipt && !frm.doc.is_modification) {
			const btn = frm.add_custom_button(__("Bevételezés (Purchase Receipt)"), () => {
				if (!guard()) return;
				frappe.confirm(
					__("Létrehozom a bevételezés piszkozatát a készletre menő sorokból?"),
					() => frm.call("create_purchase_receipt").then((r) => {
						if (r.message) frappe.set_route("Form", "Purchase Receipt", r.message);
					})
				);
			});
			if (frm.doc.status === "Bevételezhető") btn.addClass("btn-primary");
		}

		const can_invoice = !frm.doc.linked_purchase_invoice && !frm.doc.is_modification
			&& ((has_stock && frm.doc.linked_purchase_receipt) || (!has_stock && has_cost));
		if (can_invoice) {
			const label = has_stock ? __("Beszerzési számla a bevételezésből") : __("Beszerzési számla (költség)");
			const btn = frm.add_custom_button(label, () => {
				if (!guard()) return;
				frm.call("create_purchase_invoice").then((r) => {
					if (r.message) frappe.set_route("Form", "Purchase Invoice", r.message);
				});
			});
			if (!has_stock && frm.doc.status === "Rögzíthető") btn.addClass("btn-primary");
		}

		if (!frm.doc.supplier && frm.doc.supplier_tax_number) {
			const btn = frm.add_custom_button(__("Szállító felvétele egy kattintással"), () => new_supplier_dialog(frm));
			btn.addClass("btn-primary");
		}
	},
});

frappe.ui.form.on("NAV Inbound Invoice Import Line", {
	item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (is_cost(row)) return;
		if (row.item_code) {
			frappe.model.set_value(cdt, cdn, { match_status: "Egyeztetve", match_method: "manual", match_confidence: 100 });
		} else {
			frappe.model.set_value(cdt, cdn, { match_status: "Új cikk", match_confidence: 0 });
		}
	},

	target(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const patch = { target_source: "kézi" };
		if (is_cost(row)) {
			Object.assign(patch, {
				item_code: null,
				match_method: "category",
				match_status: row.expense_category ? "Egyeztetve" : "Kategória kell",
			});
		} else {
			Object.assign(patch, {
				expense_category: null,
				match_method: row.item_code ? "manual" : "none",
				match_status: row.item_code ? "Egyeztetve" : "Új cikk",
			});
		}
		frappe.model.set_value(cdt, cdn, patch);
	},

	expense_category(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, {
			target_source: "kézi",
			match_status: row.expense_category ? "Egyeztetve" : "Kategória kell",
		});
	},

	lines_add(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, { match_status: "Új cikk", match_method: "manual", target: "Készletre" });
	},

	form_render(frm, cdt, cdn) {
		// Row detail view: "Új cikk létrehozása" pre-filled from the NAV line (stock lines only).
		const row = locals[cdt][cdn];
		const grid_row = frm.fields_dict.lines.grid.grid_rows_by_docname[cdn];
		if (!grid_row || row.item_code || is_cost(row)) return;
		if (grid_row.wrapper.find(".hns-new-item").length) return;
		const $btn = $(`<button class="btn btn-xs btn-default hns-new-item" style="margin:6px 0 12px">${__("Új cikk létrehozása ebből a sorból")}</button>`);
		grid_row.wrapper.find(".grid-form-body .form-area").first().prepend($btn);
		$btn.on("click", () => {
			// cikkszám = the supplier's product code from NAV (first one, if several)
			const nav_code = (row.nav_product_code || "").split(",")[0].trim();
			const defaults = {
				item_code: nav_code || undefined,
				item_name: (row.nav_description || "").slice(0, 140),
				description: row.nav_description,
				item_group: row.suggested_item_group || undefined,
				brand: row.suggested_brand || undefined,
				stock_uom: row.uom || "db",
				is_stock_item: row.nature === "SERVICE" ? 0 : 1,
				standard_rate: 0,
			};
			frappe.new_doc("Item", defaults).then(() => {
				frappe.show_alert({ message: nav_code
					? __("Cikkszám a NAV-ból: {0} — átírható. Mentés után válaszd ki az új cikket a soron, majd 'Bevételezés'.", [nav_code])
					: __("Mentés után válaszd ki az új cikket a soron, majd 'Bevételezés'."), indicator: "blue" }, 8);
			});
		});
	},
});

function new_supplier_dialog(frm) {
	// Unknown supplier: name + tax number (+ address from NAV) are filled in automatically;
	// the only question is whether its invoices are goods or a cost — both optional.
	const d = new frappe.ui.Dialog({
		title: __("Új szállító: {0}", [frm.doc.supplier_name || frm.doc.supplier_tax_number]),
		fields: [
			{ fieldtype: "HTML", options: `<p class="text-muted">${__("Adószám")}: <b>${frappe.utils.escape_html(frm.doc.supplier_tax_number || "")}</b>. ${__("A nevet és a címet a NAV adataiból töltöm ki.")}</p>` },
			{
				fieldname: "target", label: __("Mit szokott számlázni? (nem kötelező)"), fieldtype: "Select",
				options: ["", "Készletre", "Költség", "Saját felhasználás", "Beszerzési járulék"].join("\n"),
				description: __("Ha megadod, a szállító minden számlája ezzel indul — soronként átírható."),
			},
			{
				fieldname: "expense_category", label: __("Költségkategória"), fieldtype: "Link", options: "NAV Expense Category",
				depends_on: "eval:doc.target && doc.target != 'Készletre'",
				mandatory_depends_on: "eval:doc.target && doc.target != 'Készletre'",
			},
		],
		primary_action_label: __("Felvétel és egyeztetés"),
		primary_action(values) {
			frm.call("create_supplier", values).then((r) => {
				d.hide();
				const m = r.message || {};
				frappe.show_alert({
					message: m.created
						? __("Szállító felvéve: {0}. További {1} számlája is újraegyeztetve.", [m.supplier, m.also_updated || 0])
						: __("Már létezett: {0} — hozzákötve.", [m.supplier]),
					indicator: "green",
				}, 7);
				frm.reload_doc();
			});
		},
	});
	d.show();
}

function supplier_default_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("{0} — NAV számlák alapértelmezése", [frm.doc.supplier]),
		fields: [
			{
				fieldname: "target", label: __("Alapértelmezett cél"), fieldtype: "Select",
				options: ["", "Készletre", "Költség", "Saját felhasználás", "Beszerzési járulék"].join("\n"),
				description: __("Ettől a szállítótól jövő minden sor ezzel indul. Soronként mindig átírható."),
			},
			{
				fieldname: "expense_category", label: __("Költségkategória"), fieldtype: "Link", options: "NAV Expense Category",
				depends_on: "eval:doc.target && doc.target != 'Készletre'",
				mandatory_depends_on: "eval:doc.target && doc.target != 'Készletre'",
			},
		],
		primary_action_label: __("Beállítás és újraegyeztetés"),
		primary_action(values) {
			frm.call("set_supplier_default", values).then((r) => {
				d.hide();
				const n = (r.message && r.message.also_updated) || 0;
				frappe.show_alert({
					message: n ? __("Beállítva. A szállító további {0} nyitott számlája is újraegyeztetve.", [n]) : __("Beállítva."),
					indicator: "green",
				}, 7);
				frm.reload_doc();
			});
		},
	});
	frappe.db.get_value("Supplier", frm.doc.supplier, ["nav_default_target", "nav_expense_category"]).then((r) => {
		if (r.message) d.set_values({ target: r.message.nav_default_target || "", expense_category: r.message.nav_expense_category || "" });
		d.show();
	});
}

function render_summary(frm) {
	const lines = frm.doc.lines || [];
	const count = (s) => lines.filter((r) => r.match_status === s).length;
	const serial = lines.filter((r) => r.has_serial_no && r.match_status !== "Kihagy").length;
	const cost = lines.filter((r) => is_cost(r) && r.match_status !== "Kihagy").length;
	const parts = [
		`<span class="indicator-pill green">${__("Egyeztetve")}: ${count("Egyeztetve")}</span>`,
		`<span class="indicator-pill orange">${__("Javaslat")}: ${count("Javaslat")}</span>`,
		`<span class="indicator-pill red">${__("Új cikk")}: ${count("Új cikk")}</span>`,
	];
	if (count("Kategória kell")) parts.push(`<span class="indicator-pill red">${__("Kategória kell")}: ${count("Kategória kell")}</span>`);
	parts.push(`<span class="indicator-pill gray">${__("Kihagy")}: ${count("Kihagy")}</span>`);
	if (cost) parts.push(`<span class="indicator-pill purple">${__("Költségsor")}: ${cost}</span>`);
	if (serial) parts.push(`<span class="indicator-pill blue">${__("Sorozatszámos sor")}: ${serial}</span>`);
	frm.dashboard.clear_headline();
	frm.dashboard.set_headline(parts.join(" &nbsp; "));
	if (frm.doc.error_message && !["Bevételezés létrehozva", "Számla létrehozva"].includes(frm.doc.status)) {
		frm.dashboard.add_comment(frm.doc.error_message, "yellow", true);
	}
}
