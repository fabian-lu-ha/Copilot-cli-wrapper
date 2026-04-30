from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console

from copilot_cli.agent.loop import AgentLoop
from copilot_cli.agent.transcript import Transcript
from copilot_cli.config import Settings, data_dir
from copilot_cli.tools.fs import register_fs_tools
from copilot_cli.tools.registry import ToolRegistry
from copilot_cli.tools.shell import register_shell_tools
from copilot_cli.ui.permissions import PermissionGate, PermissionState


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="copilot", description="Claude-Code-style CLI on top of M365 Copilot.")
    p.add_argument("--headless", action="store_true", help="Run Edge headless (sign-in must already be cached).")
    p.add_argument("--autopilot", action="store_true", help="Auto-approve every tool call. Risky.")
    p.add_argument("--resume", metavar="SESSION_ID", help="Resume a prior session by id.")
    p.add_argument("--workdir", help="Working directory for tool calls. Defaults to cwd.")
    p.add_argument("--model", help="Model to select in M365 Copilot (e.g. 'GPT-5.4 Thinking').")
    p.add_argument("--list-models", action="store_true", help="Print discovered models and exit.")
    p.add_argument("-p", "--prompt", help="One-shot prompt; exit when done.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


async def _run(args) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    console = Console()

    settings = Settings.from_env()
    if args.headless:
        settings.headless = True
    if args.workdir:
        from pathlib import Path
        settings.workdir = Path(args.workdir).resolve()
    if args.model:
        settings.model = args.model

    registry = ToolRegistry()
    register_fs_tools(registry, settings.workdir)
    register_shell_tools(registry, settings.workdir)

    perm_state = PermissionState(autopilot=args.autopilot)
    perms = PermissionGate(perm_state, console)

    transcript = Transcript.load(args.resume) if args.resume else Transcript()
    console.print(f"[dim]session id: {transcript.session_id}  ({transcript.path})")
    console.print(f"[dim]workdir:   {settings.workdir}")
    if perm_state.autopilot:
        console.print("[bold red]AUTOPILOT MODE: every tool call auto-approved[/]")

    from copilot_cli.backend.playwright_backend import PlaywrightBackend  # lazy: heavy import
    backend = PlaywrightBackend(settings)
    console.print("[dim]launching Edge … first run will require interactive sign-in")
    await backend.start()
    console.print("[green]signed in. ready.[/]")

    if args.list_models:
        models = await backend.list_models()
        if models:
            console.print("Discovered models:")
            for m in models:
                console.print(f"  - {m}")
        else:
            console.print("[yellow]could not auto-discover models from the page UI[/]")
        await backend.stop()
        return 0

    if settings.model:
        ok = await backend.set_model(settings.model)
        if ok:
            console.print(f"[dim]model set to: {settings.model}")
        else:
            console.print(f"[yellow]could not set model {settings.model!r}; using UI default[/]")

    loop = AgentLoop(backend, registry, settings, transcript, perms, console)

    try:
        if args.prompt:
            await loop.run_user_turn(args.prompt)
            return 0

        history_path = data_dir() / "input_history"
        session = PromptSession(history=FileHistory(str(history_path)))
        while True:
            try:
                line = await session.prompt_async("\nyou › ")
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line in {"/exit", "/quit"}:
                break
            if line == "/new":
                await backend.new_conversation()
                console.print("[dim]→ fresh upstream conversation")
                continue
            if line == "/yolo":
                perm_state.autopilot = not perm_state.autopilot
                console.print(f"[dim]autopilot = {perm_state.autopilot}")
                continue
            if line == "/models":
                models = await backend.list_models()
                if models:
                    for i, m in enumerate(models):
                        console.print(f"  {i}: {m}")
                else:
                    console.print("[yellow]could not discover models[/]")
                continue
            if line.startswith("/model "):
                target = line[len("/model "):].strip()
                ok = await backend.set_model(target)
                console.print(f"[dim]set_model({target!r}) -> {ok}")
                if ok:
                    settings.model = target
                continue
            if line == "/context":
                used = transcript.estimated_tokens()
                ratio = used / settings.context_window_tokens
                console.print(f"context: {used:,}/{settings.context_window_tokens:,} ({ratio*100:.1f}%)")
                continue
            if line == "/compact":
                await loop.compact()
                continue
            if line == "/help":
                console.print(
                    "/new       reset upstream chat\n"
                    "/yolo      toggle autopilot\n"
                    "/models    list discovered models\n"
                    "/model X   pick model X (substring match)\n"
                    "/context   show token usage\n"
                    "/compact   summarize older transcript\n"
                    "/exit      quit"
                )
                continue
            await loop.run_user_turn(line)
        return 0
    finally:
        await backend.stop()


def main() -> None:
    args = _build_argparser().parse_args()

    def _sigint(*_):
        sys.stderr.write("\ninterrupted.\n")
        sys.exit(130)

    signal.signal(signal.SIGINT, _sigint)
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
