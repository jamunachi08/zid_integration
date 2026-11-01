"""ZID OAuth 'Connect' flow.

Replaces the external PACTZIDAPI/oauth/callback service the old setup used.
Register this URL as the Redirect URL of your ZID partner app:

    https://<your-site>/api/method/zid_integration.oauth.callback
"""

from urllib.parse import urlencode

import frappe
import requests
from frappe import _
from frappe.utils import get_url

from zid_integration.zid_client import DEFAULT_OAUTH_BASE, SETTINGS, save_tokens

STATE_PREFIX = "zid_oauth_state:"


def get_callback_url():
	return get_url("/api/method/zid_integration.oauth.callback")


@frappe.whitelist()
def get_authorize_url():
	frappe.only_for("System Manager")
	settings = frappe.get_doc(SETTINGS)
	if not settings.client_id:
		frappe.throw(_("Set Client ID and Client Secret first"))
	redirect_uri = settings.redirect_uri or get_callback_url()
	state = frappe.generate_hash(length=32)
	frappe.cache.set_value(STATE_PREFIX + state, frappe.session.user, expires_in_sec=900)
	base = (settings.oauth_base_url or DEFAULT_OAUTH_BASE).rstrip("/")
	query = urlencode(
		{"client_id": settings.client_id, "redirect_uri": redirect_uri, "response_type": "code", "state": state}
	)
	return f"{base}/oauth/authorize?{query}"


@frappe.whitelist(allow_guest=True)
def callback(code=None, state=None, **kwargs):
	def finish(ok, message):
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = "/app/zid-settings?" + urlencode(
			{"zid_oauth": "success" if ok else "error", "message": message[:200]}
		)

	if not state or not frappe.cache.get_value(STATE_PREFIX + state):
		return finish(False, _("Invalid or expired state. Start 'Connect to ZID' again."))
	frappe.cache.delete_value(STATE_PREFIX + state)
	if not code:
		return finish(False, kwargs.get("error_description") or kwargs.get("error") or _("No code returned"))

	settings = frappe.get_doc(SETTINGS)
	base = (settings.oauth_base_url or DEFAULT_OAUTH_BASE).rstrip("/")
	try:
		resp = requests.post(
			f"{base}/oauth/token",
			data={
				"grant_type": "authorization_code",
				"client_id": settings.client_id,
				"client_secret": settings.get_password("client_secret", raise_exception=False) or "",
				"redirect_uri": settings.redirect_uri or get_callback_url(),
				"code": code,
			},
			timeout=60,
		)
		if resp.status_code >= 400:
			raise Exception(f"{resp.status_code}: {resp.text[:300]}")
		save_tokens(resp.json())
	except Exception as e:
		frappe.log_error(title="ZID OAuth callback failed", message=frappe.get_traceback())
		return finish(False, str(e))
	return finish(True, _("Connected to ZID"))
