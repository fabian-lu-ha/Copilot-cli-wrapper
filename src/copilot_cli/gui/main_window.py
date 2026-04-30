"""Main window: sidebar | (topbar + chat view + input). Wires everything together."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from copilot_cli.agent.transcript import Transcript
from copilot_cli.config import Settings
from copilot_cli.gui.agent_runner import GuiAgentRunner
from copilot_cli.gui.chat_view import ChatView
from copilot_cli.gui.input_box import InputBox
from copilot_cli.gui.message_widget import (
    AssistantMessage,
    ToolCallWidget,
    UserMessage,
)
from copilot_cli.gui.permission_dialog import PermissionDialog
from copilot_cli.gui.sidebar import Sidebar
from copilot_cli.tools.fs import register_fs_tools
from copilot_cli.tools.registry import ToolRegistry
from copilot_cli.tools.shell import register_shell_tools

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.setWindowTitle("Copilot CLI")
        self.resize(1200, 800)
        self.settings = settings

        self.registry = ToolRegistry()
        register_fs_tools(self.registry, settings.workdir)
        register_shell_tools(self.registry, settings.workdir)

        self.transcript = Transcript()
        self.backend = None  # set in start_async()
        self.runner: Optional[GuiAgentRunner] = None
        self._current_assistant: Optional[AssistantMessage] = None
        self._current_tool_widget: Optional[ToolCallWidget] = None

        self._build()

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        self.sidebar = Sidebar()
        self.sidebar.new_chat_requested.connect(self._on_new_chat)
        self.sidebar.session_selected.connect(self._on_session_selected)
        self.sidebar.model_picker_requested.connect(self._on_model_picker)
        h.addWidget(self.sidebar)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        topbar = QFrame()
        topbar.setObjectName("topbar")
        tb = QHBoxLayout(topbar)
        tb.setContentsMargins(16, 6, 16, 6)
        tb.addStretch(1)
        self.autopilot_btn = QPushButton("Autopilot: off")
        self.autopilot_btn.setProperty("role", "ghost")
        self.autopilot_btn.setStyleSheet("padding: 6px 12px; border-radius: 8px;")
        self.autopilot_btn.setCheckable(True)
        self.autopilot_btn.toggled.connect(self._on_autopilot_toggled)
        tb.addWidget(self.autopilot_btn)
        right.addWidget(topbar)

        self.chat = ChatView()
        right.addWidget(self.chat, 1)

        self.input = InputBox()
        self.input.submitted.connect(self._on_submit)
        right.addWidget(self.input)

        right_w = QWidget()
        right_w.setLayout(right)
        h.addWidget(right_w, 1)

        self.setCentralWidget(root)
        self.sidebar.refresh_sessions(self.transcript.session_id)

    # ---------- async startup ----------

    async def start_async(self) -> None:
        from copilot_cli.backend.playwright_backend import PlaywrightBackend
        self._show_status("launching Edge — sign in if prompted")
        self.backend = PlaywrightBackend(self.settings)
        await self.backend.start()
        self.runner = GuiAgentRunner(self.backend, self.registry, self.settings, self.transcript)
        self._wire_runner_signals()
        if self.settings.model:
            ok = await self.backend.set_model(self.settings.model)
            if ok:
                self.sidebar.set_model(self.settings.model)
        self._show_status("ready")

    async def stop_async(self) -> None:
        if self.backend:
            await self.backend.stop()

    def _wire_runner_signals(self) -> None:
        r = self.runner
        assert r is not None
        r.user_message.connect(self._render_user)
        r.assistant_started.connect(self._render_assistant_started)
        r.assistant_delta.connect(self._render_assistant_delta)
        r.assistant_finished.connect(self._render_assistant_finished)
        r.tool_call_started.connect(self._render_tool_started)
        r.tool_call_finished.connect(self._render_tool_finished)
        r.final_answer.connect(self._render_final)
        r.info.connect(self._show_status)
        r.error_occurred.connect(self._show_error)
        r.context_updated.connect(self.sidebar.set_context_usage)
        r.busy_changed.connect(self.input.set_busy)
        r.permission_requested.connect(self._handle_permission)

    # ---------- handlers ----------

    def _on_submit(self, text: str) -> None:
        if not self.runner:
            return
        asyncio.ensure_future(self.runner.run_user_turn(text))

    def _on_new_chat(self) -> None:
        self.transcript = Transcript()
        if self.runner:
            self.runner.transcript = self.transcript
            from copilot_cli.agent.system_prompt import build_system_prompt
            self.transcript.add("system", build_system_prompt(self.settings.workdir, self.registry))
        self.chat.clear()
        self.sidebar.refresh_sessions(self.transcript.session_id)

    def _on_session_selected(self, session_id: str) -> None:
        loaded = Transcript.load(session_id)
        if not loaded.messages:
            return
        self.transcript = loaded
        if self.runner:
            self.runner.transcript = loaded
        self.chat.clear()
        for m in loaded.messages:
            if m.role == "user":
                self.chat.add_widget(UserMessage(m.content))
            elif m.role == "assistant":
                w = AssistantMessage()
                w.set_full_text(m.content)
                self.chat.add_widget(w)
            # system / tool replays are skipped in the visual scroll; they're
            # still part of the transcript that gets sent upstream.
        self.sidebar.refresh_sessions(session_id)

    def _on_model_picker(self) -> None:
        # Split async (Playwright list_models) from sync (Qt modal dialog).
        # Calling QInputDialog.getItem inside an async coro deadlocks
        # qasync: the modal spins Qt's event loop, which tries to drive
        # another asyncio task (Playwright's connection.run), and asyncio
        # rejects nested task entry with
        #   RuntimeError: Cannot enter into task ... while another task
        #   ... is being executed.
        # Resolve the futures with add_done_callback and run all Qt UI
        # interactions in plain sync callbacks instead.
        if self.backend is None:
            return

        def show_picker(fut):
            try:
                models = fut.result()
            except Exception as e:
                QMessageBox.warning(self, "Models", f"Could not list models: {e}")
                return
            if not models:
                QMessageBox.information(
                    self, "Models",
                    "Could not auto-discover models from the page.",
                )
                return
            choice, accepted = QInputDialog.getItem(
                self, "Pick model", "Model:", models, 0, False,
            )
            if not (accepted and choice):
                return
            apply_fut = asyncio.ensure_future(self.backend.set_model(choice))

            def on_applied(f):
                try:
                    applied = f.result()
                except Exception as e:
                    QMessageBox.warning(self, "Models", f"set_model failed: {e}")
                    return
                if applied:
                    self.settings.model = choice
                    self.sidebar.set_model(choice)
                else:
                    QMessageBox.warning(
                        self, "Models", f"Could not switch to {choice!r}.",
                    )

            apply_fut.add_done_callback(on_applied)

        list_fut = asyncio.ensure_future(self.backend.list_models())
        list_fut.add_done_callback(show_picker)

    def _on_autopilot_toggled(self, checked: bool) -> None:
        if self.runner:
            self.runner.set_autopilot(checked)
        self.autopilot_btn.setText(f"Autopilot: {'ON' if checked else 'off'}")

    def _handle_permission(self, tool, args, future) -> None:
        dlg = PermissionDialog(tool, args, self)
        dlg.exec()
        future.set_result(dlg.decision)

    # ---------- render helpers ----------

    def _render_user(self, text: str) -> None:
        self.chat.add_widget(UserMessage(text))

    def _render_assistant_started(self) -> None:
        self._current_assistant = AssistantMessage()
        self.chat.add_widget(self._current_assistant)

    def _render_assistant_delta(self, visible_text: str) -> None:
        if self._current_assistant is not None:
            self._current_assistant.set_full_text(visible_text)

    def _render_assistant_finished(self, visible_text: str) -> None:
        if self._current_assistant is not None:
            self._current_assistant.set_full_text(visible_text)
            self._current_assistant.finalize()
            self._current_assistant = None

    def _render_tool_started(self, name: str, args: dict) -> None:
        widget = ToolCallWidget(name, args)
        self._current_tool_widget = widget
        self.chat.add_widget(widget)

    def _render_tool_finished(self, name: str, output: str) -> None:
        if self._current_tool_widget is not None:
            self._current_tool_widget.set_output(output)
            self._current_tool_widget = None

    def _render_final(self, text: str) -> None:
        self._show_status("done")

    def _show_status(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 5000)

    def _show_error(self, msg: str) -> None:
        self._show_status(f"error: {msg}")
