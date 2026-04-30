from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from platformdirs import user_config_path, user_data_path

APP_NAME = "copilot-cli"


def data_dir() -> Path:
    p = user_data_path(APP_NAME, appauthor=False, roaming=False)
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_dir() -> Path:
    p = user_config_path(APP_NAME, appauthor=False, roaming=False)
    p.mkdir(parents=True, exist_ok=True)
    return p


def edge_profile_dir() -> Path:
    p = data_dir() / "edge-profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sessions_dir() -> Path:
    p = data_dir() / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Selectors:
    """DOM selectors for the Copilot web UI. Editable so users can adapt
    when Microsoft changes the page without waiting for a release."""

    chat_url: str = "https://m365.cloud.microsoft/chat"
    # Lexical editor is a span with role="textbox" and data-lexical-editor;
    # `#m365-chat-editor-target-element` is the stable per-page id observed in
    # the M365 Copilot UI as of 2026-04. Fallbacks cover other tenant rings
    # and pre-Lexical builds.
    input_box: str = (
        '#m365-chat-editor-target-element, '
        '[data-lexical-editor="true"][role="textbox"], '
        '[contenteditable="true"][role="textbox"], '
        'textarea[aria-label*="Message" i]'
    )
    # Modern Copilot has no explicit send button — Enter submits. Backend
    # tries Enter first and only falls back to this selector if the input
    # still has the prompt. Keep it permissive in case a tenant ring re-adds
    # the button.
    send_button: str = (
        'button[aria-label*="Send" i]:not([aria-label*="feedback" i]), '
        'button[data-testid="send-button"]'
    )
    # Assistant bubbles use class `fai-CopilotMessage` and role="article"
    # with aria-labelledby="copilot-message-…". `__content` would target only
    # the rendered text, but for streaming we want the whole bubble so DOM
    # polling can read its inner_text.
    response_messages: str = (
        '.fai-CopilotMessage, '
        '[role="article"][aria-labelledby^="copilot-message-"], '
        '[data-content="ai-message"], '
        '[data-testid="bot-message"]'
    )
    new_chat_button: str = '[data-testid="newChatButton"], button[aria-label*="New chat" i]'

    @classmethod
    def load(cls) -> "Selectors":
        p = config_dir() / "selectors.yaml"
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        return cls()


@dataclass
class Settings:
    headless: bool = False
    edge_channel: str = "msedge"
    profile_dir: Path = field(default_factory=edge_profile_dir)
    # How long with NO substrate frame at all before we give up. Copilot
    # pauses for up to ~10s while it grounds against search/OneDrive — but
    # those pauses still emit metadata frames (search progress, throttling)
    # that we count as heartbeats. Real silence > 15s means the WS died.
    response_stable_seconds: float = 15.0
    response_timeout_seconds: float = 180.0
    # How long to wait for the FIRST WebSocket text frame before falling back
    # to DOM polling. Live capture (2026-04) shows the first reply token
    # typically lands 8–12s after send because Copilot does throttling +
    # SearchResults preamble first; under 10s causes false fallbacks.
    ws_first_delta_timeout: float = 15.0
    max_reflection_retries: int = 2
    auto_approve_reads: bool = True
    workdir: Path = field(default_factory=Path.cwd)
    selectors: Selectors = field(default_factory=Selectors.load)

    # Model selection. None = use whatever the UI default is.
    # The Playwright backend discovers available models from the page on
    # startup; the user picks one with --model or /model.
    model: str | None = None

    # Approximate context budget for the chosen M365 Copilot model. BizChat
    # currently exposes ~128K tokens for GPT-5 family in 2026, but practical
    # quality drops well before that; we warn at 80% and offer /compact.
    context_window_tokens: int = 128_000
    context_warn_ratio: float = 0.80
    context_auto_compact_ratio: float = 0.92

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        if os.environ.get("COPILOT_CLI_HEADLESS") == "1":
            s.headless = True
        if d := os.environ.get("COPILOT_CLI_PROFILE_DIR"):
            s.profile_dir = Path(d)
        if m := os.environ.get("COPILOT_CLI_MODEL"):
            s.model = m
        return s
