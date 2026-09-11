from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class AgentClientError(RuntimeError):
    """The agent could not complete a request."""


class AgentClientCancelled(AgentClientError):
    """The user cancelled the current request."""


class AgentClient(Protocol):
    """Pi client surface used by channels and their test doubles."""

    async def chat(
        self, messages: list[dict[str, str]], trace_id: str, *, session_key: str | None = None
    ) -> str: ...

    def chat_stream(
        self, messages: list[dict[str, str]], trace_id: str, *, session_key: str | None = None
    ) -> AsyncIterator[str]: ...

    def cancel(self, trace_id: str) -> bool: ...

    def reset_session(self, session_key: str) -> None: ...

    async def close(self) -> None: ...
