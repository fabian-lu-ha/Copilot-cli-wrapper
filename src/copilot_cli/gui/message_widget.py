"""Message bubbles: user / assistant / tool-call collapsible.

Streams assistant text by re-rendering markdown on each delta. For typical
message sizes (a few KB) this is fine; if it ever isn't we can throttle to
60 fps via a QTimer."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from copilot_cli.gui.markdown import render_markdown


class _AutoSizingBrowser(QTextBrowser):
    """A QTextBrowser that reports the height of its rendered document so
    enclosing layouts can size it without scrollbars."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("messageBody")
        self.setOpenExternalLinks(True)
        self.setReadOnly(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.document().contentsChanged.connect(self._adjust_height)

    def _adjust_height(self) -> None:
        # Make the document layout to the actual viewport width before measuring.
        self.document().setTextWidth(self.viewport().width())
        h = int(self.document().size().height()) + 4
        self.setFixedHeight(max(20, h))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._adjust_height()


class MessageWidget(QFrame):
    """Base bubble. Subclasses for user/assistant/tool just set object name and content."""

    def __init__(self, role: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.role = role
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        label = QLabel({"user": "You", "assistant": "Copilot", "tool": "Tool"}.get(self.role, self.role.title()))
        label.setObjectName("roleLabel")
        v.addWidget(label)

        body = _AutoSizingBrowser()
        body.setObjectName("messageBody")
        v.addWidget(body)
        self._body = body

    def set_markdown(self, text: str) -> None:
        # Markdown re-rendered on every update — preserves syntax highlighting
        # and table formatting during streaming.
        self._body.setHtml(render_markdown(text))

    def set_plaintext(self, text: str) -> None:
        self._body.setPlainText(text)


class UserMessage(MessageWidget):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__("user", parent)
        self.setObjectName("userBubble")
        self.set_plaintext(text)


class AssistantMessage(MessageWidget):
    """Streaming assistant message — call append_delta() during streaming,
    then finalize() when the turn ends to lock in markdown rendering."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("assistant", parent)
        self.setObjectName("assistantBubble")
        self._buffer = ""

    def set_full_text(self, text: str) -> None:
        self._buffer = text
        self.set_markdown(self._buffer)

    def append_delta(self, delta: str) -> None:
        self._buffer += delta
        self.set_markdown(self._buffer)

    def finalize(self) -> None:
        self.set_markdown(self._buffer)


class ToolCallWidget(QFrame):
    """Collapsible tool-call summary. Click the header to expand the output."""

    def __init__(self, name: str, args: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("toolBubble")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._name = name
        self._args = args
        self._output: str = "(running…)"
        self._expanded = False
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)
        toggle = QPushButton(self._summary_text("▶"))
        toggle.setObjectName("toolToggle")
        toggle.setCursor(Qt.PointingHandCursor)
        toggle.setFlat(True)
        toggle.clicked.connect(self._toggle)
        header.addWidget(toggle, 1)
        v.addLayout(header)
        self._toggle_btn = toggle

        out = QTextBrowser()
        out.setObjectName("toolOutput")
        out.setVisible(False)
        out.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        out.setMaximumHeight(360)
        out.setFont(QFont("Consolas"))
        v.addWidget(out)
        self._out = out

    def _summary_text(self, arrow: str) -> str:
        # Compact one-line summary like Claude Code shows.
        first_arg = ""
        for k, val in self._args.items():
            text = str(val).replace("\n", " ")
            if len(text) > 70:
                text = text[:70] + "…"
            first_arg = f"  {k}={text}"
            break
        return f"{arrow}  {self._name}{first_arg}"

    def set_output(self, text: str) -> None:
        self._output = text
        if self._expanded:
            self._out.setPlainText(text)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._toggle_btn.setText(self._summary_text("▼" if self._expanded else "▶"))
        self._out.setVisible(self._expanded)
        if self._expanded:
            self._out.setPlainText(self._output)
