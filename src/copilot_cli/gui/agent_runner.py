"""Qt-signal-emitting agent runner.

Mirrors the CLI's AgentLoop logic but emits Qt signals so the GUI can render
streaming text, tool calls, and final answers without blocking the UI thread.
Runs on the asyncio loop that qasync bridges into Qt's event loop.

Permission prompts are awaited via an asyncio.Future the dialog completes,
so the agent loop blocks naturally until the user clicks Allow/Deny.
"""
from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import QObject, Signal

from copilot_cli.agent.parser import FinalAnswer, StreamingParser, ToolCall
from copilot_cli.agent.system_prompt import build_system_prompt
from copilot_cli.agent.transcript import Transcript
from copilot_cli.backend.base import CopilotBackend
from copilot_cli.config import Settings
from copilot_cli.tools.registry import Tool, ToolRegistry

log = logging.getLogger(__name__)

MAX_STEPS_PER_TURN = 25

COMPACT_REQUEST = (
    "Summarize the conversation so far in 8-15 bullet points. Cover: the user's "
    "overall goal, key decisions made, files read or edited (with paths), and "
    "current state of any in-progress task. Reply with ONLY the summary text, "
    "no preamble. Do not call any tool."
)


class GuiAgentRunner(QObject):
    # Streaming + lifecycle signals.
    user_message = Signal(str)               # user message added to transcript
    assistant_started = Signal()             # turn beginning
    assistant_delta = Signal(str)            # cumulative visible text (not delta)
    assistant_finished = Signal(str)         # final visible text for the bubble
    tool_call_started = Signal(str, dict)    # tool name, args
    tool_call_finished = Signal(str, str)    # tool name, output
    final_answer = Signal(str)
    info = Signal(str)
    error_occurred = Signal(str)
    context_updated = Signal(int, int)       # used, budget
    busy_changed = Signal(bool)

    # Async permission request: emit (tool, args, future). The GUI listens,
    # opens a dialog, and sets the future's result. The runner awaits it.
    permission_requested = Signal(object, dict, object)

    def __init__(
        self,
        backend: CopilotBackend,
        registry: ToolRegistry,
        settings: Settings,
        transcript: Transcript,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.registry = registry
        self.settings = settings
        self.transcript = transcript
        self._session_allowed: set[str] = set()
        self._bash_prefixes: set[str] = set()
        self._autopilot = False

        if not transcript.messages:
            transcript.add("system", build_system_prompt(settings.workdir, registry))

    def set_autopilot(self, enabled: bool) -> None:
        self._autopilot = enabled

    def is_autopilot(self) -> bool:
        return self._autopilot

    async def run_user_turn(self, user_input: str) -> None:
        try:
            self.busy_changed.emit(True)
            self.transcript.add("user", user_input)
            self.user_message.emit(user_input)
            await self._maybe_auto_compact()

            await self.backend.new_conversation()
            next_prompt = self.transcript.render_for_upstream()

            for step in range(MAX_STEPS_PER_TURN):
                self.assistant_started.emit()
                parser = StreamingParser()
                assistant_text = ""

                async for chunk in self.backend.send(next_prompt):
                    parser.feed(chunk)
                    assistant_text = parser.buffer
                    self.assistant_delta.emit(parser.visible_text)
                    if parser.pop_complete() is not None:
                        break

                self.assistant_finished.emit(parser.visible_text)
                self.transcript.add("assistant", assistant_text.strip())
                self._emit_context()

                event = parser.pop_complete()
                if isinstance(event, FinalAnswer):
                    self.final_answer.emit(event.text)
                    return
                if isinstance(event, ToolCall):
                    self.tool_call_started.emit(event.name, event.args)
                    output = await self._dispatch_tool(event)
                    self.tool_call_finished.emit(event.name, output)
                    tool_msg = self._format_tool_result(event.name, output)
                    self.transcript.add("tool", tool_msg)
                    next_prompt = tool_msg
                    self._emit_context()
                    continue

                # Plain prose answer, treat as done.
                return

            self.error_occurred.emit("hit max steps for this turn")
        except Exception as e:
            log.exception("agent turn failed")
            self.error_occurred.emit(f"{type(e).__name__}: {e}")
        finally:
            self.busy_changed.emit(False)

    async def _dispatch_tool(self, call: ToolCall) -> str:
        tool = self.registry.get(call.name)
        if tool is None:
            return f"ERROR: unknown tool {call.name!r}"
        ok, reason = await self._check_permission(tool, call.args)
        if not ok:
            return f"ERROR: tool call denied ({reason})"
        try:
            return await tool.handler(call.args)
        except Exception as e:
            log.exception("tool %s raised", call.name)
            return f"ERROR: {type(e).__name__}: {e}"

    async def _check_permission(self, tool: Tool, args: dict) -> tuple[bool, str]:
        if not tool.requires_approval:
            return True, "auto: read-only"
        if self._autopilot:
            return True, "auto: autopilot"
        if tool.name in self._session_allowed:
            return True, "auto: session-allowed"
        if tool.name == "run_bash":
            cmd = args.get("command", "")
            first = cmd.split()[0] if cmd.split() else ""
            if first and first in self._bash_prefixes:
                return True, f"auto: '{first}' allowed"

        # Hand off to GUI.
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.permission_requested.emit(tool, args, future)
        decision = await future
        if not decision or not decision.allowed:
            return False, "denied by user"
        if decision.scope == "session":
            self._session_allowed.add(tool.name)
        elif decision.scope == "prefix" and decision.prefix:
            self._bash_prefixes.add(decision.prefix)
        return True, f"approved ({decision.scope})"

    async def _maybe_auto_compact(self) -> None:
        used = self.transcript.estimated_tokens()
        budget = self.settings.context_window_tokens
        ratio = used / budget if budget else 0
        if ratio >= self.settings.context_auto_compact_ratio:
            self.info.emit("auto-compacting transcript…")
            await self.compact()

    async def compact(self) -> None:
        await self.backend.new_conversation()
        prompt = self.transcript.render_for_upstream() + "\n\nUser: " + COMPACT_REQUEST
        summary = ""
        async for chunk in self.backend.send(prompt):
            summary += chunk
        new_tokens = self.transcript.compact(summary.strip(), keep_last_turns=4)
        self.info.emit(f"compacted to {new_tokens:,} tokens")
        self._emit_context()

    def _emit_context(self) -> None:
        used = self.transcript.estimated_tokens()
        self.context_updated.emit(used, self.settings.context_window_tokens)

    @staticmethod
    def _format_tool_result(name: str, output: str) -> str:
        return (
            f"<tool_result>\n  <name>{name}</name>\n"
            f"  <output>{output}</output>\n</tool_result>"
        )
