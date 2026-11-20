"""Pages demo projection, static build, and parser prompts."""

from __future__ import annotations

import json
import re
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
        self.assertIsNone(re.search(r"[\u3400-\u9fff\uf900-\ufaff]", readme))

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
            self.assertNotIn("http://127.0.0.1", blob)
            self.assertNotIn("https://127.0.0.1", blob)
            self.assertNotIn('type="password"', blob)
            self.assertNotIn("enable_model", blob)
            self.assertNotIn("apiKey", blob)
            self.assertNotIn("js_model_fetch", blob)
            page_js = (output / "app.js").read_text(encoding="utf-8")
            self.assertNotIn("model-key", page_js)
            self.assertEqual(page_js.count("localStorage.setItem"), 1)
            self.assertIn("locale_storage_key", page_js)
            self.assertNotIn("sessionStorage", page_js)
            self.assertNotIn("indexedDB", page_js)
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
            self.assertEqual(asked_shoes["display_cards"], [])
            phases = [(item["agent"], item["phase"]) for item in asked_shoes["model_invocations"]]
            self.assertEqual(phases[0], ("ca", "route"))
            self.assertEqual(phases[-1], ("ca", "answer"))
            self.assertEqual(asked_shoes["model_invocations"][0]["output"]["skill_id"], "product_quote")
            self.assertTrue(all(item["simulated"] and item["network"] is False for item in asked_shoes["model_invocations"]))
            self.assertFalse(asked_shoes["model_called"])
            self.assertTrue(asked_shoes["presentation"])
            self.assertTrue(any(step.get("kind") == "grounded" for step in asked_shoes["presentation"]))
            blue = app.turn("How much is the Blue stoneware mug 350 ml?")
            self.assertEqual([card["product_id"] for card in blue["display_cards"]], ["prod-mug-blue"])
            again = app.turn("How much is this item?")
            self.assertEqual(again["consulted_product_id"], "prod-mug-blue")
            self.assertEqual(again["display_cards"], [])
            search = app.turn("find drinkware under 20")
            self.assertEqual(search["model_invocations"][0]["output"]["route"], "complex_handoff")
            self.assertTrue(any(item["agent"] == "pa" and item["phase"] == "plan_and_tool" for item in search["model_invocations"]))
            self.assertEqual(search["model_invocations"][-1]["phase"], "answer")
            self.assertEqual(
                [card["product_id"] for card in search["display_cards"]],
                ["prod-btl-steel", "prod-mug-blue", "prod-mug-sand"],
            )
            compared = app.turn("compare Matte ceramic mug 350 ml and Blue stoneware mug 350 ml")
            self.assertEqual(
                [card["product_id"] for card in compared["display_cards"]],
                ["prod-mug-blue", "prod-mug-sand"],
            )
        finally:
            app.close()

    def test_scenario_copy_and_suggestions_differ(self):
        config = json.loads((ROOT / "shopassist" / "pages" / "demo_config.json").read_text(encoding="utf-8"))
        welcomes = [scenario["welcome"]["en"] for scenario in config["scenarios"]]
        self.assertEqual(len(welcomes), len(set(welcomes)))
        suggestions = [tuple(scenario["suggestions"]["en"]) for scenario in config["scenarios"]]
        self.assertEqual(len(suggestions), len(set(suggestions)))
        self.assertNotEqual(
            next(item for item in config["scenarios"] if item["id"] == "shipment_known")["context_note"]["en"],
            next(item for item in config["scenarios"] if item["id"] == "shipment_missing")["context_note"]["en"],
        )
        for scenario in config["scenarios"]:
            for locale, lines in scenario["suggestions"].items():
                for line in lines:
                    self.assertNotEqual(detect_intent(line), "unknown", line)
            self.assertEqual(len(scenario["suggestions"]["en"]), len(set(scenario["suggestions"]["en"])))

    def test_unrecognized_text_is_not_a_catalog_miss(self):
        from shopassist.reply_render import DEMO_UNSUPPORTED_EN

        app = DemoApp()
        try:
            app.consult("prod-shoes-court")
            english = app.turn("asdf qwerty not a shopping question")
            chinese = app.turn("随便聊聊今天")
            self.assertEqual(english["route_reason_code"], "unrecognized")
            self.assertEqual(chinese["route_reason_code"], "unrecognized")
            self.assertEqual(english["reply_text"], DEMO_UNSUPPORTED_EN)
            self.assertNotIn("48", english["reply_text"])
            self.assertEqual(app.turn("这个商品多少钱？")["cards"][0]["fields"]["price"], 48)
            missing = app.turn("How much is it?")
            self.assertNotEqual(missing["route_reason_code"], "unrecognized")
            self.assertEqual(english["terminal"]["reason_code"], "unrecognized")
            self.assertNotIn("price", english["reply_text"].lower())
        finally:
            app.close()

    def test_example_questions_match_advertised_model_calls(self):
        config = json.loads((ROOT / "shopassist" / "pages" / "demo_config.json").read_text(encoding="utf-8"))
        classes = set()
        app = DemoApp()
        try:
            for scenario in config["scenarios"]:
                app.consult(scenario["product_ids"][0])
                for locale in ("en", "zh"):
                    turn = app.turn(scenario["samples"][locale])
                    invocations = turn["model_invocations"]
                    ca = sum(1 for item in invocations if item["agent"] == "ca")
                    pa = sum(1 for item in invocations if item["agent"] == "pa")
                    self.assertEqual(ca, scenario["calls"]["ca"], scenario["id"] + locale)
                    self.assertEqual(pa, scenario["calls"]["pa"], scenario["id"] + locale)
                    self.assertEqual(ca + pa, len(invocations))
                    self.assertTrue(all(item["simulated"] and item["network"] is False for item in invocations))
                    classes.add((ca, pa))
                    if pa == 2:
                        self.assertEqual(
                            [item["phase"] for item in invocations if item["agent"] == "pa"],
                            ["plan_and_tool", "observe_and_finish"],
                        )
                        self.assertEqual([skill["skill_id"] for skill in turn["skills"]], ["product_search"])
            self.assertIn((1, 0), classes)
            self.assertIn((2, 0), classes)
            self.assertIn((2, 2), classes)
            self.assertTrue(any(ca == 2 and pa > 2 for ca, pa in classes))
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
