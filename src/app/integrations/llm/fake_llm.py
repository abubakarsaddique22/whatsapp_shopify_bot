"""A pretend LLM for tests: no network, no key, always the same answer (Phase 5, step 3).

You decide what it says. Each call to `complete()` returns the next prepared answer. If the
prepared item is an exception (for example `ProviderError`), it is raised instead, which is how
we test timeouts and failures. When the answers run out it raises `ProviderError` too, so a test
that makes one call too many fails loudly instead of getting a made-up reply.

    llm = FakeLLM(["not json", '{"language": "en", ...}'])   # first call bad, second good
    await llm.complete("system", "user")                       # -> "not json"
    llm.calls                                                  # -> [("system", "user")]

Used by the router tests (invalid JSON gets one repair) and, later, by the agent tests.
"""

from app.core.errors import ProviderError


class FakeLLM:
    def __init__(self, answers: list[str | Exception]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, str]] = []  # (system, user) of every call, in order

    async def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self._answers:
            raise ProviderError("FakeLLM has no more prepared answers")
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
