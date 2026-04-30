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

# M365 Copilot wraps internal chain-of-thought between literal Show**...**Hide
# markers, prefixes search/code-interpreter activity with banner text, and
# leaves "Coding and executing ```python ... ```" blocks in the response.
# None of that should reach the user; strip it before display and before the
# tag scanner sees it.
_COPILOT_NOISE_PATTERNS = [
    re.compile(r"Show\*\*[^*]*\*\*", re.DOTALL),
    re.compile(r"\*\*Hide", re.DOTALL),
    re.compile(r"Coding and executing```[^`]*```", re.DOTALL),
    re.compile(r'\{"executedCode":[^}]*"outputFiles":\[\]\}', re.DOTALL),
    re.compile(r"OK,? I'?ll search for [^\n]*\n", re.IGNORECASE),
    re.compile(r"^Copilot said:\s*\nCopilot\s*\n", re.MULTILINE),
    re.compile(r"^Lining things up\.\.\.\s*\n", re.MULTILINE),
    re.compile(r"^Generating response\s*\n", re.MULTILINE),
]


def strip_copilot_noise(text: str) -> str:
    """Remove M365 Copilot UI / chain-of-thought artifacts that bleed into
    the streamed reply. Idempotent — safe to call repeatedly during a
    streaming render."""
    for pat in _COPILOT_NOISE_PATTERNS:
        text = pat.sub("", text)
    return text


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
        """Text safe to display to the user (everything outside an in-progress
        tag, with Copilot UI artifacts stripped)."""
        raw = self.buffer
        # Hide content from the first unmatched opening tag onward so partial
        # tool_use XML doesn't leak to the terminal.
        for marker in (TOOL_OPEN, FINAL_OPEN):
            idx = raw.rfind(marker)
            if idx != -1:
                close = TOOL_CLOSE if marker == TOOL_OPEN else FINAL_CLOSE
                if raw.find(close, idx) == -1:
                    raw = raw[:idx]
                    break
        return strip_copilot_noise(raw[self._consumed_to:])

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
        # Tool call has priority over final.
        # Walk all <tool_use>...</tool_use> blocks in order; return the first
        # that is well-formed. Models often quote the format as an example
        # ("emit <tool_use>...</tool_use>") — that block has no <name>/<args>
        # and would crash the loop, so we skip past it instead of raising.
        cursor = 0
        while True:
            t_open = self.buffer.find(TOOL_OPEN, cursor)
            if t_open == -1:
                break
            t_close = self.buffer.find(TOOL_CLOSE, t_open + 1)
            if t_close == -1:
                break
            block = self.buffer[t_open + len(TOOL_OPEN): t_close]
            raw = self.buffer[t_open: t_close + len(TOOL_CLOSE)]
            try:
                return _parse_tool_block(block, raw)
            except ValueError:
                cursor = t_close + len(TOOL_CLOSE)
                continue

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
    args = _parse_lenient_json_object(args_text) if args_text else {}
    if not isinstance(args, dict):
        raise ValueError(f"args must be a JSON object, got {type(args).__name__}")
    return ToolCall(name=name, args=args, raw=raw)


def _parse_lenient_json_object(text: str) -> dict:
    """Parse a string that should be a JSON object, tolerating common
    streaming/model artifacts: surrounding ```json fences, leading prose,
    trailing extra closing braces (the model sometimes echoes a brace from
    an example), and whitespace.
    """
    s = text.strip()
    # Strip ```json / ``` fences.
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    s = s.strip()
    # Try direct parse first.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Brace-balanced extraction: find the first '{', then walk forward
    # respecting string escapes until we find the matching '}'. Anything
    # before/after that range is dropped.
    start = s.find("{")
    if start < 0:
        raise ValueError(f"args is not valid JSON: {text!r}")
    depth = 0
    in_str = False
    escape = False
    end = -1
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    if end < 0:
        raise ValueError(f"args is not valid JSON: {text!r}")
    candidate = s[start:end]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise ValueError(f"args is not valid JSON: {text!r} ({e})") from e
