"""HTTP client for the ZID Manager API (same endpoints the old PACT.ZID.exe used)."""

from __future__ import annotations

import time
from datetime import timedelta

import frappe
import requests
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime
from frappe.utils.password import set_encrypted_password

SETTINGS = "ZID Settings"
DEFAULT_API_BASE = "https://api.zid.sa/v1"
DEFAULT_OAUTH_BASE = "https://oauth.zid.sa"
TIMEOUT = 60
MAX_RETRIES = 3
REFRESH_BEFORE_DAYS = 30


class ZidApiError(Exception):
	pass


class ZidAuthError(ZidApiError):
	"""Tokens missing/invalid - the whole run should stop."""


class ZidClient:
	def __init__(self, settings=None):
		self.settings = settings or frappe.get_doc(SETTINGS)
		s = self.settings
		self.api_base = (s.api_base_url or DEFAULT_API_BASE).rstrip("/")
		self.oauth_base = (s.oauth_base_url or DEFAULT_OAUTH_BASE).rstrip("/")
		self.delay = max(cint(s.request_delay_ms), 0) / 1000.0
		self.authorization = s.get_password("authorization_token", raise_exception=False) or ""
		self.manager_token = s.get_password("manager_token", raise_exception=False) or ""
		self.session = requests.Session()
		self._last_call = 0.0
		self._refreshed = False

	# ------------------------------------------------------------ tokens

	def ensure_token(self):
		if not (self.authorization and self.manager_token):
			raise ZidAuthError(_("ZID tokens are missing. Use 'Connect to ZID' or paste the tokens in ZID Settings."))
		expires = self.settings.token_expires_on
		if expires and get_datetime(expires) <= add_to_date(now_datetime(), days=REFRESH_BEFORE_DAYS):
			self.refresh_token()

	def refresh_token(self):
		s = self.settings
		refresh = s.get_password("refresh_token", raise_exception=False)
		if not refresh:
			raise ZidAuthError(_("No ZID refresh token stored; reconnect to ZID."))
		data = {
			"grant_type": "refresh_token",
			"refresh_token": refresh,
			"client_id": s.client_id,
			"client_secret": s.get_password("client_secret", raise_exception=False) or "",
			"redirect_uri": s.redirect_uri or "",
		}
		resp = self.session.post(f"{self.oauth_base}/oauth/token", data=data, timeout=TIMEOUT)
		if resp.status_code >= 400:
			raise ZidAuthError(f"ZID token refresh failed ({resp.status_code}): {resp.text[:500]}")
		save_tokens(resp.json())
		self.settings = frappe.get_doc(SETTINGS)
		self.authorization = self.settings.get_password("authorization_token", raise_exception=False)
		self.manager_token = self.settings.get_password("manager_token", raise_exception=False)
		self._refreshed = True

	# ------------------------------------------------------------ http

	def _headers(self):
		return {
			"Authorization": f"Bearer {self.authorization}",
			"X-Manager-Token": self.manager_token,
			"Accept-Language": "en",
			"Accept": "application/json",
			"Content-Type": "application/json",
		}

	def _throttle(self):
		wait = self.delay - (time.monotonic() - self._last_call)
		if wait > 0:
			time.sleep(wait)
		self._last_call = time.monotonic()

	def get(self, path: str, params: dict | None = None) -> dict:
		url = f"{self.api_base}/{path.lstrip('/')}"
		for attempt in range(1, MAX_RETRIES + 1):
			self._throttle()
			try:
				resp = self.session.get(url, headers=self._headers(), params=params, timeout=TIMEOUT)
			except requests.RequestException as e:
				if attempt == MAX_RETRIES:
					raise ZidApiError(f"ZID request failed: {e}") from e
				time.sleep(5 * attempt)
				continue

			if resp.status_code == 401 and not self._refreshed:
				self.refresh_token()
				continue
			if resp.status_code == 429 or resp.status_code >= 500:
				if attempt == MAX_RETRIES:
					break
				retry_after = cint(resp.headers.get("Retry-After")) or 10 * attempt
				time.sleep(min(retry_after, 120))
				continue
			if resp.status_code in (401, 403):
				raise ZidAuthError(f"ZID rejected the tokens ({resp.status_code}): {resp.text[:300]}")
			if resp.status_code >= 400:
				raise ZidApiError(f"ZID API {resp.status_code} for {path}: {resp.text[:500]}")
			try:
				return resp.json()
			except ValueError as e:
				raise ZidApiError(f"ZID returned non-JSON for {path}: {resp.text[:300]}") from e
		raise ZidApiError(f"ZID API kept failing for {path} ({resp.status_code}): {resp.text[:300]}")

	# ------------------------------------------------------------ orders

	def list_orders(self, page: int, date_from: str, date_to: str, per_page: int = 50) -> dict:
		return self.get(
			"managers/store/orders",
			params={"page": page, "per_page": per_page, "date_from": date_from, "date_to": date_to},
		)

	def iter_orders(self, date_from: str, date_to: str, per_page: int = 50, max_pages: int = 500):
		"""Yield order summaries across all pages (Pact: GetOrders 'List')."""
		seen = 0
		for page in range(1, max_pages + 1):
			data = self.list_orders(page, date_from, date_to, per_page)
			orders = data.get("orders") or []
			if not orders:
				return
			yield from orders
			seen += len(orders)
			total = cint(data.get("total_order_count"))
			if (total and seen >= total) or len(orders) < per_page:
				return

	def get_order(self, order_id) -> dict:
		"""Full order (Pact: GetOrders 'Details' -> /orders/{id}/view)."""
		data = self.get(f"managers/store/orders/{order_id}/view")
		order = data.get("order") if isinstance(data, dict) else None
		if not order:
			raise ZidApiError(f"ZID order {order_id} not found or invalid response")
		return order


def save_tokens(payload: dict):
	"""Persist an OAuth token response: access_token -> X-Manager-Token, authorization -> Bearer."""
	if not payload.get("access_token") or not payload.get("authorization"):
		raise ZidApiError(f"Unexpected ZID token response: {str(payload)[:300]}")
	set_encrypted_password(SETTINGS, SETTINGS, payload["access_token"], "manager_token")
	set_encrypted_password(SETTINGS, SETTINGS, payload["authorization"], "authorization_token")
	if payload.get("refresh_token"):
		set_encrypted_password(SETTINGS, SETTINGS, payload["refresh_token"], "refresh_token")
	expires_in = cint(payload.get("expires_in"))
	expires_on = now_datetime() + timedelta(seconds=expires_in) if expires_in else None
	values = {"token_expires_on": expires_on, "token_updated_on": now_datetime()}
	# masked placeholders so the Password fields show as filled
	for f in ("manager_token", "authorization_token", "refresh_token"):
		values[f] = "*" * 10
	frappe.db.set_single_value(SETTINGS, values)
	frappe.db.commit()
