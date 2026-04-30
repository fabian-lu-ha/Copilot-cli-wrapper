from rich.console import Console

from copilot_cli.tools.registry import Tool
from copilot_cli.ui.permissions import PermissionGate, PermissionState


def _make_tool(name="run_bash", requires=True):
    return Tool(
        name=name,
        description="x",
        args_schema="{}",
        handler=lambda a: None,  # type: ignore[arg-type]
        requires_approval=requires,
        sensitive_args=("command",),
    )


def test_readonly_tool_auto_approved():
    state = PermissionState()
    g = PermissionGate(state, Console(quiet=True))
    ok, _ = g.check(_make_tool("read_file", requires=False), {})
    assert ok


def test_autopilot_skips_prompts():
    state = PermissionState(autopilot=True)
    g = PermissionGate(state, Console(quiet=True))
    ok, reason = g.check(_make_tool(), {"command": "rm something"})
    assert ok and "autopilot" in reason


def test_session_allowlist():
    state = PermissionState(session_allowed_tools={"run_bash"})
    g = PermissionGate(state, Console(quiet=True))
    ok, _ = g.check(_make_tool(), {"command": "ls"})
    assert ok


def test_bash_prefix_allowlist_pre_approves_known_prefix():
    state = PermissionState(bash_allowed_prefixes={"git"})
    g = PermissionGate(state, Console(quiet=True))
    ok, reason = g.check(_make_tool(), {"command": "git status"})
    assert ok
    assert "git" in reason
