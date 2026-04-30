import asyncio

import pytest

from copilot_cli.tools.fs import _edit_file, _read_file, _write_file, parse_search_replace


def test_parse_search_replace_blocks():
    edits = (
        "<<<<<<< SEARCH\n"
        "old\n"
        "=======\n"
        "new\n"
        ">>>>>>> REPLACE\n"
    )
    blocks = parse_search_replace(edits)
    assert blocks == [("old", "new")]


def test_parse_search_replace_multiple():
    edits = (
        "<<<<<<< SEARCH\nA\n=======\nB\n>>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\nC\n=======\nD\n>>>>>>> REPLACE"
    )
    blocks = parse_search_replace(edits)
    assert blocks == [("A", "B"), ("C", "D")]


def test_read_write_and_edit_roundtrip(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")

    out = asyncio.run(_read_file(tmp_path, {"path": "x.py"}))
    assert "def foo" in out

    edits = (
        "<<<<<<< SEARCH\n"
        "    return 1\n"
        "=======\n"
        "    return 2\n"
        ">>>>>>> REPLACE"
    )
    res = asyncio.run(_edit_file(tmp_path, {"path": "x.py", "edits": edits}))
    assert "applied 1" in res
    assert f.read_text(encoding="utf-8") == "def foo():\n    return 2\n"


def test_edit_rejects_nonunique_match(tmp_path):
    f = tmp_path / "y.py"
    f.write_text("x = 1\nx = 1\n", encoding="utf-8")
    edits = "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE"
    res = asyncio.run(_edit_file(tmp_path, {"path": "y.py", "edits": edits}))
    assert "matches multiple locations" in res


def test_write_creates_parent_dirs(tmp_path):
    res = asyncio.run(_write_file(tmp_path, {"path": "sub/dir/new.txt", "content": "hi"}))
    assert "wrote" in res
    assert (tmp_path / "sub" / "dir" / "new.txt").read_text(encoding="utf-8") == "hi"


def test_path_jail(tmp_path):
    with pytest.raises(PermissionError):
        asyncio.run(_read_file(tmp_path, {"path": "../../../etc/passwd"}))
