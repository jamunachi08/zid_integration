"""End-to-end test: mocked ZID API -> real Sales Invoice on a test site.

    bench --site <test-site> set-config allow_tests true
    bench --site <test-site> run-tests --module zid_integration.tests.test_sync

Creates its own items, tax template and customers in the site's default company.
Do NOT run on a production site.
"""

import copy
from collections import Counter
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate

from zid_integration import sync
from zid_integration.tests.test_mapper import SAMPLE_ORDER


class FakeClient:
	orders: dict = {}

	def __init__(self, settings=None):
		pass

	def ensure_token(self):
		pass

	def get_order(self, order_id):
		return copy.deepcopy(self.orders[str(order_id)])

	def iter_orders(self, date_from, date_to, per_page=50):
		for oid in self.orders:
			yield {"id": oid}


def make_order(order_id, **changes):
	order = copy.deepcopy(SAMPLE_ORDER)
	order["id"] = order_id
	day = add_days(nowdate(), -1)
	order["issue_date"] = frappe.utils.getdate(day).strftime("%d-%m-%Y")
	order["created_at"] = f"{day} 10:15:00"
	order.update(changes)
	return order


class TestZidSync(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.get_all(
			"Company", pluck="name", limit=1
		)[0]
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr")
		cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 0}, "name")
		cls.cost_center = frappe.get_cached_value("Company", cls.company, "cost_center")
		cls._setup_masters()
		cls._setup_settings()
		frappe.db.commit()

	@classmethod
	def _setup_masters(cls):
		company, abbr = cls.company, cls.abbr
		# VAT account + inclusive template
		vat_account = f"ZID VAT 15% - {abbr}"
		if not frappe.db.exists("Account", vat_account):
			parent = frappe.db.get_value(
				"Account", {"company": company, "is_group": 1, "root_type": "Liability", "account_type": "Tax"}
			) or frappe.db.get_value("Account", {"company": company, "is_group": 1, "root_type": "Liability"})
			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": "ZID VAT 15%",
					"parent_account": parent,
					"company": company,
					"account_type": "Tax",
					"tax_rate": 15,
				}
			).insert()
		cls.tax_template = f"ZID VAT 15% - {abbr}"
		if not frappe.db.exists("Sales Taxes and Charges Template", cls.tax_template):
			frappe.get_doc(
				{
					"doctype": "Sales Taxes and Charges Template",
					"title": "ZID VAT 15%",
					"company": company,
					"taxes": [
						{
							"charge_type": "On Net Total",
							"account_head": vat_account,
							"description": "VAT 15%",
							"rate": 15,
						}
					],
				}
			).insert()

		def item(code, stock=1, barcode=None):
			if frappe.db.exists("Item", code):
				return
			doc = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": code,
					"item_group": frappe.db.get_value("Item Group", {"is_group": 0}) or "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": stock,
					"valuation_rate": 50,
				}
			)
			if barcode:
				doc.append("barcodes", {"barcode": barcode})
			doc.insert()

		item("SKU-001")
		item("ZID-SOCKS", barcode="SKU-002")  # matched through Item Barcode
		item("ZID Shipping", stock=0)

		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": company,
				"posting_date": add_days(nowdate(), -5),
				"set_posting_time": 1,
				"items": [
					{"item_code": c, "qty": 50, "basic_rate": 50, "t_warehouse": cls.warehouse}
					for c in ("SKU-001", "ZID-SOCKS")
				],
			}
		)
		se.insert()
		se.submit()

		group = frappe.db.get_value("Customer Group", {"is_group": 0})
		territory = frappe.db.get_value("Territory", {"is_group": 0})
		for name, mobile in (("ZID Online Customer", None), ("ZID Test Ahmed", "0501234567")):
			if not frappe.db.exists("Customer", name):
				c = frappe.get_doc(
					{"doctype": "Customer", "customer_name": name, "customer_group": group, "territory": territory}
				).insert()
				if mobile:
					c.db_set("mobile_no", mobile)

		cash = frappe.get_cached_value("Company", company, "default_cash_account")
		for mop in ("Cash", "Credit Card"):
			if not frappe.db.exists("Mode of Payment", mop):
				frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": mop, "type": "Cash"}).insert()
			doc = frappe.get_doc("Mode of Payment", mop)
			if not any(a.company == company for a in doc.accounts):
				doc.append("accounts", {"company": company, "default_account": cash})
				doc.save()

	@classmethod
	def _setup_settings(cls):
		s = frappe.get_doc("ZID Settings")
		s.update(
			{
				"enabled": 0,
				"company": cls.company,
				"invoice_action": "Submit",
				"default_customer": "ZID Online Customer",
				"match_by_zid_customer_id": 1,
				"match_by_mobile": 1,
				"match_by_email": 1,
				"cost_center": cls.cost_center,
				"update_stock": 1,
				"warehouse": cls.warehouse,
				"taxes_and_charges": cls.tax_template,
				"prices_include_vat": 1,
				"coupon_apply_on": "Net Total",
				"shipping_item": "ZID Shipping",
				"shipping_includes_vat": 1,
				"total_tolerance": 0.05,
				"on_total_mismatch": "Fail",
				"default_mode_of_payment": "Cash",
				"unpaid_orders": "Record POS Payment",
				"skip_cancelled_orders": 1,
				"payment_method_mapping": [{"zid_payment_method": "Credit Card", "mode_of_payment": "Credit Card"}],
			}
		)
		s.save()

	def run_orders(self, *orders):
		currency = frappe.get_cached_value("Company", self.company, "default_currency")
		for o in orders:
			o["currency_code"] = currency
		FakeClient.orders = {str(o["id"]): o for o in orders}
		with patch.object(sync, "ZidClient", FakeClient):
			return sync.sync_orders()

	def log(self, order_id):
		return frappe.get_doc("ZID Order Log", str(order_id))

	# ------------------------------------------------------------------ tests

	def test_01_import_submitted_pos_invoice(self):
		summary = self.run_orders(make_order(9000001))
		log = self.log(9000001)
		self.assertEqual(log.status, "Imported", log.message)
		si = frappe.get_doc("Sales Invoice", log.sales_invoice)
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(si.is_pos, 1)
		self.assertEqual(si.customer, "ZID Test Ahmed")  # matched by mobile
		self.assertEqual(si.zid_order_id, "9000001")
		self.assertEqual(flt(si.grand_total, 2), 287.5)
		self.assertEqual(flt(si.total_taxes_and_charges, 2), 37.5)
		self.assertEqual(flt(si.discount_amount, 2), 20)
		self.assertEqual(flt(si.outstanding_amount, 2), 0)
		self.assertEqual([p.mode_of_payment for p in si.payments], ["Credit Card"])
		self.assertEqual([i.item_code for i in si.items], ["SKU-001", "ZID-SOCKS", "ZID Shipping"])
		self.assertTrue(frappe.db.exists("Stock Ledger Entry", {"voucher_no": si.name, "is_cancelled": 0}))
		self.assertIn("imported 1", summary)

	def test_02_no_duplicates(self):
		self.run_orders(make_order(9000001))
		self.assertEqual(frappe.db.count("Sales Invoice", {"zid_order_id": "9000001"}), 1)

	def test_03_unknown_customer_uses_default(self):
		order = make_order(9000002, customer={"id": 1, "name": "New Buyer", "email": "x@y.z", "mobile": "0599999999"})
		self.run_orders(order)
		log = self.log(9000002)
		self.assertEqual(log.status, "Imported", log.message)
		self.assertEqual(frappe.db.get_value("Sales Invoice", log.sales_invoice, "customer"), "ZID Online Customer")
		self.assertEqual(frappe.db.get_value("Sales Invoice", log.sales_invoice, "zid_customer_name"), "New Buyer")

	def test_04_unknown_sku_fails(self):
		order = make_order(9000003)
		order["products"][0]["sku"] = "DOES-NOT-EXIST"
		self.run_orders(order)
		log = self.log(9000003)
		self.assertEqual(log.status, "Failed")
		self.assertIn("DOES-NOT-EXIST", log.message)
		self.assertFalse(frappe.db.exists("Sales Invoice", {"zid_order_id": "9000003"}))

	def test_05_cancelled_skipped(self):
		self.run_orders(make_order(9000004, order_status={"code": "cancelled"}))
		self.assertEqual(self.log(9000004).status, "Skipped")

	def test_06_total_mismatch_fails(self):
		order = make_order(9000005)
		for row in order["payment"]["invoice"]:
			if row["code"] == "total":
				row["value"] = "300.00"
		self.run_orders(order)
		log = self.log(9000005)
		self.assertEqual(log.status, "Failed")
		self.assertIn("does not match", log.message)

	def test_07_draft_mode_and_default_mop(self):
		frappe.db.set_single_value("ZID Settings", "invoice_action", "Draft")
		try:
			order = make_order(9000006)
			order["payment"]["method"] = {"name": "Apple Pay"}  # unmapped -> default Cash
			self.run_orders(order)
			log = self.log(9000006)
			self.assertEqual(log.status, "Imported", log.message)
			si = frappe.get_doc("Sales Invoice", log.sales_invoice)
			self.assertEqual(si.docstatus, 0)
			self.assertEqual(si.payments[0].mode_of_payment, "Cash")
			si.submit()  # a reviewer can submit the draft as-is
		finally:
			frappe.db.set_single_value("ZID Settings", "invoice_action", "Submit")

	def test_08_retry_failed_after_fix(self):
		order = make_order(9000007)
		order["products"][0]["sku"] = "SKU-LATER"
		self.run_orders(order)
		self.assertEqual(self.log(9000007).status, "Failed")
		item = frappe.get_doc("Item", "SKU-001")
		item.append("barcodes", {"barcode": "SKU-LATER"})
		item.save()
		with patch.object(sync, "ZidClient", FakeClient):
			sync.process_order("9000007", FakeClient(), sync.get_settings(), Counter(), force=True)
		self.assertEqual(self.log(9000007).status, "Imported")
