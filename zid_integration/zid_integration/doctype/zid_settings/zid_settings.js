frappe.ui.form.on("ZID Settings", {
	onload(frm) {
		const params = new URLSearchParams(window.location.search);
		if (params.get("zid_oauth")) {
			frappe.msgprint({
				title: __("ZID"),
				message: params.get("message") || params.get("zid_oauth"),
				indicator: params.get("zid_oauth") === "success" ? "green" : "red",
			});
			window.history.replaceState({}, "", window.location.pathname);
		}
	},

	refresh(frm) {
		if (!frm.doc.redirect_uri) {
			frm.set_df_property(
				"redirect_uri",
				"description",
				__("Register this Redirect URL in your ZID app: {0}", [
					`<code>${window.location.origin}/api/method/zid_integration.oauth.callback</code>`,
				])
			);
		}

		frm.add_custom_button(__("Connect to ZID"), () => {
			frappe.call("zid_integration.oauth.get_authorize_url").then((r) => {
				if (r.message) window.location.href = r.message;
			});
		}, __("Connection"));

		frm.add_custom_button(__("Test Connection"), () => {
			frappe.call({ method: "zid_integration.sync.test_connection", freeze: true }).then((r) =>
				frappe.msgprint(r.message)
			);
		}, __("Connection"));

		frm.add_custom_button(__("Refresh Token"), () => {
			frappe.call({ method: "zid_integration.sync.refresh_token_now", freeze: true }).then((r) => {
				frappe.show_alert({ message: r.message, indicator: "green" });
				frm.reload_doc();
			});
		}, __("Connection"));

		frm.add_custom_button(__("Sync Now"), () => {
			const d = new frappe.ui.Dialog({
				title: __("Sync ZID Orders"),
				fields: [
					{ fieldname: "from_date", fieldtype: "Date", label: __("From Date"),
					  description: __("Leave blank to use the configured date range") },
					{ fieldname: "to_date", fieldtype: "Date", label: __("To Date") },
				],
				primary_action_label: __("Start"),
				primary_action(values) {
					d.hide();
					frappe.call("zid_integration.sync.sync_now", values).then((r) =>
						frappe.show_alert({ message: r.message, indicator: "blue" })
					);
				},
			});
			d.show();
		}, __("Sync"));

		frm.add_custom_button(__("Import One Order"), () => {
			frappe.prompt(
				{ fieldname: "order_id", fieldtype: "Data", label: __("ZID Order ID"), reqd: 1 },
				(v) => {
					frappe.call({
						method: "zid_integration.sync.import_single_order",
						args: { order_id: v.order_id },
						freeze: true,
						freeze_message: __("Importing..."),
					}).then((r) => {
						const log = r.message || {};
						frappe.msgprint({
							title: __("Order {0}: {1}", [v.order_id, log.status]),
							message: (log.sales_invoice
								? `<a href="/app/sales-invoice/${log.sales_invoice}">${log.sales_invoice}</a><br>`
								: "") + frappe.utils.escape_html(log.message || ""),
							indicator: log.status === "Imported" ? "green" : log.status === "Failed" ? "red" : "orange",
						});
					});
				},
				__("Import ZID Order")
			);
		}, __("Sync"));

		frm.add_custom_button(__("Retry Failed Orders"), () => {
			frappe.call("zid_integration.sync.retry_failed").then((r) =>
				frappe.show_alert({ message: r.message, indicator: "blue" })
			);
		}, __("Sync"));

		frm.add_custom_button(__("Order Log"), () => frappe.set_route("List", "ZID Order Log"));

		frm.set_query("warehouse", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("cost_center", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("taxes_and_charges", () => ({ filters: { company: frm.doc.company } }));
		frm.set_query("pos_profile", () => ({ filters: { company: frm.doc.company } }));
		frm.set_query("shipping_item", () => ({ filters: { is_stock_item: 0, disabled: 0 } }));
		frm.set_query("fallback_item", () => ({ filters: { disabled: 0, has_variants: 0 } }));
	},
});
