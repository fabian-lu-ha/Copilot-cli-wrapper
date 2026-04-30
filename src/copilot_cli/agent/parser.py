"""Streaming parser that detects tool_use blocks mid-stream.

The agent loop feeds chunks in via .feed(chunk) and asks .pop_complete()
after each. When a complete <tool_use>...</tool_use> block is found the
parser returns it; the caller then aborts the upstream stream and
executes the tool.

It also recognizes <final>...</final> as a "done" marker.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional


TOOL_OPEN = "<tool_use>"
TOOL_CLOSE = "</tool_use>"
FINAL_OPEN = "<final>"
FINAL_CLOSE = "</final>"

_NAME_RE = re.compile(r"<name>\s*(.*?)\s*</name>", re.DOTALL)
_ARGS_RE = re.compile(r"<args>\s*(.*?)\s*</args>", re.DOTALL)


@dataclass
class ToolCall:
    name: str
    args: dict
    raw: str


@dataclass
class FinalAnswer:
    text: str


class StreamingParser:
    def __init__(self) -> None:
        self.buffer: str = ""
        self._consumed_to: int = 0

    def feed(self, chunk: str) -> None:
        self.buffer += chunk

    @property
    def visible_text(self) -> str:
        """Text safe to display to the user (everything outside an in-progress tag)."""
        # Hide content from the first unmatched opening tag onward to avoid
        # leaking partial tool_use XML to the terminal.
        for marker in (TOOL_OPEN, FINAL_OPEN):
            idx = self.buffer.rfind(marker)
            if idx != -1:
                close = TOOL_CLOSE if marker == TOOL_OPEN else FINAL_CLOSE
                if self.buffer.find(close, idx) == -1:
                    return self.buffer[self._consumed_to:idx]
        return self.buffer[self._consumed_to:]

    def mark_displayed(self) -> None:
        self._consumed_to = len(self._strip_after_open())

    def _strip_after_open(self) -> str:
        for marker in (TOOL_OPEN, FINAL_OPEN):
            idx = self.buffer.rfind(marker)
            if idx != -1:
                close = TOOL_CLOSE if marker == TOOL_OPEN else FINAL_CLOSE
                if self.buffer.find(close, idx) == -1:
                    return self.buffer[:idx]
        return self.buffer

    def pop_complete(self) -> Optional[ToolCall | FinalAnswer]:
        # Tool call has priority if both present.
        t_open = self.buffer.find(TOOL_OPEN)
        t_close = self.buffer.find(TOOL_CLOSE, t_open + 1) if t_open != -1 else -1
        if t_open != -1 and t_close != -1:
            block = self.buffer[t_open + len(TOOL_OPEN): t_close]
            return _parse_tool_block(block, self.buffer[t_open: t_close + len(TOOL_CLOSE)])

        f_open = self.buffer.find(FINAL_OPEN)
        f_close = self.buffer.find(FINAL_CLOSE, f_open + 1) if f_open != -1 else -1
        if f_open != -1 and f_close != -1:
            text = self.buffer[f_open + len(FINAL_OPEN): f_close].strip()
            return FinalAnswer(text=text)

        return None


def _parse_tool_block(block: str, raw: str) -> ToolCall:
    name_m = _NAME_RE.search(block)
    args_m = _ARGS_RE.search(block)
    if not name_m or not args_m:
        raise ValueError(f"Malformed tool_use block: {block!r}")
    name = name_m.group(1).strip()
    args_text = args_m.group(1).strip()
    try:
        args = json.loads(args_text) if args_text else {}
    except json.JSONDecodeError as e:
        # Try a lenient cleanup: strip code fences.
        cleaned = re.sub(r"^```(?:json)?|```$", "", args_text, flags=re.MULTILINE).strip()
        try:
            args = json.loads(cleaned)
        except json.JSONDecodeError:
            raise ValueError(f"args is not valid JSON: {args_text!r} ({e})") from e
    if not isinstance(args, dict):
        raise ValueError(f"args must be a JSON object, got {type(args).__name__}")
    return ToolCall(name=name, args=args, raw=raw)
