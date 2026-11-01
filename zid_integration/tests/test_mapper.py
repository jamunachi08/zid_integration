"""Unit tests for the pure mapping logic.

Run without a site:   python -m unittest zid_integration.tests.test_mapper
Run on a bench:       bench --site <site> run-tests --module zid_integration.tests.test_mapper
"""

import copy
import unittest
from datetime import datetime

from zid_integration import mapper

SAMPLE_ORDER = {
	"id": 41028001,
	"invoice_number": "INV-1001",
	"order_url": "https://web.zid.sa/orders/41028001",
	"store_name": "Tatweer Sports",
	"currency_code": "SAR",
	"customer": {"id": 555, "name": "Ahmed Ali", "email": "Ahmed@Example.com", "mobile": "+966 50 123 4567"},
	"transaction_reference": "TX-998",
	"payment_status": "paid",
	"order_status": {"code": "delivered", "name": "Delivered"},
	"payment_network": "mada",
	"invoice_link": "https://web.zid.sa/invoice/abc",
	"created_at": "2026-10-30 14:22:10",
	"issue_date": "30-10-2026",
	"payment": {
		"method": {"name": "Credit Card"},
		"invoice": [
			{"code": "sub_totals_before_vat", "value": "250.00"},
			{"code": "coupon", "value": "-20"},
			{"code": "taxable_amount", "value": 230},
			{"code": "vat", "value": "37.50"},
			{"code": "shipping", "value": "23.00"},
			{"code": "total", "value": "287.50"},
		],
	},
	"products": [
		{
			"order_product_id": 1,
			"parent_name": "Running Shoe",
			"name": "Running Shoe - 42 / Black",
			"short_description": {"ar": "حذاء", "en": "Shoe"},
			"sku": "#SKU-001",
			"quantity": 2,
			"price": "115.00",
			"tax_percentage": 15,
			"tax_amount": "15.00",
			"total": "230.00",
			"discounts": [{"coupon": {"amount": 16, "percentage": 0, "unit_amount": 8}}],
			"images": [{"thumbs": {"large": "https://img/1.jpg"}}],
		},
		{
			"order_product_id": 2,
			"parent_name": None,
			"name": "Socks",
			"sku": "SKU-002",
			"quantity": "1",
			"price": 57.5,
			"tax_percentage": 15,
			"tax_amount": 7.5,
			"total": 57.5,
			"images": [],
		},
	],
}


class TestHelpers(unittest.TestCase):
	def test_to_float(self):
		self.assertEqual(mapper.to_float("1,234.50 SAR"), 1234.5)
		self.assertEqual(mapper.to_float(None), 0.0)
		self.assertEqual(mapper.to_float("-20"), -20.0)
		self.assertEqual(mapper.to_float(7), 7.0)

	def test_sku_and_mobile(self):
		self.assertEqual(mapper.normalize_sku(" #ABC-1 "), "ABC-1")
		self.assertEqual(mapper.mobile_key("+966 50 123 4567"), "501234567")
		self.assertEqual(mapper.mobile_key("0501234567"), "501234567")
		self.assertEqual(mapper.mobile_key("123"), "")

	def test_dates(self):
		self.assertEqual(mapper.parse_datetime("30-10-2026"), datetime(2026, 10, 30))
		# issue_date has no time -> created_at time is kept
		self.assertEqual(mapper.order_datetime(SAMPLE_ORDER), datetime(2026, 10, 30, 14, 22, 10))
		order = {"id": 1, "issue_date": "", "created_at": "2026-10-29T08:00:00Z"}
		self.assertEqual(mapper.order_datetime(order), datetime(2026, 10, 29, 8, 0, 0))
		with self.assertRaises(mapper.ZidMappingError):
			mapper.order_datetime({"id": 1})


class TestPlan(unittest.TestCase):
	def setUp(self):
		self.cfg = mapper.MapperConfig()

	def test_basic_plan(self):
		plan = mapper.build_invoice_plan(SAMPLE_ORDER, self.cfg)
		self.assertEqual(plan.order_id, "41028001")
		self.assertEqual(len(plan.lines), 2)
		self.assertEqual(plan.lines[0].sku, "SKU-001")
		self.assertEqual(plan.lines[0].qty, 2)
		self.assertEqual(plan.lines[0].rate, 115.0)
		self.assertEqual(plan.lines[0].coupon_unit_amount, 8)
		self.assertEqual(plan.lines[0].product_name, "Running Shoe - 42 / Black")
		self.assertEqual(plan.lines[1].qty, 1.0)
		self.assertEqual(plan.shipping_rate, 23.0)
		self.assertEqual(plan.discount_amount, 20.0)
		self.assertEqual(plan.apply_discount_on, "Net Total")
		self.assertEqual(plan.expected_total, 287.5)
		self.assertEqual(plan.expected_vat, 37.5)
		self.assertEqual(plan.payment_method, "Credit Card")
		self.assertTrue(plan.record_payment)
		self.assertEqual(plan.currency, "SAR")
		self.assertEqual(plan.customer["email"], "ahmed@example.com")
		self.assertEqual(plan.header["zid_order_status"], "delivered")
		self.assertEqual(plan.warnings, [])

	def test_estimated_total_matches_zid(self):
		plan = mapper.build_invoice_plan(SAMPLE_ORDER, self.cfg)
		self.assertAlmostEqual(mapper.estimate_grand_total(plan, self.cfg), 287.5, places=2)

	def test_single_invoice_object_and_shipping_discount(self):
		order = copy.deepcopy(SAMPLE_ORDER)
		order["payment"]["invoice"] = [
			{"code": "shipping", "value": 30},
			{"code": "shipping_discount", "value": -30},
			{"code": "total", "value": 287.5},
		]
		plan = mapper.build_invoice_plan(order, self.cfg)
		self.assertEqual(plan.shipping_rate, 0)
		# no invoice coupon -> taken from product lines (8 * 2)
		self.assertEqual(plan.discount_amount, 16)
		self.assertTrue(any("product lines" in w for w in plan.warnings))

		order["payment"]["invoice"] = {"code": "total", "value": 10}
		plan = mapper.build_invoice_plan(order, self.cfg)
		self.assertEqual(plan.expected_total, 10)

	def test_shipping_vat_basis(self):
		cfg = mapper.MapperConfig(shipping_includes_vat=False)
		plan = mapper.build_invoice_plan(SAMPLE_ORDER, cfg)
		self.assertAlmostEqual(plan.shipping_rate, 26.45, places=4)

	def test_unknown_invoice_code_warns(self):
		order = copy.deepcopy(SAMPLE_ORDER)
		order["payment"]["invoice"].append({"code": "cod_fee", "value": 10})
		plan = mapper.build_invoice_plan(order, self.cfg)
		self.assertTrue(any("cod_fee" in w for w in plan.warnings))

	def test_missing_sku_raises(self):
		order = copy.deepcopy(SAMPLE_ORDER)
		order["products"][1]["sku"] = ""
		with self.assertRaises(mapper.ZidMappingError):
			mapper.build_invoice_plan(order, self.cfg)

	def test_no_products_raises(self):
		order = copy.deepcopy(SAMPLE_ORDER)
		order["products"] = []
		with self.assertRaises(mapper.ZidMappingError):
			mapper.build_invoice_plan(order, self.cfg)

	def test_unpaid(self):
		order = copy.deepcopy(SAMPLE_ORDER)
		order["payment_status"] = "pending"
		self.assertTrue(mapper.build_invoice_plan(order, self.cfg).record_payment)
		cfg = mapper.MapperConfig(record_payment_for_unpaid=False)
		self.assertFalse(mapper.build_invoice_plan(order, cfg).record_payment)


class TestSkip(unittest.TestCase):
	def test_cancelled(self):
		order = copy.deepcopy(SAMPLE_ORDER)
		order["order_status"] = {"code": "cancelled"}
		self.assertIsNotNone(mapper.skip_reason(order, mapper.MapperConfig()))
		self.assertIsNone(mapper.skip_reason(order, mapper.MapperConfig(skip_cancelled=False)))

	def test_only_paid(self):
		order = copy.deepcopy(SAMPLE_ORDER)
		order["payment_status"] = "pending"
		self.assertIsNone(mapper.skip_reason(order, mapper.MapperConfig()))
		self.assertIsNotNone(mapper.skip_reason(order, mapper.MapperConfig(only_paid=True)))


if __name__ == "__main__":
	unittest.main()
