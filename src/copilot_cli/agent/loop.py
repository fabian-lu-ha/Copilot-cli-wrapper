"""Agent loop: send prompt -> stream -> detect tool_use -> execute -> repeat."""
from __future__ import annotations

import logging
from rich.console import Console

from copilot_cli.agent.parser import FinalAnswer, StreamingParser, ToolCall
from copilot_cli.agent.system_prompt import build_system_prompt
from copilot_cli.agent.transcript import Transcript
from copilot_cli.backend.base import CopilotBackend
from copilot_cli.config import Settings
from copilot_cli.tools.registry import ToolRegistry
from copilot_cli.ui.permissions import PermissionGate
from copilot_cli.ui.render import StreamRenderer

log = logging.getLogger(__name__)

MAX_STEPS_PER_TURN = 25

COMPACT_REQUEST = (
    "Summarize the conversation so far in 8-15 bullet points. Cover: the user's "
    "overall goal, key decisions made, files read or edited (with paths), and "
    "current state of any in-progress task. Reply with ONLY the summary text, "
    "no preamble. Do not call any tool."
)


class AgentLoop:
    def __init__(
        self,
        backend: CopilotBackend,
        registry: ToolRegistry,
        settings: Settings,
        transcript: Transcript,
        permissions: PermissionGate,
        console: Console,
    ) -> None:
        self.backend = backend
        self.registry = registry
        self.settings = settings
        self.transcript = transcript
        self.permissions = permissions
        self.console = console

        if not transcript.messages:
            transcript.add("system", build_system_prompt(settings.workdir, registry))

    async def run_user_turn(self, user_input: str) -> str:
        self.transcript.add("user", user_input)
        await self._maybe_auto_compact()
        # Reset upstream once per user turn and prime with the full transcript
        # (system prompt + all prior turns + this user message). Subsequent
        # tool-result steps within the same turn are sent as follow-up
        # messages, so Copilot's own session memory carries them.
        await self.backend.new_conversation()
        next_prompt: str = self.transcript.render_for_upstream()
        last_text = ""

        for step in range(MAX_STEPS_PER_TURN):
            parser = StreamingParser()
            renderer = StreamRenderer(self.console)
            self.console.print()
            self.console.rule(f"[dim]assistant (step {step + 1})")

            assistant_text = ""
            async for chunk in self.backend.send(next_prompt):
                parser.feed(chunk)
                assistant_text = parser.buffer
                renderer.render(parser.visible_text)
                if parser.pop_complete() is not None:
                    break

            renderer.newline()
            self.transcript.add("assistant", assistant_text.strip())

            event = parser.pop_complete()
            if isinstance(event, FinalAnswer):
                last_text = event.text
                self.console.rule("[dim]done")
                return event.text
            if isinstance(event, ToolCall):
                tool_output = await self._dispatch_tool(event)
                tool_msg = self._format_tool_result(event.name, tool_output)
                self.transcript.add("tool", tool_msg)
                self.console.print(f"[dim]→ tool {event.name}: {self._snippet(tool_output)}")
                next_prompt = tool_msg  # follow-up message in the same upstream chat
                continue

            # No tool call, no final marker. Treat plain prose as the answer.
            last_text = assistant_text.strip()
            return last_text

        self.console.print("[red]hit max steps for this turn[/]")
        return last_text

    async def _dispatch_tool(self, call: ToolCall) -> str:
        tool = self.registry.get(call.name)
        if tool is None:
            return f"ERROR: unknown tool {call.name!r}"
        ok, reason = self.permissions.check(tool, call.args)
        if not ok:
            return f"ERROR: tool call denied ({reason})"
        try:
            return await tool.handler(call.args)
        except Exception as e:
            log.exception("tool %s raised", call.name)
            return f"ERROR: {type(e).__name__}: {e}"

    @staticmethod
    def _format_tool_result(name: str, output: str) -> str:
        return (
            f"<tool_result>\n  <name>{name}</name>\n"
            f"  <output>{output}</output>\n</tool_result>"
        )

    @staticmethod
    def _snippet(s: str, n: int = 120) -> str:
        s = s.replace("\n", " ")
        return s if len(s) <= n else s[:n] + "..."

    async def _maybe_auto_compact(self) -> None:
        used = self.transcript.estimated_tokens()
        budget = self.settings.context_window_tokens
        ratio = used / budget if budget else 0
        if ratio >= self.settings.context_warn_ratio:
            self.console.print(
                f"[yellow]context: {used:,}/{budget:,} tokens "
                f"({ratio*100:.0f}%)[/]"
            )
        if ratio >= self.settings.context_auto_compact_ratio:
            self.console.print("[yellow]auto-compacting transcript…[/]")
            await self.compact()

    async def compact(self) -> None:
        """Ask the backend itself to summarize, then collapse the transcript."""
        await self.backend.new_conversation()
        prompt = self.transcript.render_for_upstream() + "\n\nUser: " + COMPACT_REQUEST
        summary = ""
        async for chunk in self.backend.send(prompt):
            summary += chunk
        new_tokens = self.transcript.compact(summary.strip(), keep_last_turns=4)
        self.console.print(f"[green]compacted to {new_tokens:,} tokens[/]")
