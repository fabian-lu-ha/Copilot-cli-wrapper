import asyncio
import time

from copilot_cli.tools.fs import _glob


def test_glob_returns_mtime_sorted(tmp_path):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    c = tmp_path / "c.py"
    a.write_text("")
    b.write_text("")
    c.write_text("")
    # Force order: a oldest, c newest.
    now = time.time()
    import os
    os.utime(a, (now - 100, now - 100))
    os.utime(b, (now - 50, now - 50))
    os.utime(c, (now, now))
    out = asyncio.run(_glob(tmp_path, {"pattern": "*.py"}))
    lines = out.splitlines()
    assert lines[0].endswith("c.py")
    assert lines[-1].endswith("a.py")


def test_glob_no_match(tmp_path):
    out = asyncio.run(_glob(tmp_path, {"pattern": "*.nope"}))
    assert out == "(no matches)"
