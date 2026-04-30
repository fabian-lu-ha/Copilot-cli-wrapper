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
from copilot_cli.config import Settings

log = logging.getLogger(__name__)

SUBSTRATE_HOSTS = ("substrate.office.com", "cloud.microsoft", "office.com")

# Heuristic regex for the model picker button's accessible name. M365 Copilot
# doesn't publish stable testid/aria-label values for it; the button label is
# usually the active model name. Per the April 2026 Copilot update, common
# names include "GPT-5.x Quick", "GPT-5.x Thinking", "Auto", "Smart",
# "Researcher", "Claude Sonnet 4.5" (when tenant enables Anthropic).
MODEL_BUTTON_NAME_RE = re.compile(r"(GPT-|Think|Quick|Smart|Auto|Claude|Researcher|Reasoning)", re.I)


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

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.profile_dir),
            channel=self.settings.edge_channel,
            headless=self.settings.headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._page.on("response", self._on_response)
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

    async def send(self, prompt: str) -> AsyncIterator[str]:
        assert self._page
        page = self._page
        sel = self.settings.selectors

        prior_count = await page.locator(sel.response_messages).count()

        input_box = page.locator(sel.input_box).first
        await input_box.wait_for(state="visible", timeout=15000)
        await input_box.click()
        await input_box.fill("")
        await input_box.type(prompt, delay=4)

        self._network_idle_event.clear()
        send_btn = page.locator(sel.send_button).first
        await send_btn.click()

        # Wait for a new assistant bubble to appear.
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
