"""LangSmith tracing switch (Phase 9, step 7): .env values -> the process environment.

pydantic-settings reads `.env` into the Settings object only. LangChain and LangSmith read the
process environment (`os.environ`), so without this call the traces stay off even when
LANGSMITH_TRACING=true is written in `.env`.

    configure_tracing()          # call once at program start (worker, app.cli chat)

Tracing is only switched on when LANGSMITH_TRACING=true AND a key is set. A trace holds the
customer's message and the bot's reply. LANGSMITH_HIDE_TEXT=true (Phase 12 Step 7) makes the
LangSmith client drop every input and output text (LANGSMITH_HIDE_INPUTS / _OUTPUTS): the trace
still shows the steps, timings, tokens and errors. Production refuses tracing without it
(core/settings.py). No phone number is ever put into the metadata (graph.turn_config).
"""

import logging
import os

from app.core.settings import Settings, get_settings

HIDE_VARIABLES = ("LANGSMITH_HIDE_INPUTS", "LANGSMITH_HIDE_OUTPUTS")

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
    for name in HIDE_VARIABLES:
        if current.langsmith_hide_text:
            os.environ[name] = "true"
        else:
            os.environ.pop(name, None)
    log.info(
        "langsmith_tracing_on",
        extra={"project": current.langsmith_project, "masked": current.langsmith_hide_text},
    )
    return True
