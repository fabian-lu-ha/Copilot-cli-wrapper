"""Token counting. Uses tiktoken with o200k_base (GPT-4o/5 family); falls back
to a chars/4 heuristic if tiktoken isn't installed.

M365 Copilot's BizChat layer doesn't expose a token count in its responses,
so the count is local-only and approximate. Good enough for budgeting."""
from __future__ import annotations

from functools import lru_cache

_HEURISTIC_CHARS_PER_TOKEN = 4


@lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken  # type: ignore
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    # Fallback heuristic.
    return max(1, len(text) // _HEURISTIC_CHARS_PER_TOKEN)
