"""Multi-line growing input + send button. Enter sends, Shift+Enter inserts newline."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

MAX_INPUT_HEIGHT = 200


class _GrowingEdit(QPlainTextEdit):
    submitted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inputEdit")
        self.setPlaceholderText("Message Copilot…  (Shift+Enter for newline)")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.document().contentsChanged.connect(self._adjust)
        self._adjust()

    def _adjust(self) -> None:
        doc = self.document()
        doc.setTextWidth(self.viewport().width())
        h = int(doc.size().height()) + 16
        h = min(MAX_INPUT_HEIGHT, max(40, h))
        self.setFixedHeight(h)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
            text = self.toPlainText().strip()
            if text:
                self.submitted.emit(text)
                self.clear()
            event.accept()
            return
        super().keyPressEvent(event)


class InputBox(QWidget):
    submitted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inputContainer")
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        frame = QFrame()
        frame.setObjectName("inputFrame")
        h = QHBoxLayout(frame)
        h.setContentsMargins(4, 4, 8, 4)
        h.setSpacing(6)

        self._edit = _GrowingEdit()
        self._edit.submitted.connect(self.submitted.emit)
        h.addWidget(self._edit, 1)

        self._send = QPushButton("↑")
        self._send.setObjectName("sendBtn")
        self._send.setCursor(Qt.PointingHandCursor)
        self._send.setFixedSize(32, 32)
        self._send.clicked.connect(self._on_send_clicked)
        # Vertically center the send button.
        btn_holder = QVBoxLayout()
        btn_holder.addStretch(1)
        btn_holder.addWidget(self._send)
        h.addLayout(btn_holder)

        v.addWidget(frame)

    def _on_send_clicked(self) -> None:
        text = self._edit.toPlainText().strip()
        if text:
            self.submitted.emit(text)
            self._edit.clear()

    def set_busy(self, busy: bool) -> None:
        self._send.setEnabled(not busy)
        self._edit.setReadOnly(busy)
