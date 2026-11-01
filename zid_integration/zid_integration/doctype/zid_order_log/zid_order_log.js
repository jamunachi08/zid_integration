frappe.ui.form.on("ZID Order Log", {
	refresh(frm) {
		if (frm.doc.status !== "Imported") {
			frm.add_custom_button(__("Retry Import"), () => {
				frappe.call({
					method: "zid_integration.sync.retry_order",
					args: { log_name: frm.doc.name },
					freeze: true,
					freeze_message: __("Importing..."),
				}).then(() => frm.reload_doc());
			});
		}
		if (frm.doc.sales_invoice) {
			frm.add_custom_button(__("Open Sales Invoice"), () =>
				frappe.set_route("Form", "Sales Invoice", frm.doc.sales_invoice)
			);
		}
	},
});
