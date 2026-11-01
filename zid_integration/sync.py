"""ZID -> ERPNext Sales Invoice sync (replacement for PACT.ZID.exe)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import (
	add_days,
	cint,
	flt,
	get_datetime,
	getdate,
	now_datetime,
	nowdate,
)

from zid_integration import mapper
from zid_integration.zid_client import SETTINGS, ZidApiError, ZidAuthError, ZidClient

LOG = "ZID Order Log"
JOB_ID = "zid_integration::sync_orders"
MANAGER_ROLES = ("System Manager", "Accounts Manager")


class ZidImportError(Exception):
	pass


# ===================================================================== entry points


def scheduled_sync():
	"""Cron tick (every 5 min). Respects enabled flag and Sync Interval."""
	if not frappe.db.get_single_value(SETTINGS, "enabled"):
		return
	interval = max(cint(frappe.db.get_single_value(SETTINGS, "sync_interval_minutes")) or 60, 5)
	last = frappe.db.get_single_value(SETTINGS, "last_sync_on")
	if last and now_datetime() < get_datetime(last) + timedelta(minutes=interval) - timedelta(seconds=30):
		return
	enqueue_sync()


def enqueue_sync(**kwargs):
	frappe.enqueue(
		"zid_integration.sync.sync_orders",
		queue="long",
		timeout=60 * 60,
		job_id=JOB_ID,
		deduplicate=True,
		**kwargs,
	)


@frappe.whitelist()
def sync_now(from_date=None, to_date=None):
	frappe.only_for(MANAGER_ROLES)
	enqueue_sync(from_date=from_date, to_date=to_date)
	return _("ZID sync queued. Check ZID Order Log for results.")


@frappe.whitelist()
def import_single_order(order_id):
	"""Fetch and import one order immediately (useful for testing)."""
	frappe.only_for(MANAGER_ROLES)
	order_id = str(order_id).strip()
	if not order_id:
		frappe.throw(_("Order ID is required"))
	settings = get_settings()
	client = ZidClient(settings)
	client.ensure_token()
	stats = Counter()
	process_order(order_id, client, settings, stats, force=True)
	return frappe.db.get_value(LOG, order_id, ["name", "status", "sales_invoice", "message"], as_dict=True)


@frappe.whitelist()
def retry_order(log_name):
	return import_single_order(frappe.db.get_value(LOG, log_name, "zid_order_id") or log_name)


@frappe.whitelist()
def retry_failed():
	frappe.only_for(MANAGER_ROLES)
	frappe.enqueue(
		"zid_integration.sync.process_failed",
		queue="long",
		timeout=60 * 60,
		job_id="zid_integration::retry_failed",
		deduplicate=True,
	)
	return _("Retry of failed orders queued.")


@frappe.whitelist()
def test_connection():
	frappe.only_for(MANAGER_ROLES)
	settings = frappe.get_doc(SETTINGS)
	client = ZidClient(settings)
	client.ensure_token()
	data = client.list_orders(1, add_days(nowdate(), -30), nowdate(), per_page=1)
	return _("Connected to ZID. Orders in the last 30 days: {0}").format(data.get("total_order_count", "?"))


@frappe.whitelist()
def refresh_token_now():
	frappe.only_for(MANAGER_ROLES)
	ZidClient(frappe.get_doc(SETTINGS)).refresh_token()
	return _("ZID token refreshed.")


# ===================================================================== sync loop


def sync_orders(from_date=None, to_date=None):
	settings = get_settings()
	started = now_datetime()
	stats = Counter()
	status = "Success"
	try:
		client = ZidClient(settings)
		client.ensure_token()
		date_from, date_to = get_date_range(settings, from_date, to_date)
		per_page = min(max(cint(settings.page_size) or 50, 1), 100)
		for summary in client.iter_orders(date_from, date_to, per_page=per_page):
			order_id = str(summary.get("id") or "").strip()
			if not order_id:
				continue
			stats["seen"] += 1
			if is_already_handled(order_id):
				stats["already_done"] += 1
				continue
			process_order(order_id, client, settings, stats)
	except Exception:
		status = "Failed"
		frappe.db.rollback()
		frappe.log_error(title="ZID sync failed", message=frappe.get_traceback())
	finally:
		summary_text = (
			f"{status} at {started:%Y-%m-%d %H:%M}: seen {stats['seen']}, imported {stats['imported']}, "
			f"skipped {stats['skipped']}, failed {stats['failed']}, already done {stats['already_done']}"
		)
		frappe.db.set_single_value(
			SETTINGS, {"last_sync_on": started, "last_sync_status": summary_text}
		)
		frappe.db.commit()
	return summary_text


def process_failed():
	settings = get_settings()
	client = ZidClient(settings)
	client.ensure_token()
	stats = Counter()
	for name in frappe.get_all(LOG, filters={"status": "Failed"}, pluck="name", order_by="creation asc"):
		process_order(name, client, settings, stats, force=True)


def get_date_range(settings, from_date=None, to_date=None):
	if from_date or to_date:
		f, t = getdate(from_date or to_date), getdate(to_date or from_date)
	elif settings.date_range_mode == "Fixed Dates":
		f, t = getdate(settings.from_date), getdate(settings.to_date)
	else:  # Rolling (Pact default: yesterday -> today)
		t = getdate(nowdate())
		f = getdate(add_days(t, -max(cint(settings.days_back), 0)))
	if f > t:
		f, t = t, f
	return f.strftime("%Y-%m-%d"), t.strftime("%Y-%m-%d")


def is_already_handled(order_id: str) -> bool:
	log_status = frappe.db.get_value(LOG, order_id, "status")
	if log_status in ("Imported", "Skipped"):
		return True
	return bool(find_existing_invoice(order_id))


def find_existing_invoice(order_id: str):
	"""Pact checked INV_DocDetails.dcAlpha30 = order id; we check zid_order_id."""
	return frappe.db.get_value(
		"Sales Invoice", {"zid_order_id": order_id, "docstatus": ["<", 2]}, "name"
	)


# ===================================================================== per order


def process_order(order_id, client, settings, stats, force=False):
	get_or_create_log(order_id)
	existing = find_existing_invoice(order_id)
	if existing:
		_update_log(order_id, status="Imported", sales_invoice=existing, message=_("Invoice already exists"))
		stats["already_done"] += 1
		return

	order = None
	try:
		order = client.get_order(order_id)
		cfg = build_mapper_config(settings)
		reason = mapper.skip_reason(order, cfg)
		if reason:
			_update_log(order_id, order=order, status="Skipped", message=reason, attempt=True)
			stats["skipped"] += 1
			return
		si, warnings = create_invoice(order, settings, cfg)
		_update_log(
			order_id,
			order=order,
			status="Imported",
			sales_invoice=si.name,
			message="\n".join(warnings) or _("Imported ({0})").format(_("Submitted") if si.docstatus == 1 else _("Draft")),
			attempt=True,
		)
		stats["imported"] += 1
	except ZidAuthError as e:
		# token problem: every following order would fail too, so stop the run
		frappe.db.rollback()
		_update_log(order_id, order=order, status="Failed", message=str(e), attempt=True)
		raise
	except Exception as e:
		frappe.db.rollback()
		message = (
			str(e)
			if isinstance(e, (ZidImportError, mapper.ZidMappingError, frappe.ValidationError, ZidApiError))
			else frappe.get_traceback()
		)
		_update_log(order_id, order=order, status="Failed", message=_strip_html(message), attempt=True)
		stats["failed"] += 1


def get_or_create_log(order_id):
	if frappe.db.exists(LOG, order_id):
		return frappe.get_doc(LOG, order_id)
	log = frappe.get_doc({"doctype": LOG, "zid_order_id": order_id, "status": "Pending"})
	log.insert(ignore_permissions=True)
	frappe.db.commit()
	return log


def _update_log(order_id, order=None, status=None, sales_invoice=None, message=None, attempt=False):
	log = get_or_create_log(order_id)
	if status:
		log.status = status
	if sales_invoice:
		log.sales_invoice = sales_invoice
	if message is not None:
		log.message = message[:10000]
	if attempt:
		log.attempts = cint(log.attempts) + 1
		log.last_attempt_on = now_datetime()
	if order:
		customer = order.get("customer") or {}
		totals = mapper.invoice_totals(order)
		try:
			log.order_date = mapper.order_datetime(order).date()
		except mapper.ZidMappingError:
			pass
		log.zid_invoice_number = mapper.text(order.get("invoice_number"))
		log.customer_name = mapper.text(customer.get("name"))[:140]
		log.order_total = totals.get("total")
		log.payment_status = mapper.status_code(order.get("payment_status"))
		log.order_status = mapper.status_code(order.get("order_status"))
		log.payment_method = mapper.payment_method_name(order)[:140]
		log.raw_order = json.dumps(order, indent=1, ensure_ascii=False, default=str)
	log.flags.ignore_permissions = True
	log.save()
	frappe.db.commit()


def _strip_html(msg: str) -> str:
	from frappe.utils import strip_html

	return strip_html(msg or "")


# ===================================================================== invoice


def get_settings():
	settings = frappe.get_doc(SETTINGS)
	missing = [
		label
		for field, label in (
			("company", "Company"),
			("default_customer", "Default Customer"),
			("default_mode_of_payment", "Default Mode of Payment"),
			("taxes_and_charges", "Sales Taxes and Charges Template"),
		)
		if not settings.get(field)
	]
	if missing:
		frappe.throw(_("ZID Settings incomplete: {0}").format(", ".join(missing)))
	if cint(settings.update_stock) and not settings.warehouse:
		frappe.throw(_("ZID Settings: Warehouse is required when Update Stock is enabled"))
	return settings


def build_mapper_config(settings) -> mapper.MapperConfig:
	return mapper.MapperConfig(
		prices_include_vat=bool(cint(settings.prices_include_vat)),
		shipping_includes_vat=bool(cint(settings.shipping_includes_vat)),
		vat_rate=get_vat_rate(settings.taxes_and_charges),
		coupon_apply_on=settings.coupon_apply_on or "Net Total",
		skip_cancelled=bool(cint(settings.skip_cancelled_orders)),
		only_paid=bool(cint(settings.only_paid_orders)),
		record_payment_for_unpaid=(settings.unpaid_orders or "Record POS Payment") == "Record POS Payment",
	)


def get_tax_rows(template):
	return frappe.get_all(
		"Sales Taxes and Charges",
		filters={"parent": template, "parenttype": "Sales Taxes and Charges Template"},
		fields=[
			"charge_type",
			"row_id",
			"account_head",
			"description",
			"rate",
			"cost_center",
			"tax_amount",
			"included_in_print_rate",
		],
		order_by="idx asc",
	)


def get_vat_rate(template) -> float:
	return sum(flt(r.rate) for r in get_tax_rows(template) if r.charge_type == "On Net Total")


def create_invoice(order, settings, cfg):
	plan = mapper.build_invoice_plan(order, cfg)
	warnings = list(plan.warnings)
	company = settings.company
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	if plan.currency and plan.currency != company_currency:
		raise ZidImportError(
			_("Order currency {0} differs from company currency {1}").format(plan.currency, company_currency)
		)

	customer, note = resolve_customer(plan.customer, settings)
	if note:
		warnings.append(note)

	item_cache: dict = {}
	missing = []
	resolved = []
	for line in plan.lines:
		item_code = resolve_item(line.sku, settings, item_cache)
		if not item_code:
			missing.append(line.sku)
		resolved.append((line, item_code))
	if missing:
		raise ZidImportError(
			_("Item not found for SKU(s): {0}. Add them as Item Barcode or Item Code.").format(
				", ".join(sorted(set(missing)))
			)
		)
	if any(code == settings.fallback_item for _l, code in resolved) and settings.fallback_item:
		warnings.append(_("Some SKUs were not found; fallback item {0} used").format(settings.fallback_item))

	posting = plan.posting_datetime
	si = frappe.new_doc("Sales Invoice")
	si.update(
		{
			"company": company,
			"customer": customer,
			"currency": company_currency,
			"posting_date": posting.date(),
			"posting_time": posting.strftime("%H:%M:%S"),
			"set_posting_time": 1,
			"due_date": posting.date(),
			"is_pos": 1 if plan.record_payment else 0,
			"update_stock": cint(settings.update_stock),
			"set_warehouse": settings.warehouse if cint(settings.update_stock) else None,
			"cost_center": settings.cost_center,
			"ignore_pricing_rule": 1,
			"disable_rounded_total": 1,
			"taxes_and_charges": settings.taxes_and_charges,
			"remarks": _("ZID order {0}").format(plan.header.get("zid_invoice_number") or plan.order_id),
		}
	)
	if settings.naming_series:
		si.naming_series = settings.naming_series
	if plan.record_payment and settings.pos_profile:
		si.pos_profile = settings.pos_profile
	else:
		si.flags.ignore_pos_profile = True
	si.update(plan.header)

	for line, item_code in resolved:
		row = {
			"item_code": item_code,
			"qty": line.qty,
			"rate": line.rate,
			"price_list_rate": line.rate,
			"cost_center": settings.cost_center,
			"zid_order_product_id": line.order_product_id,
			"zid_sku": line.sku,
			"zid_product_name": line.product_name,
			"zid_coupon_unit_amount": line.coupon_unit_amount,
		}
		if cint(settings.update_stock):
			row["warehouse"] = settings.warehouse
		si.append("items", row)

	if plan.shipping_rate > 0:
		if not settings.shipping_item:
			raise ZidImportError(_("Order has shipping {0} but no Shipping Item is set in ZID Settings").format(plan.shipping_rate))
		si.append(
			"items",
			{
				"item_code": settings.shipping_item,
				"qty": 1,
				"rate": plan.shipping_rate,
				"price_list_rate": plan.shipping_rate,
				"cost_center": settings.cost_center,
				"zid_product_name": "Shipping",
			},
		)

	for tax in get_tax_rows(settings.taxes_and_charges):
		tax = dict(tax)
		if tax["charge_type"] != "Actual":
			tax["included_in_print_rate"] = cint(settings.prices_include_vat)
		tax["cost_center"] = tax.get("cost_center") or settings.cost_center
		si.append("taxes", tax)

	if plan.discount_amount:
		si.apply_discount_on = plan.apply_discount_on
		si.discount_amount = plan.discount_amount

	si.set_missing_values(for_validate=True)
	si.calculate_taxes_and_totals()

	if plan.record_payment:
		mop = resolve_mode_of_payment(plan.payment_method, settings)
		si.append("payments", {"mode_of_payment": mop, "amount": flt(si.grand_total)})
		si.calculate_taxes_and_totals()

	keep_draft = False
	tolerance = flt(settings.total_tolerance) if settings.total_tolerance is not None else 0.05
	if plan.expected_total is not None:
		diff = flt(si.grand_total) - flt(plan.expected_total)
		if abs(diff) > tolerance:
			msg = _("Grand total {0} does not match ZID total {1} (difference {2})").format(
				flt(si.grand_total, 2), flt(plan.expected_total, 2), flt(diff, 2)
			)
			if (settings.on_total_mismatch or "Fail") == "Fail":
				raise ZidImportError(msg)
			warnings.append(msg + " - " + _("saved as Draft"))
			keep_draft = True
	if plan.expected_vat is not None:
		vat_diff = flt(si.total_taxes_and_charges) - flt(plan.expected_vat)
		if abs(vat_diff) > tolerance:
			warnings.append(
				_("VAT {0} differs from ZID VAT {1}").format(
					flt(si.total_taxes_and_charges, 2), flt(plan.expected_vat, 2)
				)
			)

	si.flags.ignore_permissions = True
	si.insert()
	if (settings.invoice_action or "Draft") == "Submit" and not keep_draft:
		si.submit()
	return si, warnings


# ===================================================================== resolvers


def resolve_item(sku: str, settings, cache: dict):
	if sku in cache:
		return cache[sku]
	item_code = (
		frappe.db.get_value("Item Barcode", {"barcode": sku, "parenttype": "Item"}, "parent")
		or frappe.db.get_value("Item", {"name": sku}, "name")
		or frappe.db.get_value("Item", {"item_code": sku}, "name")
	)
	if item_code:
		disabled, has_variants = frappe.db.get_value("Item", item_code, ["disabled", "has_variants"])
		if disabled or has_variants:
			item_code = None
	if not item_code and settings.fallback_item:
		item_code = settings.fallback_item
	cache[sku] = item_code
	return item_code


def resolve_customer(info: dict, settings):
	"""Return (customer, note). Falls back to the Default Customer like Pact did."""
	default = settings.default_customer
	if info.get("id") and cint(settings.match_by_zid_customer_id):
		name = frappe.db.get_value("Customer", {"zid_customer_id": info["id"], "disabled": 0}, "name")
		if name:
			return name, None

	if cint(settings.match_by_mobile):
		key = mapper.mobile_key(info.get("mobile"))
		if key:
			rows = frappe.db.sql(
				"""select name from `tabCustomer`
				where disabled = 0 and ifnull(mobile_no, '') != ''
				and replace(replace(replace(replace(mobile_no, ' ', ''), '-', ''), '+', ''), '(', '') like %s
				limit 2""",
				(f"%{key}",),
				pluck=True,
			)
			if len(rows) == 1:
				return rows[0], None
			if len(rows) > 1:
				return default, _("Several customers share mobile {0}; default customer used").format(info.get("mobile"))

	if cint(settings.match_by_email) and info.get("email"):
		rows = frappe.get_all(
			"Customer", filters={"email_id": info["email"], "disabled": 0}, pluck="name", limit=2
		)
		if len(rows) == 1:
			return rows[0], None
		if len(rows) > 1:
			return default, _("Several customers share email {0}; default customer used").format(info["email"])

	return default, None


def resolve_mode_of_payment(zid_method: str, settings) -> str:
	key = (zid_method or "").strip().lower()
	mop = None
	for row in settings.get("payment_method_mapping") or []:
		if (row.zid_payment_method or "").strip().lower() == key:
			mop = row.mode_of_payment
			break
	if not mop and key:
		mop = frappe.db.get_value("Mode of Payment", {"name": zid_method, "enabled": 1}, "name")
	mop = mop or settings.default_mode_of_payment

	if not frappe.db.get_value(
		"Mode of Payment Account", {"parent": mop, "company": settings.company}, "default_account"
	):
		raise ZidImportError(
			_("Mode of Payment {0} has no default account for company {1}").format(mop, settings.company)
		)
	return mop
