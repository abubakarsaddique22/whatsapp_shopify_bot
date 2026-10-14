"""Phase 5, step 3: the one thing the rest of the app knows about an LLM.

Any model (Groq, the fake in tests, later Ollama) only has to offer `complete()`. The router
(step 4) and later the composer call it and never import a provider package. Parsing JSON and
repairing it is NOT done here: that is the router's job, so this file stays tiny.

Rules for every implementation:
- Any failure (timeout, HTTP error, empty answer, wrong key) is raised as `ProviderError`.
  Never let a provider-specific exception leave this package.
- Never log the prompts or the answer (they contain customer messages) and never put the
  API key in an error message.
"""

from typing import Protocol


class ChatModel(Protocol):
    async def complete(self, system: str, user: str) -> str:
        """Send one system prompt and one user message; return the model's text answer.

        Raises `app.core.errors.ProviderError` if no usable answer came back.
        """
        ...
