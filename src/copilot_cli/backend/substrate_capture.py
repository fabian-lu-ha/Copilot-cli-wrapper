"""Captures and parses substrate WebSocket frames so we can yield assistant
text token-by-token instead of polling the DOM.

The protocol is SignalR-over-WebSocket: each frame is one or more JSON records
separated by 0x1e (ASCII record separator). Records carry a `type` field:
  1 = streaming chunk          (yields a `delta`)
  2 = final invocation result  (signals end-of-turn)
  3 = close / completion       (also signals end-of-turn)
  6 = ping                     (ignored)
  7 = close                    (ignored)

The actual chat payload shape isn't publicly documented and varies by tenant
ring, so `extract_assistant_text` walks several known-good paths
(`arguments[].messages[].text`, `arguments[].text`, `item.messages[].text`) and
returns whatever it finds. If nothing matches, the caller should fall back to
DOM polling.

Substrate sometimes sends FULL accumulated text per frame rather than true
deltas; the consumer in PlaywrightBackend tracks `last_text` and emits the
suffix to avoid printing duplicates.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, Optional

log = logging.getLogger(__name__)

RECORD_SEP = "\x1e"

SUBSTRATE_URL_NEEDLES = (
    "m365chat",
    "chathub",
    "substrate.office.com",
)


@dataclass
class FrameEvent:
    kind: str          # "full" | "chunk" | "end"
    text: Optional[str]


def is_substrate_url(url: str) -> bool:
    u = url.lower()
    return any(n in u for n in SUBSTRATE_URL_NEEDLES)


def extract_write_at_cursor(record: dict) -> Optional[str]:
    """Return the incremental chunk from a `writeAtCursor` substrate frame.

    M365 Copilot streams the assistant reply as: one initial type=1 frame with
    `messages[].text` carrying the first token (e.g. "This"), then a sequence
    of type=1 frames each with `arguments[].writeAtCursor` carrying the next
    chunk (" is", " a single short sentence to", ...). The final frame replays
    the assembled text via `messages[].text`. The caller appends each chunk
    onto its own running buffer.
    """
    args = record.get("arguments")
    if not isinstance(args, list):
        return None
    for arg in args:
        if not isinstance(arg, dict):
            continue
        chunk = arg.get("writeAtCursor")
        if isinstance(chunk, str) and chunk:
            return chunk
    return None


def parse_signalr_payload(payload: str | bytes) -> Iterable[dict]:
    """Split a raw frame payload into parsed JSON records. Tolerates the
    SignalR handshake frame ('{}'+RS) and partial/ill-formed records."""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8", errors="replace")
        except Exception:
            return
    for raw in payload.split(RECORD_SEP):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def extract_assistant_text(record: dict) -> Optional[str]:
    """Walk the record looking for assistant text. Returns the FULL accumulated
    text when found (substrate often resends the full message per frame), or
    None if this record doesn't carry assistant text.
    """
    args = record.get("arguments")
    if isinstance(args, list):
        for arg in args:
            if not isinstance(arg, dict):
                continue
            t = _from_messages(arg.get("messages"))
            if t is not None:
                return t
            t = arg.get("text")
            if isinstance(t, str) and t:
                return t
            item = arg.get("item")
            if isinstance(item, dict):
                t = _from_messages(item.get("messages"))
                if t is not None:
                    return t
    item = record.get("item")
    if isinstance(item, dict):
        t = _from_messages(item.get("messages"))
        if t is not None:
            return t
    return None


_META_CONTENT_TYPES = {"searchresults", "suggestion", "progress"}


def _from_messages(messages) -> Optional[str]:
    """Walk a messages[] list and return the actual assistant reply, skipping
    meta-annotations like search-results pre-ambles and reference lists.

    Substrate intermixes several kinds of bot-authored frames during a turn:
      - the real reply (no contentType, responseIdentifier="Default")
      - search-results banner (contentType="SearchResults") e.g. "OK, I'll search for X..."
      - references list (messageType startswith "ReferencesList")
      - progress / suggestion notices (contentType in _META_CONTENT_TYPES)
    All have author="bot" so an author-only filter would emit the wrong text
    as the streamed answer.
    """
    if not isinstance(messages, list):
        return None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        author = (msg.get("author") or msg.get("role") or "").lower()
        if author and author not in ("bot", "assistant", "copilot"):
            continue
        ct = (msg.get("contentType") or "").lower()
        if ct in _META_CONTENT_TYPES:
            continue
        mt = (msg.get("messageType") or "")
        if mt.startswith("ReferencesList") or mt.startswith("InternalSearch"):
            continue
        text = msg.get("text") or msg.get("content")
        if isinstance(text, str) and text:
            return text
    return None


def is_end_record(record: dict) -> bool:
    t = record.get("type")
    return t in (2, 3)


class SubstrateCapture:
    """Subscribes to all substrate WebSockets opened on the page and queues
    frame events for the current turn."""

    def __init__(self, page) -> None:  # page: playwright.async_api.Page
        self.page = page
        self._q: asyncio.Queue[FrameEvent] = asyncio.Queue()
        self._active = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        page.on("websocket", self._on_ws)

    def begin_turn(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._active = True
        # Drain stale frames from prior turns.
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                break

    def end_turn(self) -> None:
        self._active = False

    def _on_ws(self, ws) -> None:  # ws: playwright WebSocket
        if not is_substrate_url(ws.url):
            return
        log.debug("substrate ws connected: %s", ws.url)
        ws.on("framereceived", self._on_frame_received)

    def _on_frame_received(self, payload) -> None:
        if not self._active:
            return
        # Playwright Python passes the raw payload (bytes or str). Some
        # versions wrap it in a dict {"payload": ...}; handle both.
        if isinstance(payload, dict) and "payload" in payload:
            payload = payload["payload"]
        try:
            for record in parse_signalr_payload(payload):
                if is_end_record(record):
                    self._enqueue(FrameEvent("end", None))
                    continue
                if record.get("type") != 1:
                    continue
                # writeAtCursor (incremental delta) takes precedence over
                # messages[].text, because some frames contain BOTH a cursor
                # marker AND the bootstrap message — emitting both would
                # double-print the first token.
                chunk = extract_write_at_cursor(record)
                if chunk is not None:
                    self._enqueue(FrameEvent("chunk", chunk))
                    continue
                text = extract_assistant_text(record)
                if text:
                    self._enqueue(FrameEvent("full", text))
                    continue
                # No text in this type-1 frame — could be throttling info,
                # search-progress, references-list, or a "thinking" beat.
                # Enqueue a heartbeat so the consumer's idle timer resets;
                # real end-of-turn comes via type=2/3, not via silence.
                self._enqueue(FrameEvent("heartbeat", None))
        except Exception as e:
            log.debug("frame parse error: %s", e)

    def _enqueue(self, ev: FrameEvent) -> None:
        # Frame callbacks fire on the loop; put_nowait is safe.
        try:
            self._q.put_nowait(ev)
        except asyncio.QueueFull:
            pass

    async def stream_deltas(self, idle_timeout: float, hard_timeout: float) -> AsyncIterator[str]:
        """Yield assistant-text deltas. Stops on:
          - an "end" event (type 2 or 3 SignalR frame), OR
          - `idle_timeout` seconds with no new frame, OR
          - `hard_timeout` seconds total since first delta.

        Two frame shapes drive the stream:
          - "full" — a complete accumulated text snapshot (the bootstrap
            messages[].text frame and the final replay). Emit the new suffix
            relative to last_text.
          - "chunk" — an incremental writeAtCursor delta. Emit verbatim and
            append onto last_text.
        """
        last_text = ""
        first_delta_at: Optional[float] = None
        loop = asyncio.get_event_loop()
        while True:
            try:
                ev = await asyncio.wait_for(self._q.get(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                return
            if ev.kind == "end":
                return
            if ev.kind == "heartbeat":
                # No text but the WS is alive (throttling / search-progress /
                # references). Just reset the idle timer by looping.
                continue
            if ev.kind == "chunk" and ev.text is not None:
                if first_delta_at is None:
                    first_delta_at = loop.time()
                yield ev.text
                last_text += ev.text
                if (loop.time() - first_delta_at) > hard_timeout:
                    return
                continue
            if ev.kind == "full" and ev.text is not None:
                full = ev.text
                if full.startswith(last_text):
                    delta = full[len(last_text):]
                    if delta:
                        if first_delta_at is None:
                            first_delta_at = loop.time()
                        yield delta
                    last_text = full
                else:
                    # Replacement (rare): substrate replaced the buffer.
                    if first_delta_at is None:
                        first_delta_at = loop.time()
                    yield full
                    last_text = full
                if first_delta_at is not None and (loop.time() - first_delta_at) > hard_timeout:
                    return

    async def wait_for_first_delta(
        self, timeout: float, hard_timeout: Optional[float] = None
    ) -> Optional[FrameEvent]:
        """Wait for the first text-bearing event ("full" or "chunk") or end
        event. `timeout` is the max gap between ANY substrate frames — each
        heartbeat (throttling / search-progress / references) resets it,
        because they prove Copilot is actively processing. `hard_timeout`
        caps the total wait regardless. Heartbeats are drained silently;
        the returned text event is re-queued so stream_deltas() sees it.
        """
        loop = asyncio.get_event_loop()
        hard_deadline = loop.time() + (hard_timeout if hard_timeout is not None else timeout * 8)
        while True:
            now = loop.time()
            if now >= hard_deadline:
                return None
            silence_left = min(timeout, hard_deadline - now)
            try:
                ev = await asyncio.wait_for(self._q.get(), timeout=silence_left)
            except asyncio.TimeoutError:
                return None
            if ev.kind in ("full", "chunk", "end"):
                await self._q.put(ev)
                return ev
            # heartbeat: WS is alive, Copilot is still thinking. Reset the
            # silence timer by looping (next iteration re-derives it from
            # current time).
