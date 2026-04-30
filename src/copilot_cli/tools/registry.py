from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional


@dataclass
class Tool:
    name: str
    description: str
    args_schema: str  # human-readable schema, e.g. '{"path": str, "edits": str}'
    handler: Callable[[dict], Awaitable[str]]
    requires_approval: bool = False  # True for write/edit/bash; gated by permission system
    sensitive_args: tuple = ()  # arg keys to display in approval prompt


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name} already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())
