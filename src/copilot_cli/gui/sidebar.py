"""Left sidebar: new chat, sessions list, model picker, context indicator."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from copilot_cli.config import sessions_dir


class Sidebar(QWidget):
    new_chat_requested = Signal()
    session_selected = Signal(str)  # session_id
    model_picker_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(260)
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QLabel("copilot-cli")
        header.setObjectName("sidebarHeader")
        v.addWidget(header)

        new_btn = QPushButton("+ New chat")
        new_btn.setObjectName("newChatBtn")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.clicked.connect(self.new_chat_requested.emit)
        h = QHBoxLayout()
        h.setContentsMargins(8, 4, 8, 8)
        h.addWidget(new_btn)
        v.addLayout(h)

        sub = QLabel("Sessions")
        sub.setObjectName("sidebarSubheader")
        v.addWidget(sub)

        self._list = QListWidget()
        self._list.setObjectName("sessionList")
        self._list.itemClicked.connect(self._on_item_clicked)
        v.addWidget(self._list, 1)

        # Footer: model picker + context label.
        self._model_btn = QPushButton("Model: (auto)")
        self._model_btn.setObjectName("modelPicker")
        self._model_btn.setCursor(Qt.PointingHandCursor)
        self._model_btn.clicked.connect(self.model_picker_requested.emit)

        self._ctx_label = QLabel("ctx: 0%")
        self._ctx_label.setObjectName("contextLabel")
        self._ctx_label.setAlignment(Qt.AlignCenter)

        footer = QVBoxLayout()
        footer.setContentsMargins(8, 8, 8, 12)
        footer.setSpacing(6)
        footer.addWidget(self._model_btn)
        footer.addWidget(self._ctx_label)
        v.addLayout(footer)

    def refresh_sessions(self, current_id: Optional[str] = None) -> None:
        self._list.clear()
        sdir: Path = sessions_dir()
        files = sorted(sdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[:200]:
            sid = f.stem
            item = QListWidgetItem(self._label_for(f))
            item.setData(Qt.UserRole, sid)
            self._list.addItem(item)
            if sid == current_id:
                self._list.setCurrentItem(item)

    @staticmethod
    def _label_for(path: Path) -> str:
        # Read the first 1KB for a quick title from the first user message.
        try:
            with path.open("r", encoding="utf-8") as f:
                head = f.read(2048)
        except OSError:
            return path.stem
        for line in head.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                import json
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("role") == "user":
                txt = obj.get("content", "").strip().replace("\n", " ")
                return (txt[:48] + "…") if len(txt) > 48 else (txt or path.stem)
        return path.stem

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        sid = item.data(Qt.UserRole)
        if sid:
            self.session_selected.emit(sid)

    def set_model(self, name: Optional[str]) -> None:
        self._model_btn.setText(f"Model: {name}" if name else "Model: (auto)")

    def set_context_usage(self, used: int, budget: int) -> None:
        ratio = (used / budget) if budget else 0
        color = "#8a92a4"
        if ratio > 0.8:
            color = "#e2756a"
        elif ratio > 0.6:
            color = "#e2c56a"
        self._ctx_label.setText(f"ctx: {used:,} / {budget:,}  ({ratio*100:.0f}%)")
        self._ctx_label.setStyleSheet(f"color: {color}; font-size: 11px;")
