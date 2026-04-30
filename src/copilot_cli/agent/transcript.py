from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Literal

from copilot_cli.config import sessions_dir
from copilot_cli.tokenizer import count_tokens


@dataclass
class Message:
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    ts: float = field(default_factory=time.time)


@dataclass
class Transcript:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    messages: List[Message] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return sessions_dir() / f"{self.session_id}.jsonl"

    def append(self, msg: Message) -> None:
        self.messages.append(msg)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(msg), ensure_ascii=False) + "\n")

    def add(self, role: str, content: str) -> None:
        self.append(Message(role=role, content=content))

    @classmethod
    def load(cls, session_id: str) -> "Transcript":
        t = cls(session_id=session_id)
        if t.path.exists():
            with t.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    t.messages.append(Message(**d))
        return t

    def render_for_upstream(self) -> str:
        """Render the transcript as a single text block to send upstream.

        Copilot is essentially stateless from our wrapper's perspective once
        we send a fresh prompt, so we replay the whole conversation each
        turn. The model sees this as ONE giant user message — there's no
        real assistant-role plumbing — so we explicitly label which lines
        were "your previous reply" vs. "harness output" and end with a
        directive about what the next reply must look like. Without this
        framing the model often re-emits the same <tool_use> instead of
        building on the result.
        """
        sections: list[str] = []
        # 1. System prompt (rules + tool list).
        sys_msgs = [m for m in self.messages if m.role == "system"]
        for m in sys_msgs:
            sections.append(m.content)

        # 2. Conversation so far, with explicit role narration.
        non_sys = [m for m in self.messages if m.role != "system"]
        if non_sys:
            sections.append("\n\n=== CONVERSATION SO FAR ===\n")
            for m in non_sys:
                if m.role == "user":
                    sections.append(f"\n[user said]\n{m.content}\n")
                elif m.role == "assistant":
                    sections.append(
                        f"\n[your previous reply — already sent, the harness "
                        f"saw this]\n{m.content}\n"
                    )
                elif m.role == "tool":
                    sections.append(
                        f"\n[harness output — this is the actual result of "
                        f"your previous action; do not re-issue it]\n{m.content}\n"
                    )

        # 3. End with a directive describing what the NEXT reply must be.
        # Different cue depending on whether the last non-system message
        # was a tool result (mid-task) or a user request (start of task).
        last_non_sys = non_sys[-1] if non_sys else None
        if last_non_sys is not None and last_non_sys.role == "tool":
            sections.append(
                "\n=== NOW REPLY ===\n"
                "Look at the harness output above. The action ran. Choose ONE:\n"
                "  - If you have enough information, reply with ONLY "
                "<final>your answer to the user's question</final>.\n"
                "  - If you need another action, reply with ONLY a new "
                "<tool_use>...</tool_use>.\n"
                "Do not re-emit the action you already ran. Do not narrate. "
                "Do not use Pages / Code Interpreter. Just the tag.\n"
            )
        elif last_non_sys is not None and last_non_sys.role == "user":
            sections.append(
                "\n=== NOW REPLY ===\n"
                "Either request a harness action with "
                "<tool_use>...</tool_use> (no preamble) or, if no action "
                "is needed, answer in plain prose and wrap your conclusion "
                "in <final>...</final>.\n"
            )

        return "".join(sections).strip()

    def estimated_tokens(self) -> int:
        return count_tokens(self.render_for_upstream())

    def compact(self, summary: str, keep_last_turns: int = 4) -> int:
        """Replace older messages with a summary; keep system prompt + last N
        user/assistant pairs verbatim. Returns the new estimated token count.

        Tool messages are dropped when they precede the kept window — they're
        usually large and rarely needed once the model has already acted on them.
        """
        if not self.messages:
            return 0
        system_msgs = [m for m in self.messages if m.role == "system"]
        non_system = [m for m in self.messages if m.role != "system"]

        # Find the index of the boundary that keeps `keep_last_turns` user/assistant
        # turns plus everything after.
        kept_tail: list[Message] = []
        ua_seen = 0
        for m in reversed(non_system):
            kept_tail.insert(0, m)
            if m.role in ("user", "assistant"):
                ua_seen += 1
                if ua_seen >= keep_last_turns * 2:
                    break

        summary_msg = Message(
            role="system",
            content=f"[Earlier conversation compacted to summary]\n{summary}",
        )
        self.messages = system_msgs + [summary_msg] + kept_tail
        # Rewrite the jsonl from scratch so resume reflects the compaction.
        with self.path.open("w", encoding="utf-8") as f:
            for m in self.messages:
                f.write(json.dumps(asdict(m), ensure_ascii=False) + "\n")
        return self.estimated_tokens()
