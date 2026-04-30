"""Modal dialog for tool-call approval. Mirrors the CLI's [once|session|prefix|deny]
choices but as buttons."""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from copilot_cli.tools.registry import Tool


@dataclass
class PermissionDecision:
    allowed: bool
    scope: str  # "once" | "session" | "prefix" | "deny"
    prefix: Optional[str] = None  # only set when scope == "prefix"


class PermissionDialog(QDialog):
    def __init__(self, tool: Tool, args: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("permDialog")
        self.setWindowTitle(f"Approve {tool.name}?")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._decision: Optional[PermissionDecision] = None
        self._tool = tool
        self._args = args
        self._build(tool, args)

    def _build(self, tool: Tool, args: dict) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        title = QLabel(f"{tool.name} wants to run")
        title.setObjectName("permTitle")
        layout.addWidget(title)

        desc = QLabel(tool.description)
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #b6bccb; font-size: 12px;")
        layout.addWidget(desc)

        for k in (tool.sensitive_args or tuple(args.keys())):
            v = args.get(k, "")
            text = str(v)
            if len(text) > 1500:
                text = text[:1500] + "\n... [truncated]"
            key = QLabel(k)
            key.setObjectName("permArgKey")
            val = QLabel(text)
            val.setObjectName("permArgVal")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            val.setFont(QFont("Consolas"))
            layout.addWidget(key)
            layout.addWidget(val)

        btns = QHBoxLayout()
        btns.setSpacing(8)

        deny = QPushButton("Deny")
        deny.setProperty("role", "danger")
        deny.clicked.connect(lambda: self._set("deny", False))
        btns.addWidget(deny)

        btns.addStretch(1)

        if tool.name == "run_bash":
            cmd = args.get("command", "")
            try:
                first = shlex.split(cmd)[0] if cmd else ""
            except ValueError:
                first = cmd.split()[0] if cmd.split() else ""
            if first:
                pref = QPushButton(f"Allow '{first}' for session")
                pref.setProperty("role", "ghost")
                pref.clicked.connect(lambda: self._set("prefix", True, prefix=first))
                btns.addWidget(pref)

        sess = QPushButton(f"Allow {tool.name} for session")
        sess.setProperty("role", "ghost")
        sess.clicked.connect(lambda: self._set("session", True))
        btns.addWidget(sess)

        once = QPushButton("Allow once")
        once.setProperty("role", "primary")
        once.setDefault(True)
        once.clicked.connect(lambda: self._set("once", True))
        btns.addWidget(once)

        layout.addSpacing(8)
        layout.addLayout(btns)

    def _set(self, scope: str, allowed: bool, prefix: Optional[str] = None) -> None:
        self._decision = PermissionDecision(allowed=allowed, scope=scope, prefix=prefix)
        self.accept() if allowed else self.reject()

    @property
    def decision(self) -> Optional[PermissionDecision]:
        return self._decision
