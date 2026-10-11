"""Project-wide exceptions and FastAPI handlers.

Rule: raise these, never bare Exception. Messages must never contain secrets.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for every error we raise on purpose."""

    status_code = 500
    code = "app_error"

    def __init__(self, message: str = "", *, detail: dict | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.detail = detail or {}


class ConfigError(AppError):
    code = "config_error"


class PermissionDenied(AppError):
    """The permission gate refused a tool call (deterministic, not the LLM)."""

    status_code = 403
    code = "permission_denied"


class ValidationFailed(AppError):
    """LLM output or tool arguments failed validation."""

    status_code = 422
    code = "validation_failed"


class InvalidSignature(AppError):
    status_code = 401
    code = "invalid_signature"


class ShopifyError(AppError):
    code = "shopify_error"


class ShopifyAuthError(ShopifyError):
    code = "shopify_auth_error"


class ShopifyThrottled(ShopifyError):
    code = "shopify_throttled"


class ShopifyUnavailable(ShopifyError):
    status_code = 503
    code = "shopify_unavailable"


class ForbiddenScope(ShopifyError):
    """The Shopify app has a scope outside our read-only allowlist."""

    code = "forbidden_scope"


class WhatsAppError(AppError):
    code = "whatsapp_error"


class WhatsAppTemporaryError(WhatsAppError):
    """Timeout, 429 or 5xx from WhatsApp: the outbox may retry later. Other errors will not work."""

    code = "whatsapp_temporary_error"


class ProviderError(AppError):
    """LLM / speech / embedding provider failed."""

    status_code = 502
    code = "provider_error"


class TranscriptionError(ProviderError):
    code = "transcription_error"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        log.warning("app_error", extra={"code": exc.code, "message_text": exc.message})
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error")
        return JSONResponse(status_code=500, content={"error": "internal_error"})
