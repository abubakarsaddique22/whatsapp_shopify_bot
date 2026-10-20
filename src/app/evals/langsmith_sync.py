"""Phase 12, Step 7: the evaluation sets as LangSmith datasets, and every eval run as an experiment.

    uv run python scripts\\langsmith_datasets.py --dry-run      what would be uploaded (no network)
    uv run python scripts\\langsmith_datasets.py                upload / update every dataset
    uv run python scripts\\run_eval.py --set language --ollama --langsmith
                                                               the run is saved as an experiment

Datasets (one per evaluation set, name `wa-bot-<set>`): one example per row of the evals/*.jsonl
file (tools: per case of evals/tools.build_cases). The example's metadata holds `example_key` (the
row id), so an upload is idempotent: a new row is added, a changed row is updated, an unchanged row
is left alone. Rows are never deleted here (an old experiment still points at them).

Experiment: a LangSmith project linked to the dataset, one run per question (inputs, the bot's
reply, linked to the example) and one feedback score per check (correct, router_language, ...).
The scoring is OURS (src/app/evals/*): LangSmith only stores and compares it.

Privacy (Blueprint Phase 12 card: 'privacy masking'): every string that leaves this machine goes
through `mask()` = the log redaction (phone numbers, card numbers, tokens, keys). The sets are
synthetic (no real customer message is ever in evals/), and the masking makes sure that stays true
even if a real number slips into a file. Tracing of the live bot has its own switch:
LANGSMITH_HIDE_TEXT (core/tracing.py), required in production.

Nothing here runs without LANGSMITH_API_KEY, and the unit tests use a fake client.
"""

import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.core.errors import ConfigError
from app.core.logging import redact
from app.core.settings import Settings, get_settings

ROOT = Path(__file__).resolve().parents[3]
EVALS = ROOT / "evals"
DATASET_PREFIX = "wa-bot-"
KEY_FIELD = "example_key"
# fields of a row that are what the CUSTOMER says (inputs); every other field is the expected answer
INPUT_FIELDS = ("setup", "question", "text", "query_en")
FILES: dict[str, str] = {
    "golden": "product_qa.jsonl",
    "natural": "natural_qa.jsonl",
    "language": "router_golden.jsonl",
    "retrieval": "rag_golden.jsonl",
    "security": "adversarial.jsonl",
}
SETS = (*FILES, "tools")


# ---- masking


def mask(value: Any) -> Any:
    """The same value with every string redacted (dicts and lists walked through)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {str(k): mask(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = sorted(value) if isinstance(value, set | frozenset) else value
        return [mask(v) for v in items]
    return value


# ---- dataset examples


@dataclass(frozen=True)
class Example:
    key: str  # the row id, stored as metadata.example_key
    inputs: dict[str, Any]
    outputs: dict[str, Any]


def dataset_name(set_name: str) -> str:
    return f"{DATASET_PREFIX}{set_name}"


def example_from_row(row: Mapping[str, Any]) -> Example:
    inputs = {k: row[k] for k in INPUT_FIELDS if k in row}
    outputs = {k: v for k, v in row.items() if k != "id" and k not in INPUT_FIELDS}
    return Example(str(row["id"]), mask(inputs), mask(outputs))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def tools_examples() -> list[Example]:
    from app.evals import tools  # here, not at the top: it reads the eval files when it runs

    return [
        Example(
            case.id,
            mask({"messages": case.texts}),
            mask({"group": case.group, "required": case.required, "forbidden": case.forbidden}),
        )
        for case in tools.build_cases()
    ]


def examples_for(set_name: str) -> list[Example]:
    """The examples of one set, from its file. KeyError for an unknown set."""
    if set_name == "tools":
        return tools_examples()
    examples = [example_from_row(row) for row in read_jsonl(EVALS / FILES[set_name])]
    keys = [e.key for e in examples]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{FILES[set_name]} has a duplicate id")
    return examples


# ---- the client (the subset of langsmith.Client that is used, so a fake can stand in)


class Client(Protocol):
    def has_dataset(self, *, dataset_name: str) -> bool: ...
    def create_dataset(self, dataset_name: str, *, description: str = ...) -> Any: ...
    def read_dataset(self, *, dataset_name: str) -> Any: ...
    def list_examples(self, *, dataset_id: Any) -> Iterable[Any]: ...
    def create_example(
        self, *, inputs: dict, outputs: dict, metadata: dict, dataset_id: Any
    ) -> Any: ...
    def update_example(
        self, example_id: Any, *, inputs: dict, outputs: dict, metadata: dict
    ) -> Any: ...
    def create_project(
        self, project_name: str, *, reference_dataset_id: Any, metadata: dict
    ) -> Any: ...
    def create_run(self, name: str, inputs: dict, run_type: str, **kwargs: Any) -> Any: ...
    def create_feedback(
        self, run_id: Any, key: str, *, score: float, comment: str | None, session_id: Any
    ) -> Any: ...


def make_client(settings: Settings | None = None) -> Client:
    """The real LangSmith client. ConfigError without LANGSMITH_API_KEY."""
    current = settings or get_settings()
    key = current.langsmith_api_key.get_secret_value()
    if not key:
        raise ConfigError("LANGSMITH_API_KEY is empty: datasets and experiments need it")
    from langsmith import Client as LangSmithClient  # only when really used

    return LangSmithClient(api_key=key)  # type: ignore[return-value]


@dataclass
class PushReport:
    name: str
    added: int = 0
    updated: int = 0
    unchanged: int = 0


def push_dataset(client: Client, set_name: str, examples: Sequence[Example]) -> PushReport:
    """Create the dataset if needed, then add new rows and update changed ones (by example_key)."""
    name = dataset_name(set_name)
    if client.has_dataset(dataset_name=name):
        dataset = client.read_dataset(dataset_name=name)
    else:
        dataset = client.create_dataset(
            name, description=f"evals: {set_name} (synthetic, masked; from the repo, Phase 12)"
        )
    existing: dict[str, Any] = {}
    for example in client.list_examples(dataset_id=dataset.id):
        key = (example.metadata or {}).get(KEY_FIELD)
        if key:
            existing[key] = example
    report = PushReport(name)
    for example in examples:
        metadata = {KEY_FIELD: example.key, "set": set_name}
        old = existing.get(example.key)
        if old is None:
            client.create_example(
                inputs=example.inputs,
                outputs=example.outputs,
                metadata=metadata,
                dataset_id=dataset.id,
            )
            report.added += 1
        elif old.inputs != example.inputs or old.outputs != example.outputs:
            client.update_example(
                old.id, inputs=example.inputs, outputs=example.outputs, metadata=metadata
            )
            report.updated += 1
        else:
            report.unchanged += 1
    return report


# ---- experiments


@dataclass
class ExperimentRow:
    """One question of an eval run, as LangSmith stores it."""

    key: str  # the example_key (row id)
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    scores: dict[str, bool | float] = field(default_factory=dict)
    comment: str = ""


def experiment_name(set_name: str, label: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{set_name}-{label}-{stamp}"


def record_experiment(
    client: Client,
    set_name: str,
    rows: Sequence[ExperimentRow],
    *,
    label: str,
    metadata: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    examples: Sequence[Example] | None = None,
) -> str:
    """Save one eval run as an experiment on the set's dataset. Returns the experiment name.

    The dataset is brought up to date first, so every row can be linked to its example. A row
    whose key is not in the dataset is still stored, only without the link.
    """
    push_dataset(client, set_name, examples_for(set_name) if examples is None else examples)
    dataset = client.read_dataset(dataset_name=dataset_name(set_name))
    example_ids = {
        (e.metadata or {}).get(KEY_FIELD): e.id for e in client.list_examples(dataset_id=dataset.id)
    }
    name = experiment_name(set_name, label, now)
    project = client.create_project(
        name,
        reference_dataset_id=dataset.id,
        metadata={"set": set_name, "label": label, **(metadata or {})},
    )
    moment = now or datetime.now(UTC)
    for row in rows:
        run_id = uuid.uuid4()
        client.create_run(
            f"eval-{set_name}",
            mask(row.inputs),
            "chain",
            id=run_id,
            project_name=name,
            outputs=mask(row.outputs),
            reference_example_id=example_ids.get(row.key),
            start_time=moment,
            end_time=moment,
            extra={"metadata": {KEY_FIELD: row.key}},
        )
        for score_key, value in row.scores.items():
            client.create_feedback(
                run_id,
                score_key,
                score=float(value),
                comment=mask(row.comment) or None,
                session_id=project.id,  # newer SDKs want the experiment's id with every score
            )
    return name


# ---- from our result objects to experiment rows


def rows_from_product_qa(results: Iterable[Any]) -> list[ExperimentRow]:
    """product_qa.Result (golden and natural)."""
    out = []
    for r in results:
        scores: dict[str, bool | float] = {"correct": r.ok}
        if r.verdict:
            scores["answered"] = r.verdict == "correct"
        out.append(
            ExperimentRow(r.id, {"question": r.question}, {"reply": r.reply}, scores, r.reason)
        )
    return out


def rows_from_language(results: Iterable[Any]) -> list[ExperimentRow]:
    """language.LanguageResult."""
    out = []
    for r in results:
        scores: dict[str, bool | float] = {"router_language": r.router_ok}
        if r.scored_reply:
            scores["reply_language"] = r.reply_ok
        routed = r.router_language.value if r.router_language else None
        out.append(
            ExperimentRow(
                r.case.id,
                {"text": r.case.text},
                {"reply": r.reply, "router_language": routed},
                scores,
            )
        )
    return out


def rows_from_tools(outcomes: Iterable[Any]) -> list[ExperimentRow]:
    """tools.ToolOutcome."""
    out = []
    for o in outcomes:
        problems = [*([f"missing {o.case.required}"] if o.missing else []), *o.violations]
        out.append(
            ExperimentRow(
                o.case.id,
                {"messages": o.case.texts},
                {"tools": o.ran, "intent": o.intent, "reply": o.reply},
                {"right_tools": o.ok},
                "; ".join(problems),
            )
        )
    return out


def rows_from_security(results: Iterable[Any]) -> list[ExperimentRow]:
    """security.RouterResult (the customer messages; the store-text scan has no model)."""
    return [
        ExperimentRow(
            r.row.id,
            {"text": r.row.text},
            {"intent": r.got.intent.value},
            {"safe_intent": r.ok},
            "router fell back (provider)" if r.fell_back else "",
        )
        for r in results
    ]


CONVERTERS: dict[str, Callable[[Iterable[Any]], list[ExperimentRow]]] = {
    "golden": rows_from_product_qa,
    "natural": rows_from_product_qa,
    "language": rows_from_language,
    "tools": rows_from_tools,
    "security": rows_from_security,
}
