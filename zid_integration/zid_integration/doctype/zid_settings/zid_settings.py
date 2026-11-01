import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, getdate


class ZIDSettings(Document):
	def validate(self):
		if self.date_range_mode == "Fixed Dates" and self.from_date and self.to_date:
			if getdate(self.from_date) > getdate(self.to_date):
				frappe.throw(_("From Date cannot be after To Date"))
		if cint(self.page_size) < 1 or cint(self.page_size) > 100:
			self.page_size = 50
		if cint(self.sync_interval_minutes) < 5:
			self.sync_interval_minutes = 5
		self.validate_company_links()
		self.validate_mapping()
		self.validate_tax_template()
		if self.shipping_item and frappe.db.get_value("Item", self.shipping_item, "is_stock_item"):
			frappe.throw(_("Shipping Item {0} must be a non-stock item").format(self.shipping_item))

	def validate_company_links(self):
		if not self.company:
			return
		checks = (
			("Warehouse", self.warehouse),
			("Cost Center", self.cost_center),
			("Sales Taxes and Charges Template", self.taxes_and_charges),
			("POS Profile", self.pos_profile),
		)
		for doctype, name in checks:
			if name and frappe.db.get_value(doctype, name, "company") != self.company:
				frappe.throw(_("{0} {1} does not belong to company {2}").format(doctype, name, self.company))

	def validate_mapping(self):
		seen = set()
		mops = [self.default_mode_of_payment] if self.default_mode_of_payment else []
		for row in self.payment_method_mapping:
			key = (row.zid_payment_method or "").strip().lower()
			if key in seen:
				frappe.throw(_("Row {0}: ZID payment method {1} is mapped twice").format(row.idx, row.zid_payment_method))
			seen.add(key)
			mops.append(row.mode_of_payment)
		if self.company:
			for mop in set(mops):
				if not frappe.db.get_value(
					"Mode of Payment Account", {"parent": mop, "company": self.company}, "default_account"
				):
					frappe.msgprint(
						_("Mode of Payment {0} has no default account for {1}; orders using it will fail.").format(
							frappe.bold(mop), self.company
						),
						indicator="orange",
						alert=True,
					)

	def validate_tax_template(self):
		if not self.taxes_and_charges:
			return
		rows = frappe.get_all(
			"Sales Taxes and Charges",
			filters={"parent": self.taxes_and_charges, "parenttype": "Sales Taxes and Charges Template"},
			fields=["charge_type", "rate"],
		)
		if not any(r.charge_type == "On Net Total" and r.rate for r in rows):
			frappe.msgprint(
				_("Tax template has no 'On Net Total' VAT row; VAT will not be calculated."), indicator="orange"
			)
