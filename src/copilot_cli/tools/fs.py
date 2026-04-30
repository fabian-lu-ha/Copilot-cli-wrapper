"""Filesystem tools: read_file, write_file, edit_file, list_dir, grep."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Tuple

from copilot_cli.tools.registry import Tool, ToolRegistry

MAX_READ_BYTES = 200_000


def _resolve(workdir: Path, p: str) -> Path:
    candidate = (workdir / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
    # Jail to workdir.
    try:
        candidate.relative_to(workdir.resolve())
    except ValueError:
        raise PermissionError(f"Path {p} is outside the working directory")
    return candidate


async def _read_file(workdir: Path, args: dict) -> str:
    path = _resolve(workdir, args["path"])
    if not path.exists():
        return f"ERROR: file not found: {path}"
    data = path.read_bytes()
    if len(data) > MAX_READ_BYTES:
        return (
            f"ERROR: file is {len(data)} bytes (>{MAX_READ_BYTES}). "
            "Read a slice with offset/limit instead, or use grep."
        )
    text = data.decode("utf-8", errors="replace")
    if "offset" in args or "limit" in args:
        lines = text.splitlines()
        offset = int(args.get("offset", 0))
        limit = int(args.get("limit", len(lines)))
        text = "\n".join(lines[offset: offset + limit])
    return text


async def _write_file(workdir: Path, args: dict) -> str:
    path = _resolve(workdir, args["path"])
    content = args["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


_BLOCK_RE = re.compile(
    r"<{7}\s*SEARCH\s*\n(.*?)\n={7}\s*\n(.*?)\n>{7}\s*REPLACE",
    re.DOTALL,
)


def parse_search_replace(edits: str) -> list[Tuple[str, str]]:
    blocks = []
    for m in _BLOCK_RE.finditer(edits):
        blocks.append((m.group(1), m.group(2)))
    if not blocks:
        raise ValueError("No SEARCH/REPLACE blocks found in edits")
    return blocks


async def _edit_file(workdir: Path, args: dict) -> str:
    path = _resolve(workdir, args["path"])
    if not path.exists():
        return f"ERROR: file not found: {path}"
    text = path.read_text(encoding="utf-8")
    blocks = parse_search_replace(args["edits"])
    applied = 0
    for search, replace in blocks:
        if search not in text:
            return (
                f"ERROR: SEARCH block not found in {path}. The text must match "
                f"byte-for-byte. First 200 chars of search:\n{search[:200]}"
            )
        if text.count(search) > 1:
            return (
                f"ERROR: SEARCH block matches multiple locations in {path}. "
                "Add more context to make it unique."
            )
        text = text.replace(search, replace, 1)
        applied += 1
    path.write_text(text, encoding="utf-8")
    return f"applied {applied} edit(s) to {path}"


async def _list_dir(workdir: Path, args: dict) -> str:
    p = _resolve(workdir, args.get("path", "."))
    if not p.is_dir():
        return f"ERROR: not a directory: {p}"
    entries = []
    for child in sorted(p.iterdir()):
        kind = "d" if child.is_dir() else "f"
        try:
            size = child.stat().st_size if child.is_file() else "-"
        except OSError:
            size = "?"
        entries.append(f"{kind} {size:>10} {child.name}")
    return "\n".join(entries) if entries else "(empty)"


async def _grep(workdir: Path, args: dict) -> str:
    pattern = args["pattern"]
    path = args.get("path", ".")
    target = _resolve(workdir, path)
    # Prefer ripgrep if available, else Python re-walk.
    try:
        rg = subprocess.run(
            ["rg", "--no-heading", "-n", "--color=never", pattern, str(target)],
            capture_output=True, text=True, timeout=20,
        )
        out = rg.stdout
        if rg.returncode == 0 and out:
            return out[:50_000]
        if rg.returncode == 1:
            return "(no matches)"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Fallback: Python search.
    matches = []
    rx = re.compile(pattern)
    files = [target] if target.is_file() else target.rglob("*")
    for f in files:
        if not f.is_file():
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    matches.append(f"{f}:{i}:{line}")
                    if len(matches) >= 500:
                        break
        except OSError:
            continue
        if len(matches) >= 500:
            break
    return "\n".join(matches) if matches else "(no matches)"


def register_fs_tools(registry: ToolRegistry, workdir: Path) -> None:
    registry.register(Tool(
        name="read_file",
        description="Read a UTF-8 text file. Optional offset/limit to read a line range.",
        args_schema='{"path": str, "offset"?: int, "limit"?: int}',
        handler=lambda a: _read_file(workdir, a),
    ))
    registry.register(Tool(
        name="write_file",
        description="Overwrite a file with new content. Creates parent dirs.",
        args_schema='{"path": str, "content": str}',
        handler=lambda a: _write_file(workdir, a),
        requires_approval=True,
        sensitive_args=("path",),
    ))
    registry.register(Tool(
        name="edit_file",
        description="Apply Aider-style SEARCH/REPLACE edits to an existing file.",
        args_schema='{"path": str, "edits": str (SEARCH/REPLACE blocks)}',
        handler=lambda a: _edit_file(workdir, a),
        requires_approval=True,
        sensitive_args=("path",),
    ))
    registry.register(Tool(
        name="list_dir",
        description="List files in a directory.",
        args_schema='{"path"?: str}',
        handler=lambda a: _list_dir(workdir, a),
    ))
    registry.register(Tool(
        name="grep",
        description="Search files for a regex pattern (uses ripgrep if available).",
        args_schema='{"pattern": str, "path"?: str}',
        handler=lambda a: _grep(workdir, a),
    ))
