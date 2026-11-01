"""Pure mapping logic: ZID order JSON -> Sales Invoice plan.

This module has no Frappe imports so it can be unit tested without a site.
It mirrors what the old PACT.ZID.exe did in Integration.PostDoc:

    Pact field                          -> ERPNext
    issue_date (dd-MM-yyyy)             -> posting_date
    products[].sku (barcode, '#'-trim)  -> items[].item_code (via Item Barcode / Item Code)
    products[].quantity / price         -> qty / rate (VAT inclusive, "dcAlpha153=Inclusive")
    payment.invoice[code=coupon]        -> invoice discount_amount
    payment.invoice[code=shipping]      -> shipping service item
    payment.invoice[code=shipping_discount] -> reduces shipping
    payment.invoice[code=total]         -> reconciliation against grand_total
    payment.method.name                 -> Mode of Payment (POS payments table)
    dcAlpha30..46 text fields           -> zid_* custom fields
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

KNOWN_INVOICE_CODES = {
	"sub_totals",
	"sub_totals_before_vat",
	"sub_total",
	"coupon",
	"discount",
	"taxable_amount",
	"vat",
	"tax",
	"shipping",
	"shipping_discount",
	"total",
}

CANCELLED_STATUS_CODES = {"cancelled", "canceled", "reversed", "refunded", "returned"}
PAID_STATUS_CODES = {"paid"}

DATE_FORMATS = (
	"%d-%m-%Y",
	"%d-%m-%Y %H:%M:%S",
	"%d-%m-%Y %H:%M",
	"%Y-%m-%d %H:%M:%S",
	"%Y-%m-%dT%H:%M:%S",
	"%Y-%m-%dT%H:%M:%S.%fZ",
	"%Y-%m-%dT%H:%M:%SZ",
	"%Y-%m-%d",
	"%d/%m/%Y",
)


class ZidMappingError(Exception):
	"""Raised when an order cannot be turned into an invoice plan."""


@dataclass
class MapperConfig:
	prices_include_vat: bool = True
	shipping_includes_vat: bool = True
	vat_rate: float = 15.0
	coupon_apply_on: str = "Net Total"  # or "Grand Total"
	skip_cancelled: bool = True
	only_paid: bool = False
	record_payment_for_unpaid: bool = True


@dataclass
class PlanLine:
	sku: str
	qty: float
	rate: float
	product_name: str
	order_product_id: str = ""
	tax_percentage: float = 0.0
	tax_amount: float = 0.0
	coupon_unit_amount: float = 0.0
	image: str = ""


@dataclass
class InvoicePlan:
	order_id: str
	posting_datetime: datetime
	lines: list[PlanLine]
	shipping_rate: float
	discount_amount: float
	apply_discount_on: str
	expected_total: float | None
	expected_vat: float | None
	payment_method: str
	record_payment: bool
	currency: str
	customer: dict
	header: dict
	warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- helpers


def to_float(value) -> float:
	"""ZID sends numbers as numbers, numeric strings, or '1,234.50 SAR'."""
	if value is None or value == "":
		return 0.0
	if isinstance(value, bool):
		return float(value)
	if isinstance(value, (int, float)):
		return float(value)
	text = str(value).replace(",", "").strip()
	match = re.search(r"-?\d+(?:\.\d+)?", text)
	return float(match.group(0)) if match else 0.0


def as_list(value) -> list:
	"""Same as SingleOrArrayConverter in the old exe."""
	if value is None:
		return []
	if isinstance(value, list):
		return value
	return [value]


def text(value) -> str:
	if value is None:
		return ""
	if isinstance(value, dict):
		# localized dicts like {"ar": "...", "en": "..."}
		return str(value.get("en") or value.get("ar") or next(iter(value.values()), "") or "")
	return str(value).strip()


def normalize_sku(sku) -> str:
	"""Pact's spExt_OnScanPOS trimmed a leading '#'."""
	return text(sku).lstrip("#").strip()


def mobile_key(mobile) -> str:
	"""Last 9 digits of a mobile number: '+966 50 123 4567' -> '501234567'."""
	digits = re.sub(r"\D", "", text(mobile))
	return digits[-9:] if len(digits) >= 9 else ""


def parse_datetime(value) -> datetime | None:
	raw = text(value)
	if not raw:
		return None
	for fmt in DATE_FORMATS:
		try:
			return datetime.strptime(raw, fmt)
		except ValueError:
			continue
	# tolerate timezone suffixes such as "+03:00"
	try:
		return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
	except ValueError:
		return None


def order_datetime(order: dict) -> datetime:
	"""issue_date (what Pact used), falling back to created_at; keep created_at time if issue_date has none."""
	issue = parse_datetime(order.get("issue_date"))
	created = parse_datetime(order.get("created_at"))
	if issue and created and issue.time() == datetime.min.time() and issue.date() == created.date():
		return created
	result = issue or created
	if not result:
		raise ZidMappingError(
			f"Order {order.get('id')}: cannot read issue_date '{order.get('issue_date')}' "
			f"or created_at '{order.get('created_at')}'"
		)
	return result


def status_code(value) -> str:
	if isinstance(value, dict):
		value = value.get("code") or value.get("name")
	return text(value).lower()


def invoice_totals(order: dict) -> dict[str, float]:
	totals: dict[str, float] = {}
	payment = order.get("payment") or {}
	for row in as_list(payment.get("invoice")):
		if not isinstance(row, dict):
			continue
		code = text(row.get("code")).lower()
		if not code:
			continue
		totals[code] = totals.get(code, 0.0) + to_float(row.get("value"))
	return totals


def payment_method_name(order: dict) -> str:
	payment = order.get("payment") or {}
	method = payment.get("method") or {}
	if isinstance(method, dict):
		return text(method.get("name") or method.get("code"))
	return text(method)


def skip_reason(order: dict, cfg: MapperConfig) -> str | None:
	order_status = status_code(order.get("order_status"))
	if cfg.skip_cancelled and order_status in CANCELLED_STATUS_CODES:
		return f"Order status is '{order_status}'"
	payment_status = status_code(order.get("payment_status"))
	if cfg.only_paid and payment_status not in PAID_STATUS_CODES:
		return f"Payment status is '{payment_status or 'unknown'}' (only paid orders are imported)"
	return None


def _vat_factor(cfg: MapperConfig) -> float:
	return 1 + (cfg.vat_rate or 0) / 100.0


# ---------------------------------------------------------------- main


def build_invoice_plan(order: dict, cfg: MapperConfig) -> InvoicePlan:
	if not isinstance(order, dict) or not order.get("id"):
		raise ZidMappingError("Order payload has no 'id'")

	order_id = text(order.get("id"))
	warnings: list[str] = []

	lines: list[PlanLine] = []
	line_coupon_total = 0.0
	for idx, product in enumerate(as_list(order.get("products")), start=1):
		if not isinstance(product, dict):
			continue
		qty = to_float(product.get("quantity"))
		if qty <= 0:
			warnings.append(f"Line {idx}: quantity {qty} ignored")
			continue
		sku = normalize_sku(product.get("sku"))
		if not sku:
			raise ZidMappingError(f"Line {idx} ('{text(product.get('name'))}') has no SKU")

		coupon_unit = 0.0
		for disc in as_list(product.get("discounts")):
			if isinstance(disc, dict) and isinstance(disc.get("coupon"), dict):
				coupon_unit += abs(to_float(disc["coupon"].get("unit_amount")))
		line_coupon_total += coupon_unit * qty

		image = ""
		images = as_list(product.get("images"))
		if images and isinstance(images[0], dict):
			thumbs = images[0].get("thumbs") or {}
			image = text(thumbs.get("large")) if isinstance(thumbs, dict) else ""

		name = text(product.get("name"))
		parent_name = text(product.get("parent_name"))
		display_name = name if not parent_name or parent_name in name else f"{parent_name} - {name}"

		lines.append(
			PlanLine(
				sku=sku,
				qty=qty,
				rate=to_float(product.get("price")),
				product_name=display_name[:140],
				order_product_id=text(product.get("order_product_id") or product.get("id")),
				tax_percentage=to_float(product.get("tax_percentage")),
				tax_amount=to_float(product.get("tax_amount")),
				coupon_unit_amount=coupon_unit,
				image=image,
			)
		)

	if not lines:
		raise ZidMappingError(f"Order {order_id} has no products")

	totals = invoice_totals(order)
	for code, value in totals.items():
		if code not in KNOWN_INVOICE_CODES and value:
			warnings.append(f"Unmapped ZID invoice line '{code}' = {value}")

	# Shipping (Pact: dcNum25 / dcNum26 -> account 5150)
	shipping = abs(totals.get("shipping", 0.0))
	shipping_discount = abs(totals.get("shipping_discount", 0.0))
	shipping_net = shipping - shipping_discount
	if shipping_net < 0:
		warnings.append(f"Shipping discount {shipping_discount} exceeds shipping {shipping}; shipping set to 0")
		shipping_net = 0.0
	# bring shipping onto the same VAT basis as item prices
	if cfg.prices_include_vat and not cfg.shipping_includes_vat:
		shipping_net = shipping_net * _vat_factor(cfg)
	elif not cfg.prices_include_vat and cfg.shipping_includes_vat:
		shipping_net = shipping_net / _vat_factor(cfg)
	shipping_net = round(shipping_net, 6)

	# Coupon (Pact: dcNum22). Invoice-level value wins; else sum of line coupons.
	discount = abs(totals.get("coupon", 0.0)) or abs(totals.get("discount", 0.0))
	if not discount and line_coupon_total:
		discount = line_coupon_total
		warnings.append("Coupon taken from product lines (no 'coupon' invoice line)")

	expected_total = totals.get("total") if "total" in totals else None
	expected_vat = totals.get("vat") if "vat" in totals else totals.get("tax")

	payment_status = status_code(order.get("payment_status"))
	record_payment = payment_status in PAID_STATUS_CODES or cfg.record_payment_for_unpaid

	customer = order.get("customer") or {}
	customer_info = {
		"id": text(customer.get("id")),
		"name": text(customer.get("name")),
		"email": text(customer.get("email")).lower(),
		"mobile": text(customer.get("mobile")),
	}

	header = {
		"zid_order_id": order_id,
		"zid_invoice_number": text(order.get("invoice_number")),
		"zid_order_url": text(order.get("order_url"))[:140],
		"zid_invoice_link": text(order.get("invoice_link"))[:140],
		"zid_order_status": status_code(order.get("order_status")),
		"zid_payment_status": payment_status,
		"zid_payment_method": payment_method_name(order)[:140],
		"zid_customer_id": customer_info["id"],
		"zid_customer_name": customer_info["name"][:140],
		"zid_customer_email": customer_info["email"][:140],
		"zid_customer_mobile": customer_info["mobile"][:140],
		"zid_transaction_reference": text(order.get("transaction_reference"))[:140],
		"zid_payment_network": text(order.get("payment_network"))[:140],
		"zid_order_total": expected_total or 0.0,
		"zid_vat_amount": expected_vat or 0.0,
	}

	return InvoicePlan(
		order_id=order_id,
		posting_datetime=order_datetime(order),
		lines=lines,
		shipping_rate=shipping_net,
		discount_amount=round(discount, 6),
		apply_discount_on=cfg.coupon_apply_on or "Net Total",
		expected_total=expected_total,
		expected_vat=expected_vat,
		payment_method=payment_method_name(order),
		record_payment=record_payment,
		currency=text(order.get("currency_code") or order.get("currency")).upper(),
		customer=customer_info,
		header=header,
		warnings=warnings,
	)


def estimate_grand_total(plan: InvoicePlan, cfg: MapperConfig) -> float:
	"""Rough ERPNext grand total for the plan; used in tests and sanity checks."""
	gross = sum(line.qty * line.rate for line in plan.lines) + plan.shipping_rate
	factor = _vat_factor(cfg)
	net = gross / factor if cfg.prices_include_vat else gross
	if plan.apply_discount_on == "Net Total":
		return round((net - plan.discount_amount) * factor, 2)
	return round(net * factor - plan.discount_amount, 2)
