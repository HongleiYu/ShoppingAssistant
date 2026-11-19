"""Browser smoke for the product-entry chat and guided demo."""

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
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="pages-browser-"))
        cls.site = cls.root / "ShoppingAssistant"
        cache = Path(os.environ.get("PYODIDE_CACHE") or "/tmp/pyodide-0.27.7")
        build_site(cls.site, "/ShoppingAssistant/", pyodide_cache=cache, download=True)
        handler = partial(SimpleHTTPRequestHandler, directory=str(cls.root))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/ShoppingAssistant/"
        cls.shots = Path(os.environ.get("PAGES_SCREENSHOT_DIR") or "/tmp/pages-shots")
        cls.shots.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_english_guided_then_own_chat(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.goto(self.url + "?guided=1")
            page.get_by_text("Hello. You opened this chat from the product page.").wait_for(timeout=180000)
            page.locator(".price", has_text="12 USD").first.wait_for()
            page.locator(".guided-user", has_text="How much is this item?").wait_for(timeout=30000)
            page.get_by_text("This item is 12 USD, with 20 in stock.").wait_for()
            page.locator(".step-trace", has_text="product_quote").wait_for()
            page.screenshot(path=str(self.shots / "pages_guided_desktop.png"))
            page.locator(".guided-user", has_text="find drinkware under 20").wait_for(timeout=30000)
            page.locator(".step-trace", has_text="product_search").wait_for()
            page.locator(".guided-user", has_text="What is the return window?").wait_for(timeout=30000)
            page.locator(".bubble.assistant .text", has_text="The current return window is 30 days").wait_for()
            page.locator("details.step-trace").last.click()
            page.screenshot(path=str(self.shots / "pages_trace_step.png"))
            page.locator("#own-chat").click()
            page.wait_for_function("() => document.querySelectorAll('.guided-user').length === 0", timeout=15000)
            page.get_by_text("Hello. You opened this chat from the product page.").wait_for()
            page.screenshot(path=str(self.shots / "pages_own_chat_desktop.png"))
            context.close()
            browser.close()

    def test_chinese_guided_steps(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.url + "?locale=zh&guided=1")
            page.get_by_text("你好。你是从商品页进来的。").wait_for(timeout=180000)
            page.locator(".guided-user", has_text="这个商品多少钱？").wait_for(timeout=30000)
            page.get_by_text("这件商品是 12 美元，库存 20。").wait_for()
            page.locator(".guided-user", has_text="找 drinkware，20以内").wait_for(timeout=30000)
            page.locator(".guided-user", has_text="退货时限是多久？").wait_for(timeout=30000)
            page.get_by_text("现行退货时限是 30 天").wait_for()
            browser.close()

    def test_skip_before_ready(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.url + "?guided=1")
            page.get_by_text("Loading the local runtime.").wait_for(timeout=30000)
            page.locator("#skip").click()
            page.get_by_text("Hello. You opened this chat from the product page.").wait_for(timeout=180000)
            page.wait_for_timeout(2000)
            self.assertEqual(page.locator(".guided-user").count(), 0)
            browser.close()

    def test_takeover_during_pause(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.url + "?guided=1")
            page.get_by_text("This item is 12 USD, with 20 in stock.").wait_for(timeout=180000)
            page.locator("#message").fill("What is the return window?")
            page.locator("#send").click()
            page.locator(".bubble.assistant .text", has_text="The current return window is 30 days").wait_for(timeout=30000)
            page.wait_for_timeout(2200)
            self.assertEqual(page.locator(".guided-user", has_text="find drinkware under 20").count(), 0)
            browser.close()

    def test_replay_reset_race(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.url + "?guided=1")
            page.locator(".guided-user").first.wait_for(timeout=180000)
            page.locator("#replay").click()
            page.locator("#reset").click()
            page.get_by_text("Hello. You opened this chat from the product page.").wait_for(timeout=30000)
            page.wait_for_timeout(2500)
            self.assertEqual(page.locator(".guided-user").count(), 0)
            browser.close()

    def test_unknown_product_and_mobile_scenarios(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(self.url + "?product=prod-not-real&guided=0")
            page.get_by_text("That product id is not in the snapshot.").wait_for(timeout=180000)
            self.assertEqual(page.locator(".price").count(), 0)
            box = page.locator("#composer").bounding_box()
            self.assertIsNotNone(box)
            self.assertLessEqual(box["y"] + box["height"], 844 + 1)
            page.locator("#open-scenarios").click()
            page.locator("#scenario-list").get_by_text("How much is this item?").wait_for()
            page.screenshot(path=str(self.shots / "pages_mobile_scenarios.png"))
            browser.close()
