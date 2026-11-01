frappe.listview_settings["ZID Order Log"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = { Imported: "green", Failed: "red", Skipped: "gray", Pending: "orange" };
		return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
	},
	onload(listview) {
		listview.page.add_inner_button(__("Retry Failed"), () =>
			frappe.call("zid_integration.sync.retry_failed").then((r) =>
				frappe.show_alert({ message: r.message, indicator: "blue" })
			)
		);
	},
};
