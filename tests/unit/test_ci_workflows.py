"""Phase 12, Step 8: the CI gate itself is checked, so a later edit cannot quietly drop a step.

Blueprint 11.6: ruff, mypy, pytest -m "not llm", pip-audit and a gitleaks secret scan on every push
and pull request, plus a separate nightly or manual job with the real model. Phase 12 adds the
failure drills and the T01 to T25 map check to the gate.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as True
    return workflow.get("on") or workflow.get(True) or {}


def run_lines(workflow: dict[str, Any]) -> list[str]:
    return [
        step["run"] for job in workflow["jobs"].values() for step in job["steps"] if "run" in step
    ]


def uses(workflow: dict[str, Any]) -> list[str]:
    return [
        step["uses"] for job in workflow["jobs"].values() for step in job["steps"] if "uses" in step
    ]


def test_ci_runs_on_every_push_to_main_and_every_pull_request() -> None:
    on = triggers(load("ci.yml"))
    assert on["push"]["branches"] == ["main"]
    assert "pull_request" in on


@pytest.mark.parametrize(
    "command",
    [
        "alembic upgrade head",
        "ruff check .",
        "mypy src",
        'pytest -m "not llm and not live"',
        "scripts/failure_drills.py",
        "scripts/test_map.py --check",
        "pip-audit",
    ],
)
def test_ci_runs_every_gate(command: str) -> None:
    assert any(command in line for line in run_lines(load("ci.yml"))), command


def test_no_gate_in_ci_is_allowed_to_fail() -> None:
    for job in load("ci.yml")["jobs"].values():
        for step in job["steps"]:
            assert not step.get("continue-on-error"), step.get("name")


def test_ci_scans_every_commit_for_secrets() -> None:
    workflow = load("ci.yml")
    assert any(u.startswith("gitleaks/gitleaks-action@") for u in uses(workflow))
    (job,) = [
        j
        for j in workflow["jobs"].values()
        if any("gitleaks" in s.get("uses", "") for s in j["steps"])
    ]
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0


def test_the_gitleaks_allowlist_stays_narrow() -> None:
    text = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    assert "useDefault = true" in text
    assert "^src/" not in text and "^evals/" not in text


def test_the_eval_job_runs_at_night_and_by_hand() -> None:
    on = triggers(load("eval.yml"))
    assert on["schedule"] and "workflow_dispatch" in on


def test_the_eval_job_gates_on_security_with_the_real_model() -> None:
    workflow = load("eval.yml")
    (job,) = workflow["jobs"].values()
    assert job["env"]["LLM_PROVIDER"] == "groq"
    security = [s for s in job["steps"] if "--set security" in s.get("run", "")]
    assert security and not security[0].get("continue-on-error")


@pytest.mark.parametrize("name", ["ci.yml", "eval.yml"])
def test_secrets_are_only_passed_as_environment_never_written_into_a_command(name: str) -> None:
    workflow = load(name)
    assert workflow["permissions"] == {"contents": "read"}
    for line in run_lines(workflow):
        assert "secrets." not in line
