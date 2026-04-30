"""Per-tool approval gates with a session allowlist.

Decisions:
- "once": allow this single call.
- "session": allow this tool for the rest of the session (any args).
- "prefix": for run_bash, allow any command starting with the same first token (e.g. 'git').
- "deny": refuse this call.

There is also a global "yolo" / autopilot mode where every tool is auto-approved.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Set

from rich.console import Console
from rich.prompt import Prompt

from copilot_cli.tools.registry import Tool


@dataclass
class PermissionState:
    autopilot: bool = False
    session_allowed_tools: Set[str] = field(default_factory=set)
    bash_allowed_prefixes: Set[str] = field(default_factory=set)


class PermissionGate:
    def __init__(self, state: PermissionState, console: Console) -> None:
        self.state = state
        self.console = console

    def check(self, tool: Tool, args: dict) -> tuple[bool, str]:
        """Returns (allowed, reason)."""
        if not tool.requires_approval:
            return True, "auto: read-only tool"
        if self.state.autopilot:
            return True, "auto: autopilot mode"
        if tool.name in self.state.session_allowed_tools:
            return True, "auto: allowed for session"
        if tool.name == "run_bash":
            cmd = args.get("command", "")
            try:
                first = shlex.split(cmd)[0] if cmd else ""
            except ValueError:
                first = cmd.split()[0] if cmd.split() else ""
            if first and first in self.state.bash_allowed_prefixes:
                return True, f"auto: '{first}' allowed for session"

        return self._prompt(tool, args)

    def _prompt(self, tool: Tool, args: dict) -> tuple[bool, str]:
        c = self.console
        c.print()
        c.rule(f"[bold yellow]Approve {tool.name}?")
        for k in tool.sensitive_args or args.keys():
            v = args.get(k, "")
            display = v if len(str(v)) < 400 else str(v)[:400] + "..."
            c.print(f"  [bold]{k}[/]: {display}")
        c.rule()
        choices = ["y", "s", "a", "n"]
        labels = "[y]es once / [s]ession (this tool) / [a]lways prefix (bash) / [n]o"
        if tool.name != "run_bash":
            choices = ["y", "s", "n"]
            labels = "[y]es once / [s]ession (this tool) / [n]o"
        ans = Prompt.ask(labels, choices=choices, default="y")
        if ans == "y":
            return True, "approved once"
        if ans == "s":
            self.state.session_allowed_tools.add(tool.name)
            return True, "approved for session"
        if ans == "a" and tool.name == "run_bash":
            cmd = args.get("command", "")
            try:
                first = shlex.split(cmd)[0]
            except (ValueError, IndexError):
                first = cmd.split()[0] if cmd.split() else ""
            if first:
                self.state.bash_allowed_prefixes.add(first)
                return True, f"prefix '{first}' approved for session"
        return False, "denied by user"
