"""Direct substrate WebSocket backend — bypasses the Playwright UI entirely.

The browser path (PlaywrightBackend) drives Copilot through its real web UI
which gives us authentic enforcement of all the tenant policy (DLP, web-
grounding toggle, model picker, etc.) but pays for it with UI bleed: Copilot's
frontend auto-opens Pages, runs Code Interpreter unbidden, and routes some
responses into a side pane we have to chase down.

This backend skips all of that. It opens the same SignalR-over-WebSocket
endpoint that Copilot's web UI uses (`wss://substrate.office.com/m365Copilot/
Chathub/{userId}@{tenantId}`), authenticates with the cookies from a
Playwright-managed Edge session, and sends/receives raw frames. There is no
HTML, no Lexical editor, no Pages auto-create, no Show**Thinking**Hide
chain-of-thought blocks — just the model's reply tokens.

Status: SKELETON. The capture and parsing reuse SubstrateCapture from the
existing browser backend so frame handling is shared. The auth + cookie
extraction is implemented but the actual outbound invocation payload is
spec'd in a comment — wiring it up is the next step.

Usage:
    from copilot_cli.backend.substrate_direct import SubstrateDirectBackend
    backend = SubstrateDirectBackend(settings)
    await backend.start()
    async for chunk in backend.send("hello"):
        print(chunk, end="")
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator, Optional
from urllib.parse import urlencode

from copilot_cli.backend.base import CopilotBackend
from copilot_cli.config import Settings

log = logging.getLogger(__name__)

# Constants captured from a real Copilot web session (probe_artifacts/
# websocket_frames.jsonl, 2026-04). These match what Office Web sends; the
# substrate backend rejects unknown values.
CHATHUB_HOST = "substrate.office.com"
CHATHUB_PATH = "/m365Copilot/Chathub"
SIGNALR_RS = "\x1e"
HANDSHAKE = json.dumps({"protocol": "json", "version": 1}) + SIGNALR_RS

# Captured live; safe defaults that produce a normal chat reply. Removing
# "SideBySide" / "RenderCardRequest" suppresses Pages auto-routing.
DEFAULT_ALLOWED_MESSAGE_TYPES = [
    "Chat", "Suggestion", "InternalSearchQuery", "Disengaged",
    "InternalLoaderMessage", "Progress", "EndOfRequest",
    "ReferencesListComplete",
]
DEFAULT_OPTIONS_SETS = [
    "search_result_progress_messages_with_search_queries",
    "enable_msa_user", "enable_batch_token_processing", "enable_gg_gpt",
]
DEFAULT_CLIENT_INFO = {
    "clientPlatform": "mcmcopilot-web",
    "clientAppName": "Office",
    "clientEntrypoint": "mcmcopilot-officeweb",
    "clientAppType": "Web",
    "deviceOS": "macOS",
    "deviceType": "Desktop",
}


class SubstrateDirectBackend(CopilotBackend):
    """WebSocket-only backend. Reuses Playwright once to mint a session;
    after that, all chat traffic goes through aiohttp directly to substrate.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cookies: Optional[dict] = None
        self._user_id: Optional[str] = None
        self._tenant_id: Optional[str] = None
        self._ws = None
        self._session = None  # aiohttp.ClientSession

    async def start(self) -> None:
        """One-time bootstrap: spin up Playwright just long enough to ensure
        the user is signed in and to harvest cookies + user/tenant GUIDs.
        Subsequent send() calls do not need the browser.
        """
        # Lazy imports so the GUI / browser-only consumers don't pay aiohttp's
        # import cost when this backend isn't selected.
        from playwright.async_api import async_playwright
        import aiohttp

        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.profile_dir),
                channel=self.settings.edge_channel or None,
                headless=self.settings.headless,
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://m365.cloud.microsoft/chat",
                            wait_until="domcontentloaded")
            # Wait for the signed-in chat to render so cookies + JWT are set.
            await page.locator(self.settings.selectors.input_box).first.wait_for(
                state="visible", timeout=120_000,
            )
            cookies_list = await ctx.cookies()
            # Capture the user + tenant GUIDs from the page — they're in
            # window-level globals once the SDK initializes.
            ids = await page.evaluate(
                """() => {
                    // Office's web SDK exposes these via auth context. Multiple
                    // possible globals; try them in order of stability.
                    const ctx = window.OneAuth || window.SuiteServiceProxy || window;
                    const claims = (ctx && ctx._userInfo) || (window._OneAuthIdToken && JSON.parse(atob(window._OneAuthIdToken.split('.')[1]))) || null;
                    return {
                        userId: claims?.oid || claims?.sub || null,
                        tenantId: claims?.tid || null,
                        // Fallback: read from any captured request URL.
                        href: location.href,
                    };
                }"""
            )
            await ctx.close()

        self._cookies = {c["name"]: c["value"] for c in cookies_list}
        self._user_id = ids.get("userId")
        self._tenant_id = ids.get("tenantId")
        if not self._user_id or not self._tenant_id:
            # TODO: parse the user/tenant GUIDs from the auth JWT cookie. The
            # token name varies by tenant ring (rt, rtFa, etc.); for now we
            # require Playwright to extract them.
            raise RuntimeError(
                "could not extract user/tenant GUIDs; falling back to "
                "PlaywrightBackend is recommended"
            )

        # Open the persistent aiohttp session that we'll use for the WS.
        self._session = aiohttp.ClientSession(cookies=self._cookies)

    async def stop(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def new_conversation(self) -> None:
        # No persistent server-side conversation here — every send() opens a
        # fresh ChatHub WS, so "new_conversation" is a no-op.
        return None

    def _build_ws_url(self) -> str:
        client_request_id = str(uuid.uuid4())
        params = urlencode({
            "ClientRequestId": client_request_id,
            "X-SessionId": client_request_id,
        })
        return (
            f"wss://{CHATHUB_HOST}{CHATHUB_PATH}/"
            f"{self._user_id}@{self._tenant_id}?{params}"
        )

    def _build_invocation(self, prompt: str) -> str:
        """Compose the SignalR `type=1` invocation that asks the model to reply.

        Captured live from Copilot's web client — the full schema is huge so
        we send a minimal-but-valid subset. Crucially we OMIT "SideBySide" /
        "RenderCardRequest" from allowedMessageTypes so the response cannot
        be routed into Pages.
        """
        invocation_id = str(uuid.uuid4())
        body = {
            "arguments": [{
                "source": "officeweb",
                "clientCorrelationId": invocation_id,
                "sessionId": invocation_id,
                "optionsSets": DEFAULT_OPTIONS_SETS,
                "allowedMessageTypes": DEFAULT_ALLOWED_MESSAGE_TYPES,
                "streamingMode": "ConciseWithPadding",
                "spokenTextMode": "None",
                "options": {},
                "extraExtensionParameters": {},
                "sliceIds": [],
                "threadLevelGptId": {},
                "traceId": invocation_id,
                "isStartOfSession": True,
                "clientInfo": DEFAULT_CLIENT_INFO,
                "message": {
                    "author": "user",
                    "inputMethod": "Keyboard",
                    "text": prompt,
                    "requestId": invocation_id,
                },
            }],
            "invocationId": "0",
            "target": "chat",
            "type": 1,
        }
        return json.dumps(body) + SIGNALR_RS

    async def send(self, prompt: str) -> AsyncIterator[str]:
        """Open a fresh ChatHub WS, send the prompt, stream the reply tokens.

        TODO: actually hook this up against the live endpoint. The capture
        we have proves the protocol; what's still missing is the auth header
        that some tenant rings require alongside the cookies. Once that's in,
        the parser side (extract_assistant_text + writeAtCursor) is unchanged.
        """
        if self._session is None:
            raise RuntimeError("call start() first")
        url = self._build_ws_url()
        invocation = self._build_invocation(prompt)
        log.info("opening direct ChatHub WS to %s", url)
        async with self._session.ws_connect(url) as ws:
            self._ws = ws
            # SignalR handshake.
            await ws.send_str(HANDSHAKE)
            await ws.receive()  # handshake ack: "{}\x1e"
            await ws.send_str(invocation)

            from copilot_cli.backend.substrate_capture import (
                parse_signalr_payload, extract_assistant_text,
                extract_write_at_cursor, is_end_record,
            )
            last_text = ""
            async for msg in ws:
                if msg.type.name not in ("TEXT", "BINARY"):
                    continue
                payload = msg.data if isinstance(msg.data, str) else msg.data.decode("utf-8", "replace")
                for record in parse_signalr_payload(payload):
                    if is_end_record(record):
                        return
                    if record.get("type") != 1:
                        continue
                    chunk = extract_write_at_cursor(record)
                    if chunk is not None:
                        yield chunk
                        last_text += chunk
                        continue
                    text = extract_assistant_text(record)
                    if text and text.startswith(last_text):
                        suffix = text[len(last_text):]
                        if suffix:
                            yield suffix
                            last_text = text
                    elif text:
                        yield text
                        last_text = text

    async def list_models(self) -> list[str]:
        # Direct backend doesn't expose the model picker — the model is set
        # via optionsSets entries on the invocation.
        return ["Auto"]

    async def set_model(self, name: str) -> bool:
        return name.lower() == "auto"
