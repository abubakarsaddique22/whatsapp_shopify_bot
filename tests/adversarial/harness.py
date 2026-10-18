"""The harness of the adversarial tests: the REAL graph with fake tools and a FakeLLM.

Same idea as tests/integration/test_graph_flow.py (no database, no Shopify, no model, no network),
with three additions for attacks:

* SETTINGS holds fake SECRETS, so a test can check that no model call, no log line and no reply
  ever contains one of them;
* `Bot.events` records the security events the graph writes (the strikes);
* `Bot.llm_text()` gives everything the model was shown in a turn (every system and user message).

One FakeLLM answers both roles: per message the router answer comes first, then the composer
answer(s). A call too many raises, so a test that expects NO composer call fails loudly if the
graph makes one.

What these tests prove: GIVEN a router answer, the pipeline (gate, tools, templates, validator)
stays safe. How often the real router gives the right answer is measured in Phase 12 with the
real model (`pytest -m llm`, evals/adversarial.jsonl: field ok_intents).
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import graph as graph_mod
from app.agent import nodes
from app.agent.graph import build_graph, run_turn, to_replies
from app.agent.nodes import Deps
from app.agent.state import GraphState
from app.agent.templates import render
from app.agent.tools import TOOLS
from app.core.settings import Settings
from app.db import repositories as repo
from app.domain.models import ConversationState, ToolResult
from app.integrations.llm.fake_llm import FakeLLM

PID = "gid://shopify/Product/9401"
OTHER_PID = "gid://shopify/Product/9402"

# Fake secrets. The first two look like real tokens (the log redaction knows these shapes); the
# others are plain strings that only "never put it in a prompt, a reply or a log" can protect.
SECRETS = {
    "whatsapp_access_token": "EAAGfaketokenfaketokenfaketoken1234",
    "llm_api_key": "gsk_" + "a1B2c3D4" * 5,
    "whatsapp_verify_token": "verify-secret-9f3a1c",
    "shopify_client_secret": "shpss_fakeclientsecret0123456789",
    "phone_hash_pepper": "pepper-for-tests-0001",
}

SETTINGS = Settings(  # type: ignore[call-arg]
    _env_file=None, shopify_shop_domain="shop.example.com", **SECRETS
)


def secret_values() -> list[str]:
    return list(SECRETS.values())


# ---- builders (the same shapes as the real tools return)


def new_conversation() -> ConversationState:
    return ConversationState(conversation_id="c1", sender_hash="h1")


def result(tool: str, data: Any = None, ok: bool = True, error_code: str | None = None):
    return ToolResult(
        tool=tool,
        ok=ok,
        data=data,
        error_code=error_code,
        source_id=f"{tool}:test",
        fetched_at=datetime.now(UTC),
    )


def router_says(intent: str, language: str = "en", **fields: Any) -> str:
    """What the router (FakeLLM) answers. English by default: the replies are plain English."""
    return json.dumps({"language": language, "intent": intent, "confidence": 0.9, **fields})


def composer_says(text: str, language: str = "en") -> str:
    """What the composer answers (the graph sets intent, language and source ids itself)."""
    return json.dumps({"detected_language": language, "intent": "greeting", "response_text": text})


def policy_data(text: str | None, doc_type: str = "policy_refund") -> dict[str, Any]:
    passages = []
    if text:
        passages = [
            {
                "source_id": "kb:test:1",
                "doc_type": doc_type,
                "heading": "Policy",
                "text": text,
                "url": None,
                "score": 0.8,
            }
        ]
    return {
        "abstain": not passages,
        "doc_type": doc_type,
        "best_score": 0.8 if passages else 0.1,
        "passages": passages,
    }


def any_language(kind: str) -> set[str]:
    """The text of one template in every reply language (for tests that do not care which)."""
    return {render(kind, lang) for lang in ("en", "roman_ur", "en_roman_ur")}  # type: ignore[arg-type]


# ---- the bot


class Bot:
    """Runs turns through the real graph and remembers what the fakes saw."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.calls: list[tuple[str, dict[str, Any]]] = []  # (tool, kwargs) of every tool call
        self.saved: list[ConversationState] = []  # what the finish node saved, one per turn
        self.events: list[tuple[str, dict[str, Any]]] = []  # security events (the strikes)
        self.llm = FakeLLM([])
        self.state: GraphState = GraphState()

    def tool(
        self, name: str, data: Any = None, *, ok: bool = True, error_code: str | None = None
    ) -> None:
        async def fake(session: Any, **kwargs: Any) -> ToolResult:
            self.calls.append((name, kwargs))
            return result(name, data, ok, error_code)

        self.monkeypatch.setitem(TOOLS, name, fake)

    def record_every_tool(self) -> None:
        """For tests that must show NO tool runs: every tool records its call."""
        for name in TOOLS:
            self.tool(name, {"match": "one"})

    async def turn(
        self,
        text: str,
        *answers: str | Exception,
        conversation: ConversationState | None = None,
    ) -> GraphState:
        self.llm = FakeLLM(list(answers))
        graph = build_graph(Deps(session=None, llm=self.llm), None)  # type: ignore[arg-type]
        self.state = await run_turn(
            graph,
            1,
            text,
            conversation or new_conversation(),
            correlation_id="test-turn",
            budget_seconds=None,
        )
        return self.state

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    @property
    def reply(self) -> str:
        (only,) = to_replies(self.state)
        return only.body

    def template(self, kind: str, **values: str) -> str:
        """The template text in the reply language of the turn that just ran."""
        return render(kind, self.state["reply_language"], **values)

    def prompt(self, call: int) -> str:
        """The user message of one model call (0 = router, 1 = first composer call ...)."""
        return self.llm.calls[call][1]

    def llm_text(self) -> str:
        """Everything the model was shown in the last turn: every system and user message."""
        return "\n".join(f"{system}\n{user}" for system, user in self.llm.calls)


def install_fakes(monkeypatch: pytest.MonkeyPatch) -> Bot:
    """Replace the database parts of the graph and the settings. Used by the `bot` fixture."""
    harness = Bot(monkeypatch)

    async def fake_save(session: Any, conversation_id: int, conversation: ConversationState):
        harness.saved.append(conversation.model_copy(deep=True))

    async def fake_basics(session: Any, product_id: str):
        return SimpleNamespace(title="Qorvex Reef")

    async def fake_event(session: Any, kind: str, **kwargs: Any) -> None:
        harness.events.append((kind, kwargs))

    monkeypatch.setattr(repo, "save_state", fake_save)
    monkeypatch.setattr(repo, "add_security_event", fake_event)
    monkeypatch.setattr(repo, "product_basics", fake_basics)
    monkeypatch.setattr(nodes, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(graph_mod, "get_settings", lambda: SETTINGS)
    return harness
