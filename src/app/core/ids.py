"""Correlation ids and phone-number hashing."""

import hashlib
import hmac
import uuid
from contextvars import ContextVar

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def hash_phone(wa_id: str, pepper: str) -> str:
    """Keyed hash of a phone number. Use this in logs and analytics, never the raw number."""
    return hmac.new(pepper.encode(), wa_id.encode(), hashlib.sha256).hexdigest()[:16]
