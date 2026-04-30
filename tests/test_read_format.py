import asyncio

from copilot_cli.tools.fs import (
    DEFAULT_READ_LIMIT,
    MAX_LINE_CHARS,
    _read_file,
)


def test_read_returns_cat_n_format(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    out = asyncio.run(_read_file(tmp_path, {"path": "x.py"}))
    # Each line: 6-wide right-justified number + tab + content
    assert "     1\talpha" in out
    assert "     2\tbeta" in out
    assert "     3\tgamma" in out


def test_read_offset_is_one_indexed(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    out = asyncio.run(_read_file(tmp_path, {"path": "x.py", "offset": 3, "limit": 2}))
    assert "     3\tc" in out
    assert "     4\td" in out
    assert "     1\ta" not in out
    assert "     5\te" not in out


def test_read_offset_past_eof(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("a\nb\n", encoding="utf-8")
    out = asyncio.run(_read_file(tmp_path, {"path": "x.py", "offset": 99}))
    assert "past end of file" in out


def test_read_truncates_long_lines(tmp_path):
    f = tmp_path / "wide.txt"
    f.write_text("x" * (MAX_LINE_CHARS + 500) + "\n", encoding="utf-8")
    out = asyncio.run(_read_file(tmp_path, {"path": "wide.txt"}))
    assert "[truncated]" in out
    # Content portion shouldn't contain the full overrun.
    assert "x" * (MAX_LINE_CHARS + 100) not in out


def test_read_size_gate(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * (256 * 1024 + 1))
    out = asyncio.run(_read_file(tmp_path, {"path": "big.bin"}))
    assert "ERROR" in out and "256KB" in out.replace(" ", "") or ">" in out


def test_read_default_limit_truncation_message(tmp_path):
    f = tmp_path / "many.txt"
    # 2010 short lines so default limit (2000) hits.
    f.write_text("\n".join(f"line{i}" for i in range(2010)) + "\n", encoding="utf-8")
    out = asyncio.run(_read_file(tmp_path, {"path": "many.txt"}))
    assert f"showing lines 1-{DEFAULT_READ_LIMIT}" in out
    assert "of 2010" in out
