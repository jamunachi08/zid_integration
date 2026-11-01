frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (frm.doc.zid_order_url) {
			frm.add_custom_button(__("Open in ZID"), () => window.open(frm.doc.zid_order_url, "_blank"), __("View"));
		}
		if (frm.doc.zid_order_id) {
			frm.add_custom_button(__("ZID Order Log"), () =>
				frappe.set_route("Form", "ZID Order Log", frm.doc.zid_order_id), __("View"));
		}
	},
});
