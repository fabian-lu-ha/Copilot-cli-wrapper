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
    input_box: str = 'div[contenteditable="true"][role="textbox"], textarea[aria-label*="Message"]'
    send_button: str = 'button[aria-label*="Send"], button[data-testid="send-button"]'
    response_messages: str = '[data-content="ai-message"], [data-testid="bot-message"]'
    new_chat_button: str = 'button[aria-label*="New chat"]'

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
    response_stable_seconds: float = 2.5
    response_timeout_seconds: float = 180.0
    max_reflection_retries: int = 2
    auto_approve_reads: bool = True
    workdir: Path = field(default_factory=Path.cwd)
    selectors: Selectors = field(default_factory=Selectors.load)

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        if os.environ.get("COPILOT_CLI_HEADLESS") == "1":
            s.headless = True
        if d := os.environ.get("COPILOT_CLI_PROFILE_DIR"):
            s.profile_dir = Path(d)
        return s
