"""Browser smoke for the shop consultation page."""

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

    def test_english_shoe_chat_trace_and_scenarios(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.goto(self.url)
            page.get_by_text("Hello. You opened the canvas court shoes from the product page.").wait_for(timeout=180000)
            page.locator("#brand-name", has_text="Demo Flagship Store").wait_for()
            page.locator(".product-card img[alt='Canvas court shoes']").wait_for()
            page.wait_for_function(
                """() => {
                  const img = document.querySelector(".product-card img");
                  return img && img.complete && img.naturalWidth > 32;
                }"""
            )
            page.wait_for_timeout(2000)
            self.assertEqual(page.locator(".bubble.user").count(), 0)
            for label in ("Trace", "Skip", "New chat", "Replay", "链路", "跳过", "新对话", "重播"):
                self.assertEqual(page.get_by_role("button", name=label, exact=True).count(), 0)
            self.assertGreater(
                page.locator("#trace-panel").bounding_box()["x"],
                page.locator(".chat").bounding_box()["x"],
            )
            page.screenshot(path=str(self.shots / "pages_shoe_desktop.png"))
            page.locator("#chips").get_by_text("How much is this item?").wait_for()
            page.locator("#message").fill("How much is this item?")
            page.locator("#send").click()
            page.locator(".progress-live").wait_for(timeout=30000)
            page.screenshot(path=str(self.shots / "pages_progress_desktop.png"))
            page.get_by_text("This item is 48 USD, with 16 in stock.").wait_for(timeout=30000)
            page.wait_for_function("() => !document.querySelector('.progress-live')")
            self.assertEqual(page.locator(".product-card img[alt='Canvas court shoes']").count(), 1)
            page.locator("#trace-panel").get_by_text("ca route").wait_for()
            page.locator("#trace-panel").get_by_text("ca answer").wait_for()
            page.locator("#trace-panel").get_by_text("Skill product_quote · ok").wait_for()
            page.locator("#trace-panel").get_by_text("Evidence gate").wait_for()
            user = page.locator(".bubble.user .text").first.bounding_box()
            bot = page.locator(".bubble.assistant .text").first.bounding_box()
            self.assertLess(bot["x"], user["x"])
            self.assertGreater(user["x"] + user["width"], bot["x"] + bot["width"])
            page.screenshot(path=str(self.shots / "pages_trace_desktop.png"))
            page.locator("#open-scenarios").click()
            page.locator("#scenario-list article").first.wait_for()
            self.assertEqual(page.locator("#scenario-list article").count(), 10)
            self.assertEqual(page.locator("#scenario-list article button").count(), 10)
            self.assertEqual(page.locator("#scenario-list button", has_text="Enter").count(), 10)
            self.assertEqual(page.locator("#scenario-list button", has_text="How much is this item?").count(), 0)
            page.wait_for_function(
                """() => {
                  const images = [...document.querySelectorAll("#scenario-list img")];
                  return images.length >= 8 && images.every((img) => img.complete && img.naturalWidth > 32);
                }"""
            )
            page.screenshot(path=str(self.shots / "pages_scenarios_desktop.png"))
            page.locator("article[data-scenario='search_budget'] button").click()
            page.get_by_text("This conversation searches drinkware under 20.").wait_for(timeout=30000)
            self.assertEqual(page.locator("#chips").get_by_text("How much is this item?").count(), 0)
            page.locator("#chips").get_by_text("find drinkware under 20").wait_for()
            page.locator(".product-card strong", has_text="Matte ceramic mug 350 ml").wait_for()
            page.screenshot(path=str(self.shots / "pages_scenario_search.png"))
            page.wait_for_timeout(400)
            self.assertEqual(page.locator(".bubble.user").count(), 0)
            self.assertEqual(page.locator("#trace-panel .trace-turn").count(), 0)
            page.locator("#message").fill("find drinkware under 20")
            page.locator("#send").click()
            page.locator("#trace-panel").get_by_text("Skill product_search · ok").wait_for(timeout=30000)
            page.locator("#trace-panel").get_by_text("pa plan").wait_for()
            page.screenshot(path=str(self.shots / "pages_pa_trace_desktop.png"))
            for product_id in ("prod-mug-sand", "prod-mug-blue", "prod-btl-steel"):
                page.locator(f".product-card img[src*='{product_id}']").first.wait_for()
            page.wait_for_function("() => !document.querySelector('#send').disabled")
            page.locator("#message").fill("What is the return window?")
            page.locator("#send").click()
            page.locator(".progress-live").wait_for(timeout=30000)
            page.locator("#open-scenarios").click()
            page.locator("article[data-scenario='shipment_known'] button").click()
            page.get_by_text("This conversation looks up synthetic tracking id TRK-OK.").wait_for(timeout=30000)
            page.screenshot(path=str(self.shots / "pages_scenario_shipment.png"))
            page.locator(".product-card img[alt='Canvas court shoes']").wait_for(timeout=30000)
            page.wait_for_timeout(1500)
            self.assertEqual(page.locator(".bubble.user").count(), 0)
            self.assertEqual(page.locator("#trace-panel .trace-turn").count(), 0)
            self.assertEqual(page.get_by_text("The current return window is 30 days").count(), 0)
            context.close()
            browser.close()

    def test_chinese_store_quote(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.url + "?locale=zh")
            page.locator("#brand-name", has_text="演示旗舰店").wait_for(timeout=180000)
            page.get_by_text("你好。你从商品页打开的是这双帆布鞋。").wait_for()
            page.wait_for_timeout(1500)
            self.assertEqual(page.locator(".bubble.user").count(), 0)
            for label in ("链路", "跳过", "新对话", "Trace", "Skip", "New chat"):
                self.assertEqual(page.get_by_role("button", name=label, exact=True).count(), 0)
            page.locator("#open-scenarios").click()
            self.assertEqual(page.locator("#scenario-list button", has_text="进入").count(), 10)
            page.locator("article[data-scenario='consult_stock'] button").click()
            page.wait_for_function("() => document.querySelector('#status').textContent === '可以发送。'")
            page.locator("#message").fill("这个商品多少钱？")
            page.locator("#send").click()
            page.get_by_text("这件商品是 48 美元，库存 16。").wait_for(timeout=30000)
            page.locator("#trace-panel").get_by_text("技能 product_quote · ok").wait_for()
            browser.close()

    def test_mobile_layout_and_unknown_product(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(self.url)
            page.get_by_text("Hello. You opened the canvas court shoes from the product page.").wait_for(timeout=180000)
            page.locator(".product-card img[alt='Canvas court shoes']").wait_for()
            chat = page.locator(".chat").bounding_box()
            trace = page.locator("#trace-panel").bounding_box()
            self.assertIsNotNone(chat)
            self.assertIsNotNone(trace)
            self.assertGreater(trace["y"], chat["y"])
            box = page.locator("#composer").bounding_box()
            self.assertIsNotNone(box)
            self.assertLessEqual(box["y"] + box["height"], 844 + 1)
            page.screenshot(path=str(self.shots / "pages_mobile.png"))
            page.goto(self.url + "?product=prod-not-real")
            page.get_by_text("That product id is not in the snapshot.").wait_for(timeout=180000)
            self.assertEqual(page.locator(".price").count(), 0)
            browser.close()


    def test_one_card_and_unsupported_questions(self):
        from playwright.sync_api import sync_playwright

        welcomes = {
            "consult_price": "Hello. You opened the canvas court shoes",
            "capabilities": "asks what the shop demo can answer",
            "consult_stock": "whether the canvas court shoes are in stock",
            "browse_budget": "browses drinkware under 20",
            "search_budget": "searches drinkware under 20",
            "compare": "compares the matte ceramic mug",
            "returns": "shared return note",
            "shipment_known": "tracking id TRK-OK",
            "shipment_missing": "TRK-NO-SUCH",
            "knowledge": "fictional membership note",
        }
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.url)
            page.get_by_text("Hello. You opened the canvas court shoes from the product page.").wait_for(timeout=180000)
            self.assertEqual(page.locator("#model-key").count(), 0)
            self.assertEqual(page.locator("input[type='password']").count(), 0)
            self.assertNotIn("API key", page.content())
            for scenario, snippet in welcomes.items():
                page.locator("#open-scenarios").click()
                page.locator(f"article[data-scenario='{scenario}'] button").click()
                page.locator("[data-role='welcome']", has_text=snippet).wait_for(timeout=30000)
                self.assertEqual(page.locator(".welcome-products .product-card").count(), 1)
                self.assertEqual(page.locator(".bubble.user").count(), 0)
                if scenario == "compare":
                    page.screenshot(path=str(self.shots / "pages_compare_entry.png"))
            page.locator("#open-scenarios").click()
            page.locator("article[data-scenario='consult_price'] button").click()
            page.locator("#message").fill("asdf qwerty not a shopping question")
            page.locator("#send").click()
            page.locator(".reply", has_text="This code demo only supports the provided example questions.").wait_for(timeout=30000)
            page.screenshot(path=str(self.shots / "pages_unsupported.png"))
            page.locator("#lang-zh").click()
            page.locator("#message").fill("随便聊聊今天")
            page.locator("#send").click()
            page.locator(".reply", has_text="当前代码演示仅支持给定的示例问题，请选择下方建议问题或支持的场景。").last.wait_for(timeout=30000)
            page.locator("#lang-en").click()
            page.locator("#open-scenarios").click()
            page.get_by_text("Example turn: CA 1, PA 0, total 1.").wait_for()
            page.get_by_text("Example turn: CA 2, PA 2, total 4.").wait_for()
            page.get_by_text("Example turn: CA 2, PA 3, total 5.").wait_for()
            page.get_by_text("These counts are for the example question on the card, not a guarantee for a later message.").wait_for()
            page.screenshot(path=str(self.shots / "pages_scenario_calls.png"))
            page.locator("#close-scenarios").click()
            page.locator("#lang-zh").click()
            page.locator("#open-scenarios").click()
            page.get_by_text("示例这一轮：沟通代理 2 次，规划代理 2 次，合计 4 次。").wait_for()
            page.get_by_text("这些次数只对卡片上的示例问题，不是之后任意输入的保证。").wait_for()
            page.locator("#close-scenarios").click()
            page.locator("#lang-en").click()
            page.locator("#open-scenarios").click()
            page.locator("article[data-scenario='browse_budget'] button").click()
            page.locator("[data-role='welcome']", has_text="browses drinkware under 20").wait_for(timeout=30000)
            page.locator("#chips button", has_text="browse drinkware under 20").click()
            page.locator(".reply", has_text="These items matched. Prices and stock are from the snapshot.").wait_for(timeout=30000)
            self.assertEqual(page.locator("[data-trace='sim-ca'] b").inner_text(), "2")
            self.assertEqual(page.locator("[data-trace='sim-pa'] b").inner_text(), "2")
            self.assertEqual(page.locator("[data-trace='sim-total'] b").inner_text(), "4")
            self.assertEqual(page.locator("[data-trace='sim-network'] b").inner_text(), "0")
            self.assertIn("2. pa plan_and_tool", page.locator("#trace-body").inner_text())
            self.assertIn("3. pa observe_and_finish", page.locator("#trace-body").inner_text())
            page.screenshot(path=str(self.shots / "pages_pa2_trace.png"))
            page.locator("#open-scenarios").click()
            page.locator("article[data-scenario='search_budget'] button").click()
            page.locator("[data-role='welcome']", has_text="searches drinkware under 20").wait_for(timeout=30000)
            page.locator("#chips button", has_text="find drinkware under 20").click()
            page.locator(".reply", has_text="These items matched. Prices and stock are from the snapshot.").last.wait_for(timeout=30000)
            self.assertEqual(page.locator("[data-trace='sim-ca'] b").inner_text(), "2")
            self.assertEqual(page.locator("[data-trace='sim-pa'] b").inner_text(), "3")
            self.assertEqual(page.locator("[data-trace='sim-total'] b").inner_text(), "5")
            self.assertIn("3. pa observe_and_tool", page.locator("#trace-body").inner_text())
            self.assertIn("4. pa observe_and_finish", page.locator("#trace-body").inner_text())
            page.screenshot(path=str(self.shots / "pages_pa3_trace.png"))
            browser.close()
