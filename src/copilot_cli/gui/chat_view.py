"""Scrolling chat view that holds message widgets and auto-scrolls on append."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget


class ChatView(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatScroll")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        container.setObjectName("chatContainer")
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(48, 24, 48, 24)
        self._layout.setSpacing(20)
        self._layout.addStretch(1)

        self.setWidget(container)
        self._stick_to_bottom = True
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def add_widget(self, w: QWidget) -> None:
        # Insert before the trailing stretch.
        idx = self._layout.count() - 1
        self._layout.insertWidget(idx, w)
        if self._stick_to_bottom:
            QTimer.singleShot(0, self._scroll_to_bottom)

    def clear(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _scroll_to_bottom(self) -> None:
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_scroll(self, _value: int) -> None:
        sb = self.verticalScrollBar()
        # Consider us "stuck" if within 80px of the bottom.
        self._stick_to_bottom = (sb.maximum() - sb.value()) < 80
