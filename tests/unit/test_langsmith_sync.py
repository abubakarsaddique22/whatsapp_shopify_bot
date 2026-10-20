"""Phase 12, Step 7: eval sets as LangSmith datasets, eval runs as experiments, masking.

A FakeClient stands in for langsmith.Client: no network, no key. It stores what the real client
would have stored, so the tests can look at it.
"""

import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.settings import Settings
from app.core.tracing import HIDE_VARIABLES, configure_tracing
from app.evals import langsmith_sync as ls
from app.evals.langsmith_sync import Example, ExperimentRow

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
PHONE = "0300 1234567"
CARD = "4111 1111 1111 1111"
KEY = "gsk_" + "a1B2c3D4" * 5


class FakeClient:
    def __init__(self) -> None:
        self.datasets: dict[str, SimpleNamespace] = {}
        self.examples: dict[Any, list[SimpleNamespace]] = {}
        self.projects: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.feedback: list[dict[str, Any]] = []
        self.updates = 0

    def has_dataset(self, *, dataset_name: str) -> bool:
        return dataset_name in self.datasets

    def create_dataset(self, dataset_name: str, *, description: str = "") -> SimpleNamespace:
        dataset = SimpleNamespace(id=uuid.uuid4(), name=dataset_name, description=description)
        self.datasets[dataset_name] = dataset
        self.examples[dataset.id] = []
        return dataset

    def read_dataset(self, *, dataset_name: str) -> SimpleNamespace:
        return self.datasets[dataset_name]

    def list_examples(self, *, dataset_id: Any) -> list[SimpleNamespace]:
        return list(self.examples[dataset_id])

    def create_example(
        self, *, inputs: dict, outputs: dict, metadata: dict, dataset_id: Any
    ) -> SimpleNamespace:
        example = SimpleNamespace(
            id=uuid.uuid4(), inputs=inputs, outputs=outputs, metadata=metadata
        )
        self.examples[dataset_id].append(example)
        return example

    def update_example(
        self, example_id: Any, *, inputs: dict, outputs: dict, metadata: dict
    ) -> None:
        for items in self.examples.values():
            for example in items:
                if example.id == example_id:
                    example.inputs, example.outputs, example.metadata = inputs, outputs, metadata
        self.updates += 1

    def create_project(
        self, project_name: str, *, reference_dataset_id: Any, metadata: dict
    ) -> SimpleNamespace:
        project = SimpleNamespace(id=uuid.uuid4(), name=project_name)
        self.projects.append(
            {
                "id": project.id,
                "name": project_name,
                "dataset": reference_dataset_id,
                "metadata": metadata,
            }
        )
        return project

    def create_run(self, name: str, inputs: dict, run_type: str, **kwargs: Any) -> None:
        self.runs.append({"name": name, "inputs": inputs, "run_type": run_type, **kwargs})

    def create_feedback(
        self, run_id: Any, key: str, *, score: float, comment: str | None, session_id: Any
    ) -> None:
        self.feedback.append(
            {
                "run_id": run_id,
                "key": key,
                "score": score,
                "comment": comment,
                "session_id": session_id,
            }
        )


def examples(*keys: str) -> list[Example]:
    return [Example(k, {"question": f"q {k}"}, {"expected": k}) for k in keys]


# ---- masking


def test_mask_removes_phones_cards_and_keys_everywhere() -> None:
    value = {"a": f"call {PHONE}", "b": [f"card {CARD}", {"c": f"key {KEY}"}], "n": 3}
    masked = ls.mask(value)
    text = str(masked)
    assert PHONE not in text and CARD not in text and KEY not in text
    assert masked["n"] == 3
    assert "[PHONE]" in masked["a"] and "[CARD]" in masked["b"][0]


def test_mask_keeps_normal_text_and_shopify_ids() -> None:
    text = "AP Royal Oak ki price kya hai? gid://shopify/Product/9401234567890"
    assert ls.mask(text) == text


def test_mask_turns_sets_into_sorted_lists() -> None:
    assert ls.mask(frozenset({"b", "a"})) == ["a", "b"]


# ---- examples


def test_a_row_is_split_into_inputs_and_expected_outputs() -> None:
    row = {
        "id": "price-01",
        "kind": "price",
        "setup": [],
        "question": f"price? my number {PHONE}",
        "expected": {"amount": "21000.00"},
    }
    example = ls.example_from_row(row)
    assert example.key == "price-01"
    assert set(example.inputs) == {"setup", "question"}
    assert PHONE not in example.inputs["question"]
    assert example.outputs == {"kind": "price", "expected": {"amount": "21000.00"}}


@pytest.mark.parametrize("set_name", list(ls.FILES))
def test_every_file_set_has_examples_with_unique_keys(set_name: str) -> None:
    items = ls.examples_for(set_name)
    assert items
    assert len({e.key for e in items}) == len(items)
    assert all(e.inputs for e in items), "every example needs a customer message"


# ---- datasets


def test_the_first_push_creates_the_dataset_and_every_example() -> None:
    client = FakeClient()
    report = ls.push_dataset(client, "golden", examples("a", "b"))
    assert (report.name, report.added, report.updated, report.unchanged) == (
        "wa-bot-golden",
        2,
        0,
        0,
    )
    dataset = client.datasets["wa-bot-golden"]
    stored = client.examples[dataset.id]
    assert [e.metadata["example_key"] for e in stored] == ["a", "b"]


def test_a_second_push_changes_nothing() -> None:
    client = FakeClient()
    ls.push_dataset(client, "golden", examples("a", "b"))
    report = ls.push_dataset(client, "golden", examples("a", "b"))
    assert (report.added, report.updated, report.unchanged) == (0, 0, 2)
    assert client.updates == 0


def test_a_changed_row_is_updated_and_a_new_one_added() -> None:
    client = FakeClient()
    ls.push_dataset(client, "golden", examples("a", "b"))
    changed = [Example("a", {"question": "new wording"}, {"expected": "a"}), *examples("b", "c")]
    report = ls.push_dataset(client, "golden", changed)
    assert (report.added, report.updated, report.unchanged) == (1, 1, 1)
    dataset = client.datasets["wa-bot-golden"]
    by_key = {e.metadata["example_key"]: e for e in client.examples[dataset.id]}
    assert by_key["a"].inputs == {"question": "new wording"}
    assert set(by_key) == {"a", "b", "c"}  # nothing is deleted


# ---- experiments


def test_an_experiment_links_every_run_to_its_example_and_scores_it() -> None:
    client = FakeClient()
    rows = [
        ExperimentRow("a", {"question": f"q {PHONE}"}, {"reply": "ok"}, {"correct": True}),
        ExperimentRow("b", {"question": "q b"}, {"reply": "wrong"}, {"correct": False}, "why"),
        ExperimentRow("zz", {"question": "not in the set"}, {"reply": "x"}, {"correct": True}),
    ]
    name = ls.record_experiment(
        client, "golden", rows, label="ollama", now=NOW, examples=examples("a", "b")
    )

    assert name == "golden-ollama-20261020-080000"
    (project,) = client.projects
    dataset = client.datasets["wa-bot-golden"]
    assert project["dataset"] == dataset.id
    assert project["metadata"]["label"] == "ollama"
    ids = {e.metadata["example_key"]: e.id for e in client.examples[dataset.id]}
    assert [r["reference_example_id"] for r in client.runs] == [ids["a"], ids["b"], None]
    assert all(r["project_name"] == name for r in client.runs)
    assert PHONE not in str(client.runs)  # masked before it leaves
    assert [(f["key"], f["score"]) for f in client.feedback] == [
        ("correct", 1.0),
        ("correct", 0.0),
        ("correct", 1.0),
    ]
    assert client.feedback[1]["comment"] == "why"
    assert client.feedback[0]["comment"] is None
    assert all(f["session_id"] == project["id"] for f in client.feedback)  # SDK warning fix


def test_without_a_key_there_is_no_client() -> None:
    with pytest.raises(ls.ConfigError):
        ls.make_client(Settings(_env_file=None))  # type: ignore[call-arg]


# ---- converters (our result objects -> experiment rows)


def test_product_qa_results_become_rows() -> None:
    result = SimpleNamespace(
        id="price-01", question="q", reply="Rs 21,000", ok=True, reason="", verdict=""
    )
    natural = SimpleNamespace(
        id="nat-1", question="q", reply="which one?", ok=True, reason="", verdict="clarified"
    )
    rows = ls.rows_from_product_qa([result, natural])
    assert rows[0].scores == {"correct": True}
    assert rows[1].scores == {"correct": True, "answered": False}


def test_language_results_score_the_reply_only_when_there_is_one() -> None:
    case = SimpleNamespace(id="ru-01", text="price kya hai")
    lang = SimpleNamespace(value="roman_ur")
    scored = SimpleNamespace(
        case=case,
        router_language=lang,
        reply="x",
        router_ok=True,
        reply_ok=False,
        scored_reply=True,
    )
    unscored = SimpleNamespace(
        case=case,
        router_language=None,
        reply="",
        router_ok=False,
        reply_ok=False,
        scored_reply=False,
    )
    first, second = ls.rows_from_language([scored, unscored])
    assert first.scores == {"router_language": True, "reply_language": False}
    assert second.scores == {"router_language": False}
    assert second.outputs["router_language"] is None


def test_tool_and_security_results_become_rows() -> None:
    case = SimpleNamespace(id="t-1", texts=["a", "b"], required="get_price")
    outcome = SimpleNamespace(
        case=case,
        ran=["search_products"],
        intent="price",
        reply="r",
        ok=False,
        missing=True,
        violations=[],
    )
    (tool_row,) = ls.rows_from_tools([outcome])
    assert tool_row.scores == {"right_tools": False}
    assert "missing get_price" in tool_row.comment

    attack = SimpleNamespace(
        row=SimpleNamespace(id="adv-001", text="ignore"),
        got=SimpleNamespace(intent=SimpleNamespace(value="abuse_or_injection")),
        ok=True,
        fell_back=False,
    )
    (security_row,) = ls.rows_from_security([attack])
    assert security_row.scores == {"safe_intent": True}


def test_every_set_that_asks_the_bot_has_a_converter() -> None:
    assert set(ls.CONVERTERS) == {"golden", "natural", "language", "tools", "security"}
    assert set(ls.CONVERTERS) <= set(ls.SETS)


# ---- tracing privacy


@pytest.fixture
def clean_tracing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", *HIDE_VARIABLES):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


def tracing_settings(**values: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, langsmith_tracing=True, langsmith_api_key="lsv2-test-key", **values
    )


def test_hide_text_tells_langsmith_to_drop_inputs_and_outputs(clean_tracing_env: None) -> None:
    assert configure_tracing(tracing_settings(langsmith_hide_text=True))
    assert all(os.environ[name] == "true" for name in HIDE_VARIABLES)


def test_without_hide_text_the_variables_are_removed(clean_tracing_env: None) -> None:
    os.environ["LANGSMITH_HIDE_INPUTS"] = "true"  # left over from an earlier call
    assert configure_tracing(tracing_settings())
    assert not any(name in os.environ for name in HIDE_VARIABLES)


PRODUCTION = {
    "app_env": "production",
    "shopify_shop_domain": "shop.example.com",
    "whatsapp_app_secret": "real-app-secret",
    "whatsapp_access_token": "real-access-token",
    "whatsapp_verify_token": "real-verify-token",
    "phone_hash_pepper": "real-pepper-value",
    "handoff_channel": "email",
}


def test_production_refuses_tracing_with_customer_text() -> None:
    with pytest.raises(ValueError, match="LANGSMITH_HIDE_TEXT"):
        tracing_settings(**PRODUCTION)


def test_production_allows_tracing_without_customer_text() -> None:
    assert tracing_settings(**PRODUCTION, langsmith_hide_text=True).langsmith_hide_text
