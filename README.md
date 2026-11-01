# ZID Integration for ERPNext

Imports ZID e-commerce orders into ERPNext as **POS Sales Invoices**. It replaces the
`PACT.ZID.exe` console job that posted ZID orders into Pact RevenU.

Works on **Frappe/ERPNext v15** and is written to work on **v16**: it uses no APIs that
changed between the two, and it never sets `is_created_using_pos`, so no POS Opening
Entry is needed.

## What it does

| Step | Old PACT.ZID.exe | This app |
|---|---|---|
| Schedule | Windows Task Scheduler | Frappe scheduler; interval set in ZID Settings |
| Auth | Tokens in `COM_APIFieldsMapping`; refreshed after 11 months; client secret written into the exe | Tokens stored encrypted in ZID Settings; refreshed 30 days before expiry or on HTTP 401; built-in "Connect to ZID" OAuth |
| Fetch | `GET /managers/store/orders` (50/page, yesterday→today) then `/orders/{id}/view` | Same endpoints, same default window, retries on 429/5xx |
| Duplicates | `dcAlpha30 = order id` on doc 41028 | `Sales Invoice.zid_order_id` + one **ZID Order Log** per order |
| Customer | One posting account; customer details in text fields | Matched by ZID Customer ID → mobile → email; otherwise **Default Customer**. ZID name/email/mobile are always kept on the invoice |
| Items | SKU looked up as barcode (`#` removed) | SKU → Item Barcode → Item Code (`#` removed) |
| Prices | VAT inclusive | VAT inclusive (setting) using your tax template |
| Coupon (`coupon`) | dcNum22 | Invoice discount on Net Total (setting) |
| Shipping (`shipping` − `shipping_discount`) | dcNum25/26 → account 5150 | Line for the **Shipping Item**, posted to that item's income account |
| VAT (`vat`) | Account 4900 | Account on your tax template |
| Payment method | Dimension `com_cc50043`, default 97 | **Mode of Payment** on the POS invoice (mapping table + default) |
| Stock | `spDOC_ReconcileAvgValue` | `Update Stock` from the chosen warehouse |
| Draft/post | Always posted | **Draft or Submit** (setting) |
| History files | `C://PACTZID_History_*` text files | Raw order JSON saved in ZID Order Log |

Before saving, every invoice's grand total is checked against ZID's `total`. If they
differ by more than the tolerance, the order **fails** (or is saved as Draft, if you
choose that setting) instead of being posted with wrong figures.

## Install

```bash
cd ~/frappe-bench
bench get-app https://<your-git>/zid_integration.git   # or copy the folder into apps/
bench --site <site> install-app zid_integration
bench --site <site> migrate
bench restart
```

Installing adds `zid_*` custom fields to **Sales Invoice**, **Sales Invoice Item** and
**Customer** (the Customer field is ZID Customer ID).

## Setup (ZID Settings)

1. **Masters**
   - Create a **Default Customer**, e.g. `ZID Online Customer`.
   - Create a non-stock **Shipping Item** with the shipping income account as its default
     (this replaces account 5150).
   - Create a **Sales Taxes and Charges Template**: VAT 15%, *On Net Total*, on the output
     VAT account (this replaces 4900). The app marks the row as included in the price.
   - Give every **Mode of Payment** you use (Card, Mada, COD, Apple Pay…) a default account
     for the company. COD can point to a "COD receivable / courier" clearing account.
   - Make sure every ZID SKU exists as an **Item Barcode** or **Item Code**.
2. **Connection.** Use either option:
   - **Connect to ZID:** in the ZID Partner Dashboard, set the app's Redirect URL to
     `https://<site>/api/method/zid_integration.oauth.callback`, enter Client ID and Secret,
     then click *Connection → Connect to ZID*.
   - **Reuse Pact's tokens:** the `Result` column of the latest `COM_APIFieldsMapping` row
     (`APIName='ZID'`, `CostCenterID=999`) holds the token JSON. Paste its `authorization`
     into Authorization Token, `access_token` into Manager Token, and `refresh_token` into
     Refresh Token.

   Then click *Connection → Test Connection*.
3. **Invoice settings.** Set Company, Warehouse, Cost Center, *On Import* (Draft/Submit) and
   the payment-method mapping. ZID names are matched exactly as ZID sends them, ignoring
   capitals.
4. **Go-live.** Start with **On Import = Draft**. Use *Sync → Import One Order* on a few real
   orders (normal, coupon, shipping and COD) and compare each one with the ZID invoice. Then
   switch to **Submit** and tick **Enable Scheduled Sync**.

## Day-to-day

- **ZID Order Log** shows every order: Imported, Skipped, Failed or Pending, with the reason.
- Fix the cause (for example, add a missing item barcode), then click **Retry Import**, or
  **Retry Failed** in the list view.
- *Sync → Sync Now* lets you choose a date range to re-pull older orders. Orders that were
  already imported are never duplicated.
- Cancelled or reversed orders are skipped by default.

## Tests

```bash
# mapping logic (no site needed)
python -m unittest zid_integration.tests.test_mapper

# end to end on a TEST site (creates items, customers, a tax template)
bench --site test_site set-config allow_tests true
bench --site test_site run-tests --module zid_integration.tests.test_sync
```

## Notes

- Only the company currency is supported. Orders in another currency fail with a clear message.
- ZID invoice lines the app doesn't recognise (for example a COD fee) produce a warning in
  the log. The total check then catches them; add a mapping if you use them.
- ZATCA e-invoicing apps work as usual on the created Sales Invoices.
