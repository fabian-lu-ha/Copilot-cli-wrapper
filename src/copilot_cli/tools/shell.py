"""Shell tool. Uses PowerShell 7 on Windows (with cmd fallback), bash elsewhere."""
from __future__ import annotations

import asyncio
import platform
import shutil
from pathlib import Path

from copilot_cli.tools.registry import Tool, ToolRegistry

DENYLIST_SUBSTRINGS = (
    "rm -rf /", "rm -rf ~", "rmdir /s /q c:\\",
    "format c:", "format /q",
    "del /f /s /q c:\\",
    "shutdown", "reboot", ":(){ :|:& };:",
    "mkfs", "dd if=/dev/zero",
)


def _pick_shell() -> tuple[str, list[str]]:
    if platform.system() == "Windows":
        if shutil.which("pwsh"):
            return "pwsh", ["-NoProfile", "-NonInteractive", "-Command"]
        return "powershell", ["-NoProfile", "-NonInteractive", "-Command"]
    if shutil.which("bash"):
        return "bash", ["-lc"]
    return "/bin/sh", ["-c"]


async def _run_bash(workdir: Path, args: dict) -> str:
    cmd = args["command"]
    timeout = float(args.get("timeout_seconds", 60))
    lower = cmd.lower()
    for needle in DENYLIST_SUBSTRINGS:
        if needle in lower:
            return f"ERROR: command blocked by denylist (matched {needle!r})"

    shell, shell_args = _pick_shell()
    proc = await asyncio.create_subprocess_exec(
        shell, *shell_args, cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(workdir),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"ERROR: command timed out after {timeout}s"
    text = stdout.decode("utf-8", errors="replace")
    if len(text) > 50_000:
        text = text[:50_000] + "\n... (output truncated)"
    return f"exit={proc.returncode}\n{text}"


def register_shell_tools(registry: ToolRegistry, workdir: Path) -> None:
    registry.register(Tool(
        name="run_bash",
        description=(
            "Run a shell command in the working directory. PowerShell on Windows, "
            "bash on Linux/macOS. Output combines stdout and stderr."
        ),
        args_schema='{"command": str, "timeout_seconds"?: int}',
        handler=lambda a: _run_bash(workdir, a),
        requires_approval=True,
        sensitive_args=("command",),
    ))
