"""Tests for the markdown renderer. No Qt needed."""
import pytest

pytest.importorskip("markdown_it")
pytest.importorskip("pygments")

from copilot_cli.gui.markdown import render_markdown


def test_inline_code_gets_pill():
    html = render_markdown("call `foo()` here")
    assert "background:#2a2d33" in html
    assert "foo()" in html


def test_fenced_python_codeblock_gets_pygments_highlighting():
    md = "```python\ndef foo(x):\n    return x + 1\n```"
    html = render_markdown(md)
    # Pygments inserts colored spans (noclasses=True inlines styles).
    assert "color:" in html
    assert "def" in html


def test_unknown_language_falls_back_to_plain_pre():
    md = "```\nplain text\n```"
    html = render_markdown(md)
    assert "plain text" in html


def test_table():
    md = "| a | b |\n|---|---|\n| 1 | 2 |"
    html = render_markdown(md)
    assert "<table>" in html
    assert "<td>1</td>" in html


def test_links_become_anchors():
    html = render_markdown("see [docs](https://example.com)")
    assert 'href="https://example.com"' in html


def test_no_html_injection():
    html = render_markdown("<script>alert(1)</script>")
    # html=False in markdown-it config; raw script must be escaped.
    assert "&lt;script&gt;" in html or "&lt;script" in html
    assert "<script>" not in html
