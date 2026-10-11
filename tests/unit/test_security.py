import base64
import hashlib
import hmac

import pytest

from app.core.errors import ForbiddenScope
from app.core.ids import hash_phone
from app.core.security import (
    audit_scopes,
    sign_whatsapp,
    valid_shopify_hmac,
    valid_whatsapp_signature,
)


def test_whatsapp_signature_roundtrip():
    body = b'{"hello":"world"}'
    assert valid_whatsapp_signature(body, sign_whatsapp(body, "s3cret"), "s3cret")


def test_whatsapp_signature_rejects_bad_and_missing():
    body = b"{}"
    assert not valid_whatsapp_signature(body, "sha256=deadbeef", "s3cret")
    assert not valid_whatsapp_signature(body, None, "s3cret")
    assert not valid_whatsapp_signature(body, sign_whatsapp(body, "s3cret"), "")


def test_shopify_hmac():
    body = b'{"id":1}'
    digest = hmac.new(b"sec", body, hashlib.sha256).digest()
    assert valid_shopify_hmac(body, base64.b64encode(digest).decode(), "sec")
    assert not valid_shopify_hmac(body, "bad", "sec")


def test_scope_audit_allows_read_only():
    audit_scopes({"read_products", "read_inventory"})


@pytest.mark.parametrize("scope", ["read_orders", "write_products", "read_customers", "foo"])
def test_scope_audit_blocks_private_or_write(scope):
    with pytest.raises(ForbiddenScope):
        audit_scopes({"read_products", scope})


def test_phone_hash_is_stable_and_hides_number():
    h = hash_phone("923001234567", "pepper")
    assert h == hash_phone("923001234567", "pepper")
    assert "923001234567" not in h
