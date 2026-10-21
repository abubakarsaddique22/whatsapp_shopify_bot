"""FastAPI application factory."""

import logging
import time

from fastapi import FastAPI, Request

from app.api import admin, dev, health, shopify_webhook, signup, team, whatsapp_webhook
from app.core.errors import register_error_handlers
from app.core.ids import correlation_id_var, new_correlation_id
from app.core.logging import setup_logging
from app.core.settings import get_settings

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    app = FastAPI(title="WhatsApp Shopify Support Bot", version="0.1.0")
    register_error_handlers(app)

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        cid = request.headers.get("X-Correlation-Id") or new_correlation_id()
        token = correlation_id_var.set(cid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Correlation-Id"] = cid
        log.info(
            "request",
            extra={
                "path": request.url.path,
                "method": request.method,
                "status": response.status_code,
                "ms": round((time.perf_counter() - start) * 1000),
            },
        )
        return response

    app.include_router(health.router)
    app.include_router(whatsapp_webhook.router)
    app.include_router(shopify_webhook.router)
    if settings.app_env == "local":
        app.include_router(dev.router)  # /dev/simulate and /dev/outbox exist only locally
    if settings.admin_endpoints_enabled:  # Phase 11: off by default; every call needs X-Admin-Key
        app.include_router(admin.router)
    if settings.team_password.get_secret_value():  # the team's bot ON / OFF page
        app.include_router(team.router)
    if settings.signup_key.get_secret_value():  # P5: connect the Business app number (once)
        app.include_router(signup.router)
    return app


app = create_app()
