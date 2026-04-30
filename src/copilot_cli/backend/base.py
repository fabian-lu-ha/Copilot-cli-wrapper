from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Literal


@dataclass
class ChatTurn:
    role: Literal["user", "assistant", "system"]
    content: str


class CopilotBackend(ABC):
    """Abstract chat backend. Implementations: Playwright (default), Graph API (future)."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def new_conversation(self) -> None:
        """Reset upstream chat state."""

    @abstractmethod
    async def send(self, prompt: str) -> AsyncIterator[str]:
        """Send a single prompt; yield response chunks as they arrive."""

    async def list_models(self) -> list[str]:
        """Return models the backend exposes. Empty list means unknown."""
        return []

    async def set_model(self, name: str) -> bool:
        """Best-effort switch to the named model. Returns True on success."""
        return False

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.stop()
