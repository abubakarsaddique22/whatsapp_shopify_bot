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


class NotificationError(AppError):
    """Staff could not be told about a handoff or an alert (email, Slack or WhatsApp failed).

    The message never holds the Slack URL, the SMTP password or a phone number.
    """

    code = "notification_error"


class ProviderError(AppError):
    """LLM / speech / embedding provider failed."""

    status_code = 502
    code = "provider_error"


class TranscriptionError(ProviderError):
    code = "transcription_error"


class EmbeddingError(ProviderError):
    """The embedding provider failed (timeout, quota, bad answer). Nothing is stored then."""

    code = "embedding_error"


class UnsupportedMedia(AppError):
    """The customer sent a media file we do not accept (wrong type). The bot asks politely."""

    status_code = 415
    code = "unsupported_media"


class MediaTooLarge(AppError):
    """The media file is bigger than MAX_AUDIO_BYTES. Nothing was kept."""

    status_code = 413
    code = "media_too_large"


class AudioConversionError(AppError):
    """ffmpeg could not turn the audio into WAV (damaged file, unknown format, timeout)."""

    status_code = 422
    code = "audio_conversion_error"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        log.warning("app_error", extra={"code": exc.code, "error_message": exc.message})
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error")
        return JSONResponse(status_code=500, content={"error": "internal_error"})
