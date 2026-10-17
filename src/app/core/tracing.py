"""LangSmith tracing switch (Phase 9, step 7): .env values -> the process environment.

pydantic-settings reads `.env` into the Settings object only. LangChain and LangSmith read the
process environment (`os.environ`), so without this call the traces stay off even when
LANGSMITH_TRACING=true is written in `.env`.

    configure_tracing()          # call once at program start (worker, app.cli chat)

Tracing is only switched on when LANGSMITH_TRACING=true AND a key is set. A trace holds the
customer's message and the bot's reply, so keep it off in any environment where that is not
allowed (Phase 11 adds redaction before go-live). No phone number is ever put into the metadata
(graph.turn_config).
"""

import logging
import os

from app.core.settings import Settings, get_settings

log = logging.getLogger(__name__)


def configure_tracing(settings: Settings | None = None) -> bool:
    """Export the LangSmith settings to the environment. Returns True when tracing is on."""
    current = settings or get_settings()
    if not current.langsmith_tracing:
        return False
    key = current.langsmith_api_key.get_secret_value()
    if not key:
        log.warning("langsmith_tracing_requested_but_no_api_key")
        return False
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = key
    os.environ["LANGSMITH_PROJECT"] = current.langsmith_project
    log.info("langsmith_tracing_on", extra={"project": current.langsmith_project})
    return True
