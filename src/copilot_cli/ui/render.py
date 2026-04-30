from __future__ import annotations

from rich.console import Console


class StreamRenderer:
    """Prints assistant tokens to the terminal as they arrive, hiding any
    in-progress tool_use XML so the user only sees prose."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self._printed = 0

    def render(self, visible_text: str) -> None:
        if len(visible_text) > self._printed:
            new = visible_text[self._printed:]
            self.console.print(new, end="", soft_wrap=True, highlight=False)
            self._printed = len(visible_text)

    def newline(self) -> None:
        self.console.print()
        self._printed = 0
