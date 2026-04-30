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
MAX_RETRIES_PER_STEP = 2

# Substrings that mean "Copilot didn't actually answer" — the chat bubble
# either showed a server error or a half-rendered loading placeholder. We
# treat these as a retry-worthy failure of the step.
TRANSIENT_FAILURE_NEEDLES = (
    "Something went wrong",
    "Please try again later",
    "Lining things up",
    "Generating response",
)


def _looks_like_transient_failure(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    if len(t) < 20:
        # Empty or near-empty replies are not real answers.
        return True
    return any(n in t for n in TRANSIENT_FAILURE_NEEDLES)

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
        last_text = ""

        for step in range(MAX_STEPS_PER_TURN):
            # Reset upstream and replay the FULL transcript every step. Copilot
            # doesn't natively understand <tool_result> as a tool reply — to
            # it, tags are just text. Without replaying the full back-and-forth
            # the model loses its own previous <tool_use> turn and tends to
            # re-emit it (or wander off into Pages / Code Interpreter).
            await self.backend.new_conversation()
            upstream_prompt = self.transcript.render_for_upstream()

            parser = StreamingParser()
            renderer = StreamRenderer(self.console)
            self.console.print()
            self.console.rule(f"[dim]assistant (step {step + 1})")

            # Drain the entire streamed response. We used to break the
            # second </tool_use> arrived, but cutting the model off mid-
            # generation means anything it was about to add (e.g. trailing
            # commitment text) gets dropped, and the upstream chat is left
            # in a half-finished state that confuses the next turn. Letting
            # the stream end naturally is slower per step but more reliable.
            assistant_text = ""
            event = None
            for attempt in range(MAX_RETRIES_PER_STEP + 1):
                if attempt > 0:
                    self.console.print(
                        f"[yellow]step {step+1}: retry {attempt} "
                        f"after transient/empty response[/]"
                    )
                    await self.backend.new_conversation()
                parser = StreamingParser()
                renderer = StreamRenderer(self.console)
                async for chunk in self.backend.send(upstream_prompt):
                    parser.feed(chunk)
                    assistant_text = parser.buffer
                    renderer.render(parser.visible_text)
                event = parser.pop_complete()
                # Accept anything that produced a real event OR a non-
                # transient prose answer.
                if event is not None or not _looks_like_transient_failure(assistant_text):
                    break

            renderer.newline()
            self.transcript.add("assistant", assistant_text.strip())
            if isinstance(event, FinalAnswer):
                last_text = event.text
                self.console.rule("[dim]done")
                return event.text
            if isinstance(event, ToolCall):
                tool_output = await self._dispatch_tool(event)
                tool_msg = self._format_tool_result(event.name, tool_output)
                self.transcript.add("tool", tool_msg)
                self.console.print(f"[dim]→ tool {event.name}: {self._snippet(tool_output)}")
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
        # Frame the result so Copilot knows the action ran and what to do
        # next. Without this preamble Copilot sometimes re-emits the same
        # <tool_use> (thinking it never executed) or wanders off into
        # Pages / Code Interpreter on the next step.
        return (
            f"<tool_result>\n  <name>{name}</name>\n"
            f"  <output>{output}</output>\n</tool_result>\n\n"
            f"The harness ran your {name} action. The text inside <output> "
            f"above is the actual result from the user's machine. Use it to "
            f"continue:\n"
            f"- If you have everything you need, reply with ONLY "
            f"<final>your answer</final>.\n"
            f"- If you need another action, reply with ONLY a new "
            f"<tool_use>...</tool_use>.\n"
            f"Either way, no preamble, no Pages, no Code Interpreter, no "
            f"prose explaining what you'll do — just the tag."
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
