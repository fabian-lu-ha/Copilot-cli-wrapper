"""GUI entry point. Wires Qt's event loop to asyncio via qasync."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from importlib import resources
from pathlib import Path

import qasync
from PySide6.QtWidgets import QApplication

from copilot_cli.config import Settings
from copilot_cli.gui.main_window import MainWindow


def _load_stylesheet() -> str:
    # Read from package data (works when installed editable or wheel).
    try:
        with resources.files("copilot_cli.gui").joinpath("style.qss").open("r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="copilot-gui")
    p.add_argument("--workdir", help="Working directory for tool calls; defaults to cwd.")
    p.add_argument("--model", help="Pre-select an M365 Copilot model.")
    p.add_argument("--headless", action="store_true", help="Run Edge headless (sign-in must be cached).")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main() -> None:
    args = _argparser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = Settings.from_env()
    if args.workdir:
        settings.workdir = Path(args.workdir).resolve()
    if args.model:
        settings.model = args.model
    if args.headless:
        settings.headless = True

    app = QApplication(sys.argv)
    app.setApplicationName("Copilot CLI")
    app.setStyleSheet(_load_stylesheet())

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow(settings)
    window.show()

    async def boot():
        try:
            await window.start_async()
        except Exception:
            logging.exception("startup failed")

    asyncio.ensure_future(boot())

    with loop:
        try:
            loop.run_forever()
        finally:
            asyncio.ensure_future(window.stop_async())


if __name__ == "__main__":
    main()
