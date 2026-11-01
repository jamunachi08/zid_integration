app_name = "zid_integration"
app_title = "ZID Integration"
app_publisher = "PACT"
app_description = "Import ZID e-commerce orders into ERPNext as POS Sales Invoices"
app_email = "jamunachi007@gmail.com"
app_license = "mit"

required_apps = ["erpnext"]

# Installation
after_install = "zid_integration.install.after_install"
after_migrate = "zid_integration.install.after_migrate"

# Scheduled tasks
# The tick runs every 5 minutes; the real interval is controlled by
# "Sync Interval (Minutes)" in ZID Settings.
scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"zid_integration.sync.scheduled_sync",
		],
	},
}

doctype_js = {
	"Sales Invoice": "public/js/sales_invoice.js",
}
