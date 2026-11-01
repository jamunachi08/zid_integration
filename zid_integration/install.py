import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def get_custom_fields():
	"""Fields that replace Pact's dcAlpha30..46 / dcNum text fields."""
	zid_section = "ZID"
	return {
		"Sales Invoice": [
			{
				"fieldname": "zid_section",
				"label": zid_section,
				"fieldtype": "Section Break",
				"insert_after": "remarks",
				"collapsible": 1,
			},
			{
				"fieldname": "zid_order_id",
				"label": "ZID Order ID",
				"fieldtype": "Data",
				"insert_after": "zid_section",
				"read_only": 1,
				"no_copy": 1,
				"search_index": 1,
				"in_standard_filter": 1,
				"allow_on_submit": 0,
			},
			{
				"fieldname": "zid_invoice_number",
				"label": "ZID Invoice Number",
				"fieldtype": "Data",
				"insert_after": "zid_order_id",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_order_url",
				"label": "ZID Order URL",
				"fieldtype": "Data",
				"options": "URL",
				"insert_after": "zid_invoice_number",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_invoice_link",
				"label": "ZID Invoice Link",
				"fieldtype": "Data",
				"options": "URL",
				"insert_after": "zid_order_url",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_order_status",
				"label": "ZID Order Status",
				"fieldtype": "Data",
				"insert_after": "zid_invoice_link",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_payment_status",
				"label": "ZID Payment Status",
				"fieldtype": "Data",
				"insert_after": "zid_order_status",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_payment_method",
				"label": "ZID Payment Method",
				"fieldtype": "Data",
				"insert_after": "zid_payment_status",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_column_break",
				"fieldtype": "Column Break",
				"insert_after": "zid_payment_method",
			},
			{
				"fieldname": "zid_customer_id",
				"label": "ZID Customer ID",
				"fieldtype": "Data",
				"insert_after": "zid_column_break",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_customer_name",
				"label": "ZID Customer Name",
				"fieldtype": "Data",
				"insert_after": "zid_customer_id",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_customer_email",
				"label": "ZID Customer Email",
				"fieldtype": "Data",
				"insert_after": "zid_customer_name",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_customer_mobile",
				"label": "ZID Customer Mobile",
				"fieldtype": "Data",
				"insert_after": "zid_customer_email",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_transaction_reference",
				"label": "ZID Transaction Reference",
				"fieldtype": "Data",
				"insert_after": "zid_customer_mobile",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_payment_network",
				"label": "ZID Payment Network",
				"fieldtype": "Data",
				"insert_after": "zid_transaction_reference",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_order_total",
				"label": "ZID Order Total",
				"fieldtype": "Currency",
				"options": "currency",
				"insert_after": "zid_payment_network",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_vat_amount",
				"label": "ZID VAT Amount",
				"fieldtype": "Currency",
				"options": "currency",
				"insert_after": "zid_order_total",
				"read_only": 1,
				"no_copy": 1,
			},
		],
		"Sales Invoice Item": [
			{
				"fieldname": "zid_order_product_id",
				"label": "ZID Order Product ID",
				"fieldtype": "Data",
				"insert_after": "description",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_sku",
				"label": "ZID SKU",
				"fieldtype": "Data",
				"insert_after": "zid_order_product_id",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_product_name",
				"label": "ZID Product Name",
				"fieldtype": "Data",
				"insert_after": "zid_sku",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "zid_coupon_unit_amount",
				"label": "ZID Coupon Discount / Unit",
				"fieldtype": "Currency",
				"options": "currency",
				"insert_after": "zid_product_name",
				"read_only": 1,
				"no_copy": 1,
			},
		],
		"Customer": [
			{
				"fieldname": "zid_customer_id",
				"label": "ZID Customer ID",
				"fieldtype": "Data",
				"insert_after": "customer_group",
				"unique": 1,
				"no_copy": 1,
				"search_index": 1,
				"description": "Used to match ZID orders to this customer",
			},
		],
	}


def setup_custom_fields():
	create_custom_fields(get_custom_fields(), ignore_validate=True, update=True)


def after_install():
	setup_custom_fields()


def after_migrate():
	setup_custom_fields()
	frappe.clear_cache(doctype="Sales Invoice")
