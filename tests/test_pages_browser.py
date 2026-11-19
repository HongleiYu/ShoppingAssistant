"""Browser smoke for the static demo. Skipped unless the pages workflow asks for it."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from shopassist.pages.build import build_site


@unittest.skipUnless(os.environ.get("SHOPASSIST_PAGES_BROWSER") == "1", "browser smoke is the pages workflow")
class PagesBrowserTest(unittest.TestCase):
    def test_shopping_and_knowledge_trace(self):
        from playwright.sync_api import sync_playwright

        root = Path(tempfile.mkdtemp(prefix="pages-browser-"))
        site = root / "ShoppingAssistant"
        cache = Path(os.environ.get("PYODIDE_CACHE") or "/tmp/pyodide-0.27.7")
        build_site(site, "/ShoppingAssistant/", pyodide_cache=cache, download=True)
        handler = partial(SimpleHTTPRequestHandler, directory=str(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        shots = Path(os.environ.get("PAGES_SCREENSHOT_DIR") or "/tmp/pages-shots")
        shots.mkdir(parents=True, exist_ok=True)
        url = f"http://127.0.0.1:{server.server_address[1]}/ShoppingAssistant/"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.goto(url)
                page.get_by_text("可以直接发送").wait_for(timeout=180000)
                self._send(page, "find drinkware under 20")
                page.get_by_text("USD").first.wait_for()
                self._send(page, "compare Matte ceramic mug 350 ml and Blue stoneware mug 350 ml")
                page.locator("table.compare").wait_for()
                page.screenshot(path=str(shots / "pages_desktop_compare.png"), full_page=True)
                self._send(page, "change the budget to 13")
                page.get_by_text(
                    "Matte ceramic mug 350 ml (prod-mug-sand) is 12 USD with stock 20.",
                    exact=True,
                ).wait_for()
                self._send(page, "tracking TRK-OK")
                page.locator(".bubble.assistant .text", has_text="Tracking TRK-OK is in_transit").wait_for()
                self._send(page, "tracking TRK-NO-SUCH")
                page.locator(".bubble.assistant .text", has_text="no record").wait_for()
                self._send(page, "kb policy: 会员积分")
                page.locator(".bubble.assistant .text", has_text="hashing lexical baseline").wait_for()
                page.locator(".bubble.assistant .text", has_text="syn-pol-001#s1").wait_for()
                page.locator(".bubble.assistant .text", has_text="points_expire_quarters is 1").wait_for()
                page.screenshot(path=str(shots / "pages_desktop_knowledge.png"), full_page=True)
                page.get_by_role("button", name="新对话").click()
                page.get_by_text("新对话已清空").wait_for()
                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile.goto(url)
                mobile.get_by_text("可以直接发送").wait_for(timeout=180000)
                mobile.locator("#message").fill("kb product: harbor linen napkin")
                mobile.locator("#send").click()
                mobile.locator(".price", has_text="18 USD").wait_for()
                mobile.get_by_role("button", name="链路").click()
                mobile.locator("#trace strong", has_text="kb.retrieve").wait_for()
                mobile.screenshot(path=str(shots / "pages_mobile_product.png"), full_page=True)
                browser.close()
        finally:
            server.shutdown()
            shutil.rmtree(root, ignore_errors=True)

    def _send(self, page, text: str) -> None:
        page.locator("#message").fill(text)
        page.locator("#send").click()
        page.get_by_text("这一轮已经结束").wait_for(timeout=60000)
