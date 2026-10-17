"""Deterministic security helpers (no LLM involved)."""

import base64
import hashlib
import hmac

from app.core.errors import ForbiddenScope

# ---- WhatsApp: header X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(raw body, app secret)


def valid_whatsapp_signature(raw: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not header.startswith("sha256=") or not app_secret:
        return False
    expected = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def sign_whatsapp(raw: bytes, app_secret: str) -> str:
    """Used by tests and /dev/simulate to build a valid header."""
    return "sha256=" + hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()


# ---- Shopify: header X-Shopify-Hmac-Sha256 = base64(HMAC-SHA256(raw body, client secret))


def valid_shopify_hmac(raw: bytes, header: str | None, client_secret: str) -> bool:
    if not header or not client_secret:
        return False
    digest = hmac.new(client_secret.encode(), raw, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), header)


def sign_shopify(raw: bytes, client_secret: str) -> str:
    """Used by tests to build a valid X-Shopify-Hmac-Sha256 header."""
    digest = hmac.new(client_secret.encode(), raw, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


# ---- Shopify scope audit: the bot must stay read-only

ALLOWED_SCOPES = {
    "read_products",
    "read_inventory",
    "read_locations",
    "read_legal_policies",
    "read_online_store_pages",
}
FORBIDDEN_PREFIXES = (
    "write_",
    "read_orders",
    "read_all_orders",
    "read_customers",
    "read_draft_orders",
    "read_checkouts",
    "read_payment",
)


def audit_scopes(granted: set[str]) -> None:
    """Raise ForbiddenScope if the app has any scope outside the read-only allowlist."""
    bad = {s for s in granted if s not in ALLOWED_SCOPES or s.startswith(FORBIDDEN_PREFIXES)}
    if bad:
        raise ForbiddenScope(f"Forbidden or unexpected Shopify scopes: {sorted(bad)}")
