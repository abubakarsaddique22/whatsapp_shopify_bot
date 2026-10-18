"""Fixtures of the adversarial tests."""

import pytest

from tests.adversarial.harness import Bot, install_fakes


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> Bot:
    """The real graph with fake tools, a FakeLLM and fake secrets (see harness.py)."""
    return install_fakes(monkeypatch)
