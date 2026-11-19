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
        for prompt in config["prompts"]:
            self.assertEqual(detect_intent(prompt["text"]), prompt["intent"])
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
        finally:
            shutil.rmtree(output, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
