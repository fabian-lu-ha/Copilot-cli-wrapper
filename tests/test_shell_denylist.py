import asyncio
from pathlib import Path

from copilot_cli.tools.shell import _run_bash


def test_denylist_blocks_obvious_destructive():
    out = asyncio.run(_run_bash(Path.cwd(), {"command": "rm -rf /"}))
    assert "blocked" in out


def test_normal_command_runs(tmp_path):
    out = asyncio.run(_run_bash(tmp_path, {"command": "echo hello", "timeout_seconds": 5}))
    assert "hello" in out
    assert "exit=0" in out
