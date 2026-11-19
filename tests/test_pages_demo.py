"""Pages demo projection, static build, and parser prompts."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from shopassist.pages.build import build_site
from shopassist.pages_demo import DemoApp, comparable, project_turn
from shopassist.parsing import detect_intent
from shopassist.sandbox import SessionSandbox
from shopassist.session_runner import AgentSession
from shopassist.settings import load_settings

ROOT = Path(__file__).resolve().parents[1]
TURNS = (
    "find drinkware under 20",
    "change the budget to 13",
    "compare Matte ceramic mug 350 ml and Blue stoneware mug 350 ml",
    "tracking TRK-OK",
    "tracking TRK-NO-SUCH",
)


class PagesProjectionTest(unittest.TestCase):
    def test_prompts_match_the_parser(self):
        config = json.loads((ROOT / "shopassist" / "pages" / "demo_config.json").read_text(encoding="utf-8"))
        for locale, prompts in config["prompts"].items():
            for prompt in prompts:
                self.assertEqual(detect_intent(prompt["text"]), prompt["intent"], locale)
        self.assertEqual(config["brand"], "Demo Flagship Store")
        self.assertEqual(config["brand_zh"], "演示旗舰店")
        self.assertEqual(config["default_product_id"], "prod-shoes-court")
        self.assertNotIn("guided", config)
        self.assertNotIn("澄清", json.dumps(config["ui"]["zh"]["title"], ensure_ascii=False))
        from shopassist.builtin_data import PRODUCTS

        catalog_ids = {row["product_id"] for row in PRODUCTS}
        self.assertIn("prod-shoes-court", catalog_ids)
        seen = set()
        for scenario in config["scenarios"]:
            self.assertNotIn(scenario["id"], seen)
            seen.add(scenario["id"])
            self.assertGreaterEqual(len(scenario["product_ids"]), 1)
            self.assertEqual(len(scenario["product_ids"]), len(set(scenario["product_ids"])))
            for locale, text in scenario["samples"].items():
                self.assertEqual(detect_intent(text), scenario["intent"], text)
            for product_id in scenario["product_ids"]:
                self.assertIn(product_id, catalog_ids)
                relative = config["images"][product_id]
                blob = (ROOT / "shopassist" / "pages" / "static" / relative).read_bytes()
                self.assertGreater(len(blob), 1000)
                self.assertTrue(blob.startswith(b"\xff\xd8\xff"), relative)
        html = (ROOT / "shopassist" / "pages" / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "shopassist" / "pages" / "static" / "app.js").read_text(encoding="utf-8")
        for banned in ('id="skip"', 'id="reset"', 'id="open-trace"', 'id="replay"', 'id="own-chat"', ">New chat<", ">Skip<", ">Trace<"):
            self.assertNotIn(banned, html)
        self.assertIn('id="trace-panel"', html)
        self.assertIn("Demo Flagship Store", html)
        self.assertNotIn("startGuide", script)
        self.assertNotIn("sessionStorage", script)
        self.assertNotIn("autoplay", script)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for item in config["differences"]:
            self.assertIn(item["en"], readme)

    def test_in_process_matches_mcp_for_the_demo_turns(self):
        settings = load_settings(defaults_only=True)
        demo = DemoApp()
        box = SessionSandbox.start()
        try:
            session = AgentSession(box, session_id="pages-parity", mode="mock", settings=settings)
            for text in TURNS:
                projected = demo.turn(text)
                self.assertIsInstance(projected["duration_ms"], float)
                self.assertGreaterEqual(projected["duration_ms"], 0)
                left = comparable(projected)
                right = comparable(project_turn(session.user_message(text), request_id="x", duration_ms=0))
                self.assertEqual(left["reply_text"], right["reply_text"])
                self.assertEqual(left["route"], right["route"])
                self.assertEqual(left["terminal"], right["terminal"])
                self.assertEqual(left["skills"], right["skills"])
                self.assertEqual([tool["tool"] for tool in left["tools"]], [tool["tool"] for tool in right["tools"]])
                self.assertFalse(left["model_called"])
                self.assertFalse(left["mcp_process"])
        finally:
            demo.close()
            box.close()

    def test_static_build_uses_the_project_base_and_hashing_index(self):
        output = Path(tempfile.mkdtemp(prefix="pages-site-"))
        try:
            build_site(output, "/ShoppingAssistant/", download=False)
            html = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn('<base href="/ShoppingAssistant/">', html)
            self.assertNotIn("__SHOPASSIST_BASE__", html)
            manifest = json.loads((output / "kb-index" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["encoder_kind"], "hashing_lexical_baseline")
            self.assertFalse(manifest["semantic"])
            self.assertEqual(manifest["policy_documents"], 50)
            self.assertEqual(manifest["product_records"], 1000)
            blob = "\n".join(
                path.read_text(encoding="utf-8", errors="ignore")
                for path in output.rglob("*")
                if path.suffix in {".html", ".js", ".css", ".json"} and path.name != "runtime.json"
            )
            self.assertNotIn("cdn.jsdelivr.net", blob)
            self.assertNotIn("127.0.0.1", blob)
            self.assertTrue((output / ".nojekyll").is_file())
            self.assertTrue((output / "products" / "prod-shoes-court.jpg").is_file())
            demo = json.loads((output / "demo-config.json").read_text(encoding="utf-8"))
            self.assertEqual(demo["default_product_id"], "prod-shoes-court")
            self.assertNotIn("https://", html)
            self.assertNotIn("http://", html)
        finally:
            shutil.rmtree(output, ignore_errors=True)


class ProductEntryTest(unittest.TestCase):
    def test_consult_follow_up_switch_and_unknown(self):
        app = DemoApp()
        try:
            sand = app.consult("prod-mug-sand")
            self.assertTrue(sand["ok"])
            self.assertEqual(sand["card"]["fields"]["price"], 12)
            self.assertEqual(sand["card"]["fields"]["stock"], 20)
            self.assertNotIn("price", sand["tool"]["arguments"])
            asked = app.turn("这个商品多少钱？")
            self.assertEqual(asked["consulted_product_id"], "prod-mug-sand")
            self.assertEqual(asked["cards"][0]["fields"]["price"], 12)
            stock = app.turn("Is this item in stock?")
            self.assertEqual(stock["cards"][0]["fields"]["stock"], 20)
            blue = app.turn("How much is the Blue stoneware mug 350 ml?")
            self.assertEqual(blue["consulted_product_id"], "prod-mug-blue")
            again = app.turn("这个商品多少钱？")
            self.assertEqual(again["cards"][0]["fields"]["price"], 14)
            self.assertNotEqual(again["cards"][0]["product_id"], "prod-mug-sand")
            returns = app.turn("退货时限是多久？")
            self.assertIn({"field": "window_days", "value": 30, "product_id": None, "doc_id": "kb-returns-v3"}, returns["facts"])
            fresh = app.reset("prod-mug-sand")
            self.assertEqual(fresh["consult"]["card"]["product_id"], "prod-mug-sand")
            self.assertEqual(app.turn("How much is this item?")["cards"][0]["fields"]["price"], 12)
            self.assertFalse(app.consult("prod-not-real")["ok"])
            self.assertFalse(app.consult("../secret")["ok"])
            shoes = app.consult("prod-shoes-court")
            self.assertTrue(shoes["ok"])
            self.assertEqual(shoes["card"]["name"], "Canvas court shoes")
            self.assertEqual(shoes["card"]["fields"]["price"], 48)
            self.assertEqual(shoes["card"]["fields"]["stock"], 16)
            self.assertEqual(shoes["card"]["fields"]["category"], "footwear")
            self.assertNotIn("price", shoes["tool"]["arguments"])
            asked_shoes = app.turn("How much is this item?")
            self.assertEqual(asked_shoes["consulted_product_id"], "prod-shoes-court")
            self.assertEqual(asked_shoes["cards"][0]["fields"]["price"], 48)
            self.assertEqual(asked_shoes["cards"][0]["fields"]["stock"], 16)
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
