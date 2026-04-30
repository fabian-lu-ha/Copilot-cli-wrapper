from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncIterator, Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)

from copilot_cli.backend.base import CopilotBackend
from copilot_cli.backend.substrate_capture import SubstrateCapture
from copilot_cli.config import Settings

log = logging.getLogger(__name__)

SUBSTRATE_HOSTS = ("substrate.office.com", "cloud.microsoft", "office.com")

# Heuristic regex for the model picker button's accessible name. M365 Copilot
# doesn't publish stable testid/aria-label values for it; the button label is
# usually the active model name. Per the April 2026 Copilot update, common
# names include "GPT-5.x Quick", "GPT-5.x Thinking", "Auto", "Smart",
# "Researcher", "Claude Sonnet 4.5" (when tenant enables Anthropic).
MODEL_BUTTON_NAME_RE = re.compile(r"(GPT-|Think|Quick|Smart|Auto|Claude|Researcher|Reasoning)", re.I)


# Injected before page scripts run. Patches WebSocket.send so that any
# substrate ChatHub invocation has its `allowedMessageTypes` filtered to
# drop the entries that cause Copilot to auto-route the response into a
# Pages / Canvas / side pane. The chat reply then has nowhere to go but
# the actual chat bubble, which is what we can read.
#
# The substrate request payload looks like:
#   { "arguments": [{
#       "allowedMessageTypes": ["Chat","SideBySide","RenderCardRequest", ...],
#       "optionsSets": ["cwc_flux_image", ...],
#       ...
#   }], "target": "...", "invocationId": "..." }
# SignalR may pack multiple JSON records into one frame separated by 0x1E.
_SIDEBYSIDE_BLOCKER_JS = r"""
(() => {
  if (window.__copilotCliPagesBlocker) return;
  window.__copilotCliPagesBlocker = true;
  const RS = String.fromCharCode(0x1e);
  // Message types whose presence enables Copilot to redirect the reply
  // into a side pane / Pages document. Drop them all.
  const BLOCKED_TYPES = new Set([
    "SideBySide",
    "RenderCardRequest",
    "GenerateGraphicArt",
  ]);
  // Same idea for optionsSets entries that gate the auto-Pages behavior.
  // Conservative — only drop entries with explicit "page"/"canvas"/"side"
  // tokens. This list will probably need to grow as Microsoft adds flags.
  const BLOCKED_OPTION_RE = /(side[_-]?by[_-]?side|sidepane|canvas|pages?)/i;

  const stripRecord = (raw) => {
    let s = raw;
    if (!s || s.length === 0) return s;
    let parsed;
    try { parsed = JSON.parse(s); } catch (e) { return s; }
    if (!parsed || !Array.isArray(parsed.arguments)) return s;
    let mutated = false;
    parsed.arguments.forEach(a => {
      if (a && Array.isArray(a.allowedMessageTypes)) {
        const filtered = a.allowedMessageTypes.filter(t => !BLOCKED_TYPES.has(t));
        if (filtered.length !== a.allowedMessageTypes.length) {
          a.allowedMessageTypes = filtered;
          mutated = true;
        }
      }
      if (a && Array.isArray(a.optionsSets)) {
        const filtered = a.optionsSets.filter(o => !BLOCKED_OPTION_RE.test(o));
        if (filtered.length !== a.optionsSets.length) {
          a.optionsSets = filtered;
          mutated = true;
        }
      }
    });
    return mutated ? JSON.stringify(parsed) : s;
  };

  const transform = (data) => {
    if (typeof data !== "string") return data;
    if (data.indexOf("allowedMessageTypes") < 0 && data.indexOf("optionsSets") < 0) {
      return data;
    }
    // SignalR JSON protocol: each record terminated by 0x1E.
    if (data.includes(RS)) {
      const out = data.split(RS).map(stripRecord).join(RS);
      return out;
    }
    return stripRecord(data);
  };

  const origSend = WebSocket.prototype.send;
  let hookHits = 0;
  let mutated = 0;
  WebSocket.prototype.send = function (data) {
    try {
      hookHits++;
      const before = data;
      data = transform(data);
      if (typeof before === "string" && typeof data === "string" && before !== data) {
        mutated++;
        console.log("[copilot-cli] mutated WS frame #" + mutated + " (" + before.length + "B)");
      }
      if (hookHits === 1 || hookHits % 20 === 0) {
        console.log("[copilot-cli] WS.send hook hits=" + hookHits + " mutated=" + mutated);
      }
    } catch (e) {
      console.warn("[copilot-cli] pages blocker error:", e);
    }
    return origSend.call(this, data);
  };
  console.log("[copilot-cli] pages blocker installed");
})();
"""


class PlaywrightBackend(CopilotBackend):
    """Drives the M365 Copilot web UI via a persistent Edge profile.

    First run: a real Edge window opens, user signs in interactively. The
    profile dir keeps the session for subsequent runs.

    Streaming strategy:
      1. Hook page.on("response") for substrate fetch responses to detect
         when the network turn ends (primary completion signal).
      2. Poll the latest assistant DOM node for text and yield deltas.
      3. Stop when (network turn ended) OR (text stable for N seconds).
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw: Optional[Playwright] = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._network_idle_event = asyncio.Event()
        self._capture: Optional[SubstrateCapture] = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.profile_dir),
            channel=self.settings.edge_channel,
            headless=self.settings.headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        # Inject a WebSocket.send hook that strips the SideBySide message
        # type from outbound substrate invocations. SideBySide is what tells
        # Copilot's backend "you may route this response into the Pages
        # side pane" — without it, the frontend cannot trigger auto-Pages
        # and the reply has to land in the chat. There is no user-facing
        # toggle for this; intercepting at the WS layer is the only knob.
        await self._ctx.add_init_script(_SIDEBYSIDE_BLOCKER_JS)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._page.on("response", self._on_response)
        # Pipe the injected blocker's console output up to our log so we can
        # tell if it's actually intercepting WS frames.
        def _on_console(msg):
            text = msg.text
            if "[copilot-cli]" in text:
                log.info("page console: %s", text)
        self._page.on("console", _on_console)
        # Substrate capture must be wired BEFORE we navigate so we don't miss
        # the initial WebSocket connection.
        self._capture = SubstrateCapture(self._page)
        await self._page.goto(self.settings.selectors.chat_url, wait_until="domcontentloaded")
        await self._wait_for_signin()

    async def stop(self) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()

    async def new_conversation(self) -> None:
        assert self._page
        sel = self.settings.selectors.new_chat_button
        try:
            await self._page.click(sel, timeout=3000)
        except Exception:
            await self._page.goto(self.settings.selectors.chat_url, wait_until="domcontentloaded")

    async def _close_side_panes(self) -> bool:
        """Copilot's UI auto-opens a Pages / Canvas / Loop side pane on
        anything that looks document-shaped (long prompts, XML markup) and
        writes the response there instead of in the chat — the chat bubble
        then sticks on "Lining things up..." forever. Selectors confirmed
        live (M365 Copilot, 2026-04):

          - container: [data-testid="pages-sidepane"]
          - close button inside it: [data-testid="discardButton"]
            (also has aria-label="Close")

        Returns True if a pane was found and dismissed.
        """
        if self._page is None:
            return False
        closed_any = False
        try:
            pane = self._page.locator('[data-testid="pages-sidepane"]')
            if await pane.count() > 0:
                close_btn = self._page.locator(
                    '[data-testid="pages-sidepane"] [data-testid="discardButton"], '
                    '[data-testid="pages-sidepane"] button[aria-label="Close" i]'
                )
                if await close_btn.count() > 0:
                    log.debug("closing pages-sidepane via discardButton")
                    try:
                        await close_btn.first.click(timeout=1500)
                        closed_any = True
                    except Exception as e:
                        log.debug("discardButton click failed: %s", e)
            # If the click triggered "Continue without saving?" — accept it.
            # The dialog uses role="dialog" with a "Continue" button.
            cont = self._page.locator(
                '[role="dialog"] button:has-text("Continue"), '
                '[role="alertdialog"] button:has-text("Continue")'
            )
            if await cont.count() > 0:
                log.debug("dismissing 'Continue without saving?' dialog")
                try:
                    await cont.first.click(timeout=1000)
                    closed_any = True
                except Exception:
                    pass
            # Generic fallbacks for older / variant builds.
            for sel in (
                'button[aria-label*="Close pane" i]',
                'button[data-testid*="closeReferencePane" i]',
                'button[data-testid*="closeSidePane" i]',
            ):
                btns = self._page.locator(sel)
                if await btns.count() > 0:
                    log.debug("closing side pane via %s", sel)
                    try:
                        await btns.first.click(timeout=1000)
                        closed_any = True
                    except Exception:
                        pass
        except Exception as e:
            log.debug("side-pane probe failed: %s", e)
        return closed_any

    async def send(self, prompt: str) -> AsyncIterator[str]:
        assert self._page and self._capture
        page = self._page
        sel = self.settings.selectors

        await self._close_side_panes()
        prior_count = await page.locator(sel.response_messages).count()

        input_box = page.locator(sel.input_box).first
        await input_box.wait_for(state="visible", timeout=15000)
        await input_box.click()
        # Lexical editor (M365's chat input) accepts both fill() and the
        # native execCommand('insertText') path. fill() is much faster than
        # typing keystroke-by-keystroke (~12s for 3KB → instant).
        try:
            await input_box.fill(prompt)
        except Exception:
            # Some Lexical builds reject .fill(); fall back to a single
            # InsertText event which Lexical handles natively.
            await page.keyboard.press("Meta+A" if self._is_mac() else "Control+A")
            await page.keyboard.press("Delete")
            await page.evaluate("(t) => document.execCommand('insertText', false, t)", prompt)

        # Background coroutine: poll every 1.5s for the Pages side pane
        # opening mid-stream and click its close button. Without this the
        # response gets routed to the pane and the chat bubble sticks on
        # "Lining things up..." until our DOM-poll fallback grabs that
        # placeholder.
        pane_stop = asyncio.Event()

        async def _pane_watcher() -> None:
            while not pane_stop.is_set():
                try:
                    if await self._close_side_panes():
                        log.info("closed Pages side pane that opened mid-turn")
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(pane_stop.wait(), timeout=1.5)
                except asyncio.TimeoutError:
                    pass

        watcher_task = asyncio.create_task(_pane_watcher())

        self._network_idle_event.clear()
        self._capture.begin_turn()
        try:
            # Modern M365 Copilot has no dedicated Send button — submission is
            # via Enter on the Lexical editor. Try Enter first; if the input
            # still has the prompt afterwards, fall back to clicking the
            # configured send_button selector.
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.3)
            try:
                still_has = (await input_box.inner_text(timeout=1000)).strip()
            except Exception:
                still_has = ""
            if still_has and still_has.startswith(prompt[:30]):
                send_btn = page.locator(sel.send_button).first
                if await send_btn.count() > 0:
                    await send_btn.click()

            # Race: try WebSocket capture first. If a delta arrives within
            # ws_first_delta_timeout, stream from the WS for the rest of the
            # turn. Otherwise fall back to DOM polling.
            first = await self._capture.wait_for_first_delta(
                timeout=self.settings.ws_first_delta_timeout
            )
            if first is not None and first.kind in ("full", "chunk"):
                log.debug("streaming via WebSocket capture")
                async for delta in self._capture.stream_deltas(
                    idle_timeout=self.settings.response_stable_seconds,
                    hard_timeout=self.settings.response_timeout_seconds,
                ):
                    yield delta
                return

            log.debug("WS capture produced nothing; falling back to DOM polling")
            async for delta in self._dom_poll_stream(prior_count):
                yield delta
        finally:
            self._capture.end_turn()
            pane_stop.set()
            try:
                await watcher_task
            except Exception:
                pass

    async def _dom_poll_stream(self, prior_count: int) -> AsyncIterator[str]:
        assert self._page
        page = self._page
        sel = self.settings.selectors
        deadline = asyncio.get_event_loop().time() + self.settings.response_timeout_seconds

        new_msg = None
        while asyncio.get_event_loop().time() < deadline:
            count = await page.locator(sel.response_messages).count()
            if count > prior_count:
                new_msg = page.locator(sel.response_messages).nth(count - 1)
                break
            await asyncio.sleep(0.2)
        if new_msg is None:
            raise TimeoutError("No assistant response bubble appeared")

        last_text = ""
        last_change = asyncio.get_event_loop().time()
        stable = self.settings.response_stable_seconds
        while True:
            now = asyncio.get_event_loop().time()
            if now > deadline:
                break
            try:
                text = await new_msg.inner_text(timeout=1000)
            except Exception:
                text = last_text
            if text != last_text:
                delta = text[len(last_text):] if text.startswith(last_text) else text
                if delta:
                    yield delta
                last_text = text
                last_change = now
            if self._network_idle_event.is_set() and (now - last_change) > 0.4:
                break
            if now - last_change > stable:
                break
            await asyncio.sleep(0.15)

    async def _on_response(self, resp: Response) -> None:
        url = resp.url
        if any(h in url for h in SUBSTRATE_HOSTS) and re.search(r"(chat|conversation|copilot)", url, re.I):
            try:
                if resp.status == 200:
                    self._network_idle_event.set()
            except Exception:
                pass

    async def list_models(self) -> list[str]:
        """Heuristic discovery: open the model dropdown, scrape listbox options.

        Selectors aren't stable, so we look for any button with an
        accessible name matching MODEL_BUTTON_NAME_RE, click it, then read
        elements with role=option / role=menuitem from the popup.
        """
        if self._page is None:
            return []
        page = self._page
        try:
            btn = page.get_by_role("button").filter(has_text=MODEL_BUTTON_NAME_RE).first
            # The model picker mounts a few seconds after the input box.
            # Wait briefly so /models right after startup doesn't return [].
            for _ in range(15):
                if await btn.count() > 0:
                    break
                await asyncio.sleep(0.5)
            if await btn.count() == 0:
                return []
            await btn.click(timeout=3000)
            options = page.get_by_role("option")
            if await options.count() == 0:
                options = page.get_by_role("menuitem")
            names: list[str] = []
            for i in range(await options.count()):
                t = (await options.nth(i).inner_text()).strip()
                if t and t not in names:
                    names.append(t)
            # Close the popup.
            await page.keyboard.press("Escape")
            return names
        except Exception as e:
            log.debug("model discovery failed: %s", e)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return []

    async def set_model(self, name: str) -> bool:
        if self._page is None:
            return False
        page = self._page
        try:
            btn = page.get_by_role("button").filter(has_text=MODEL_BUTTON_NAME_RE).first
            if await btn.count() == 0:
                return False
            await btn.click(timeout=3000)
            options = page.get_by_role("option").filter(has_text=re.compile(re.escape(name), re.I))
            if await options.count() == 0:
                options = page.get_by_role("menuitem").filter(has_text=re.compile(re.escape(name), re.I))
            if await options.count() == 0:
                await page.keyboard.press("Escape")
                return False
            await options.first.click(timeout=3000)
            return True
        except Exception as e:
            log.debug("set_model(%s) failed: %s", name, e)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    @staticmethod
    def _is_mac() -> bool:
        import platform as _p
        return _p.system() == "Darwin"

    async def _wait_for_signin(self) -> None:
        assert self._page
        page = self._page
        sel = self.settings.selectors.input_box
        deadline = asyncio.get_event_loop().time() + 600.0
        while asyncio.get_event_loop().time() < deadline:
            if "login.microsoftonline.com" in page.url or "login.live.com" in page.url:
                log.info("Sign-in required; complete it in the opened Edge window.")
                await asyncio.sleep(2.0)
                continue
            try:
                if await page.locator(sel).count() > 0:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        raise TimeoutError("Timed out waiting for Copilot chat input to appear after sign-in.")
