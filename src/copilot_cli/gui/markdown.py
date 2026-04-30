"""Markdown -> HTML with Pygments-highlighted code blocks.

Used by message bubbles. We use markdown-it-py with the GFM-like preset and
plug in a custom code-block renderer that runs Pygments. Output is plain HTML
that QTextBrowser/QTextEdit can render via setHtml().
"""
from __future__ import annotations

from functools import lru_cache

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound


@lru_cache(maxsize=1)
def _formatter() -> HtmlFormatter:
    # noclasses=True inlines styles so QTextBrowser doesn't need a CSS.
    return HtmlFormatter(noclasses=True, style="monokai", nowrap=False)


def _highlight(code: str, language: str) -> str:
    try:
        lexer = get_lexer_by_name(language) if language else guess_lexer(code)
    except ClassNotFound:
        try:
            lexer = guess_lexer(code)
        except ClassNotFound:
            return f'<pre style="background:#0f1115;padding:10px;border-radius:6px;color:#dfe6f0;">{_escape(code)}</pre>'
    return highlight(code, lexer, _formatter())


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


@lru_cache(maxsize=1)
def _md() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    md.enable(["table", "strikethrough"])

    # Override fenced-code renderer.
    def fence(tokens, idx, options, env):
        token = tokens[idx]
        lang = (token.info or "").strip().split()[0] if token.info else ""
        return _highlight(token.content, lang)

    md.renderer.rules["fence"] = fence

    # Inline code: subtle background pill.
    def code_inline(tokens, idx, options, env):
        return (
            f'<code style="background:#2a2d33;padding:2px 6px;'
            f'border-radius:4px;font-family:Consolas,Menlo,monospace;'
            f'font-size:0.95em;">{_escape(tokens[idx].content)}</code>'
        )

    md.renderer.rules["code_inline"] = code_inline
    return md


def render_markdown(text: str) -> str:
    """Render Markdown to HTML suitable for QTextBrowser.setHtml()."""
    return _md().render(text)
