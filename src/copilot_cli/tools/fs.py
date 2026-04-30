"""Filesystem tools: read_file, write_file, edit_file, list_dir, grep, glob.

Read tool mirrors Claude Code's behaviour:
  - default 2000 lines
  - 2000-char-per-line truncation
  - cat -n style output (spaces + 1-indexed number + tab + content)
  - 256 KB size gate
  - 25k token output cap
  - 1-indexed offset
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Tuple

from copilot_cli.tokenizer import count_tokens
from copilot_cli.tools.registry import Tool, ToolRegistry

# Claude Code Read tool defaults (see docs research):
DEFAULT_READ_LIMIT = 2000
MAX_LINE_CHARS = 2000
READ_SIZE_GATE_BYTES = 256 * 1024
READ_TOKEN_CAP = 25_000


def _resolve(workdir: Path, p: str) -> Path:
    candidate = (workdir / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
    try:
        candidate.relative_to(workdir.resolve())
    except ValueError:
        raise PermissionError(f"Path {p} is outside the working directory")
    return candidate


def _format_with_line_numbers(lines: list[str], start_lineno: int) -> str:
    """cat -n format: '%6d\\t%s'. start_lineno is 1-indexed."""
    out = []
    for i, line in enumerate(lines):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + " ... [truncated]"
        out.append(f"{start_lineno + i:6d}\t{line}")
    return "\n".join(out)


async def _read_file(workdir: Path, args: dict) -> str:
    path = _resolve(workdir, args["path"])
    if not path.exists():
        return f"ERROR: file not found: {path}"
    if path.is_dir():
        return f"ERROR: {path} is a directory; use list_dir."
    size = path.stat().st_size
    if size > READ_SIZE_GATE_BYTES:
        return (
            f"ERROR: file is {size} bytes (>{READ_SIZE_GATE_BYTES}). "
            "Read a slice with offset/limit or use grep to find the section first."
        )

    text = path.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()

    offset = max(1, int(args.get("offset", 1)))  # 1-indexed
    limit = int(args.get("limit", DEFAULT_READ_LIMIT))

    if offset > len(all_lines):
        return f"(offset {offset} is past end of file; file has {len(all_lines)} lines)"

    sliced = all_lines[offset - 1: offset - 1 + limit]
    rendered = _format_with_line_numbers(sliced, offset)

    # Token cap: trim from the bottom if over.
    if count_tokens(rendered) > READ_TOKEN_CAP:
        kept: list[str] = []
        running_tokens = 0
        for line in rendered.split("\n"):
            t = count_tokens(line + "\n")
            if running_tokens + t > READ_TOKEN_CAP:
                break
            kept.append(line)
            running_tokens += t
        rendered = "\n".join(kept) + "\n... [truncated to fit token budget; read more with offset]"

    end_line = offset + len(sliced) - 1
    if end_line < len(all_lines):
        rendered += f"\n\n[showing lines {offset}-{end_line} of {len(all_lines)}; pass offset to read more]"
    return rendered


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


async def _glob(workdir: Path, args: dict) -> str:
    """Glob files matching a pattern, sorted by modification time desc (Claude Code style)."""
    pattern = args["pattern"]
    base = _resolve(workdir, args.get("path", "."))
    if not base.is_dir():
        return f"ERROR: {base} is not a directory"
    matches = sorted(
        (p for p in base.glob(pattern) if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        return "(no matches)"
    return "\n".join(str(p) for p in matches[:500])


def register_fs_tools(registry: ToolRegistry, workdir: Path) -> None:
    registry.register(Tool(
        name="read_file",
        description=(
            "When to use: any time you need the contents of a file the user "
            "mentions, or before editing a file (always read first). Returns "
            "'cat -n' formatted output (line numbers + tab + content). Default "
            f"{DEFAULT_READ_LIMIT} lines; long lines truncated at {MAX_LINE_CHARS} "
            "chars; 256KB hard size gate; offset is 1-indexed for slicing big "
            "files.\n"
            'Example call:\n'
            '  <tool_use><name>read_file</name>'
            '<args>{"path":"src/main.py"}</args></tool_use>\n'
            "Example with slice (lines 100-200):\n"
            '  <tool_use><name>read_file</name>'
            '<args>{"path":"big.log","offset":100,"limit":100}</args></tool_use>'
        ),
        args_schema='{"path": str, "offset"?: int (1-indexed), "limit"?: int}',
        handler=lambda a: _read_file(workdir, a),
    ))
    registry.register(Tool(
        name="write_file",
        description=(
            "When to use: creating a NEW file, or completely replacing an "
            "existing file's contents. For small targeted changes to existing "
            "files, prefer edit_file. Creates parent dirs automatically.\n"
            "Example call:\n"
            '  <tool_use><name>write_file</name>'
            '<args>{"path":"hello.py","content":"print(\\"hi\\")\\n"}</args>'
            "</tool_use>"
        ),
        args_schema='{"path": str, "content": str}',
        handler=lambda a: _write_file(workdir, a),
        requires_approval=True,
        sensitive_args=("path",),
    ))
    registry.register(Tool(
        name="edit_file",
        description=(
            "When to use: small, targeted edits to an EXISTING file (e.g. "
            "fixing a typo, changing one line, renaming a variable). Always "
            "read the file first so your SEARCH text matches byte-for-byte.\n"
            "The 'edits' field is a string containing one or more "
            "Aider-style SEARCH/REPLACE blocks. Format is EXACT — seven "
            "less-than signs, the literal word SEARCH on its own line, the "
            "old text, seven equals signs on their own line, the new text, "
            "and seven greater-than signs followed by REPLACE. Whitespace "
            "in the SEARCH section must match the file exactly.\n"
            "Example call (fixing a typo):\n"
            '  <tool_use><name>edit_file</name>'
            '<args>{"path":"config.txt","edits":"<<<<<<< SEARCH\\n'
            'tiemout=30\\n=======\\ntimeout=30\\n'
            '>>>>>>> REPLACE"}</args></tool_use>\n'
            "If the SEARCH block doesn't match the file exactly, the edit "
            "fails and you'll get an ERROR back. In that case, re-read the "
            "file to see the actual text and retry."
        ),
        args_schema='{"path": str, "edits": str (one or more <<<<<<< SEARCH / ======= / >>>>>>> REPLACE blocks)}',
        handler=lambda a: _edit_file(workdir, a),
        requires_approval=True,
        sensitive_args=("path",),
    ))
    registry.register(Tool(
        name="list_dir",
        description=(
            "When to use: the user asks 'what files are in X' or you need to "
            "explore a project before reading specific files. Returns one "
            "entry per line, prefix 'f' (file) or 'd' (directory) followed "
            "by size and name. Path is optional — defaults to the working "
            "directory.\n"
            "Example call:\n"
            '  <tool_use><name>list_dir</name><args>{}</args></tool_use>\n'
            "Example for a subdir:\n"
            '  <tool_use><name>list_dir</name>'
            '<args>{"path":"src"}</args></tool_use>'
        ),
        args_schema='{"path"?: str}',
        handler=lambda a: _list_dir(workdir, a),
    ))
    registry.register(Tool(
        name="grep",
        description=(
            "When to use: searching for a regex pattern across files (e.g. "
            "'where is foo defined?', 'find all uses of bar'). Uses ripgrep "
            "if available, falls back to a Python walker. Returns "
            "file:line:matched-line for each hit.\n"
            "Example call:\n"
            '  <tool_use><name>grep</name>'
            '<args>{"pattern":"def add","path":"."}</args></tool_use>'
        ),
        args_schema='{"pattern": str, "path"?: str}',
        handler=lambda a: _grep(workdir, a),
    ))
    registry.register(Tool(
        name="glob",
        description=(
            "When to use: finding files by name pattern (e.g. all *.py files "
            "in src). Returns matching file paths sorted by modification "
            "time, newest first.\n"
            "Example call:\n"
            '  <tool_use><name>glob</name>'
            '<args>{"pattern":"**/*.py"}</args></tool_use>'
        ),
        args_schema='{"pattern": str (e.g. **/*.py), "path"?: str}',
        handler=lambda a: _glob(workdir, a),
    ))
