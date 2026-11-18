"""Offline tests for data, runtime, protocol, config, and the local UI."""

from __future__ import annotations

import ast
import json
import os
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CardsAndSplitsTest(unittest.TestCase):
    def test_every_card_passes_outcome_checks(self):
        from shopassist.cards import REQUIRED_TAGS, all_cards
        from shopassist.corpus import assert_split_integrity, coverage_report
        from shopassist.harness import evaluate_card

        cards = all_cards()
        assert_split_integrity(cards)
        report = coverage_report(cards)
        self.assertEqual(report["missing_tags"], [])
        self.assertTrue(set(REQUIRED_TAGS).issubset({tag for card in cards for tag in card["tags"]}))
        self.assertGreater(report["split_counts"]["regression"], 0)
        failed = []
        for card in cards:
            result = evaluate_card(card)
            if not result["score"]["passed"]:
                failed.append((card["card_id"], result["score"]["failures"]))
            self.assertIsNone(result["trace"]["token_usage"])
            self.assertEqual(result["trace"]["evaluation_kind"], "mock_demo")
            self.assertNotEqual(result["trace"]["pid"], result["trace"]["host_pid"])
        self.assertEqual(failed, [])

    def test_compare_plan_dependencies_and_thanks_skips_tools(self):
        from shopassist.cards import all_cards
        from shopassist.harness import evaluate_card
        from shopassist.planner_policy import plan_is_acyclic

        cards = {card["card_id"]: card for card in all_cards()}
        compare = evaluate_card(cards["compare-price"])
        plan = compare["trace"]["turns"][0]["plan"]
        self.assertTrue(plan_is_acyclic(plan))
        compare_step = next(step for step in plan if step["skill_id"] == "product_compare")
        self.assertEqual(len(compare_step["depends_on"]), 2)
        thanks = evaluate_card(cards["thanks-a"])
        self.assertEqual(thanks["trace"]["turns"][0]["tools"], [])
        self.assertFalse(thanks["trace"]["turns"][0]["planner_invoked"])
        self.assertTrue(any(event.get("method") == "resources/read" for event in thanks["trace"]["mcp_events"]))

    def test_split_leak_is_rejected(self):
        from shopassist.cards import all_cards
        from shopassist.corpus import SplitLeakError, assert_split_integrity

        cards = all_cards()
        broken = json.loads(json.dumps(cards))
        broken[0]["split"] = "val" if broken[0]["split"] != "val" else "test"
        # Keep its lineage paired with a sibling when one exists.
        sibling = next(card for card in broken[1:] if card["lineage_id"] == broken[0]["lineage_id"])
        if sibling["split"] == broken[0]["split"]:
            sibling["split"] = "regression"
        with self.assertRaises(SplitLeakError):
            assert_split_integrity(broken)

    def test_simulator_has_no_gold_or_catalog_import(self):
        source = (ROOT / "shopassist" / "simulator.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertFalse(any("gold" in name or "builtin_data" in name or "planner" in name for name in imported))


class MCPTransportTest(unittest.TestCase):
    def test_stdio_discovery_call_and_name_index(self):
        from shopassist.sandbox import SessionSandbox

        box = SessionSandbox.start(seed=4)
        try:
            tools = set(box.process.client.discovered_tools)
            self.assertIn("catalog.search", tools)
            self.assertIn("kb.get_policy", tools)
            index = box.read_name_index()
            self.assertTrue(index)
            self.assertNotIn("price", index[0])
            result = box.invoke("product_quote", {"product_id": "prod-mug-sand", "fresh": False})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["items"][0]["price"], 12)
            rows = box.replay_rows()
            self.assertTrue(any(row.get("tool") == "catalog.quote" for row in rows))
        finally:
            box.close()

    def test_timeout_and_unknown_tool(self):
        import os
        import time
        from tempfile import mkdtemp

        from shopassist.catalog import default_catalog, export_catalog
        from shopassist.knowledge import export_kb
        from shopassist.mcp.client import MCPClient
        from shopassist.mcp.server import serve_on_fds

        root = Path(mkdtemp(prefix="mcp-timeout-"))
        export_catalog(root / "catalog.json")
        export_kb(root / "kb.json")
        read_in, write_in = os.pipe()
        read_out, write_out = os.pipe()

        def slow(_arguments):
            time.sleep(0.4)
            return {"status": "ok", "note": "late"}

        thread = threading.Thread(
            target=serve_on_fds,
            args=(read_in, write_out, default_catalog(), __import__("shopassist.knowledge", fromlist=["default_kb"]).default_kb(), {"seed": 1, "faults": [], "max_calls": 5}, root / "ws"),
            kwargs={"extra_tools": {"debug.sleep": {"description": "test only", "handler": slow, "input": {"required": [], "properties": {}}}}},
            daemon=True,
        )
        thread.start()
        client = MCPClient(write_in, read_out, timeout=0.1)
        client.initialize()
        self.assertIn("debug.sleep", client.discovered_tools)
        with self.assertRaises(TimeoutError):
            client.call_tool("debug.sleep", {})
        os.close(write_in)
        os.close(read_out)

    def test_client_rejects_malformed_shape(self):
        from shopassist.mcp.contracts import validate_search_result

        errors = validate_search_result({"status": "ok", "snapshot_id": "synthetic-catalog-v1", "items": "bad"})
        self.assertTrue(errors)


class SandboxIsolationTest(unittest.TestCase):
    def test_parallel_sessions_and_reset_and_clean_env(self):
        from shopassist.sandbox import SessionSandbox

        sentinel = "unit-test-sentinel-value"
        previous = os.environ.get("SHOPASSIST_CA_API_KEY")
        os.environ["SHOPASSIST_CA_API_KEY"] = sentinel
        try:
            left = SessionSandbox.start(seed=1, faults=[{"tool": "catalog.search", "on_occurrence": 1, "fault": "timeout"}])
            right = SessionSandbox.start(seed=2)
            try:
                self.assertNotEqual(left.pid(), right.pid())
                self.assertNotEqual(left.root, right.root)
                failed = left.invoke("product_search", {"filters": {"category": "drinkware", "in_stock": True, "price_op": "lt", "max_price": 15, "color": "sand"}})
                ok = right.invoke("product_search", {"filters": {"category": "drinkware", "in_stock": True, "price_op": "lt", "max_price": 15, "color": "sand"}})
                self.assertEqual(failed["status"], "timeout")
                self.assertEqual(ok["status"], "ok")
                marker = left.workspace / "marker.txt"
                marker.write_text("temp", encoding="utf-8")
                left.reset()
                self.assertFalse(marker.exists())
                environ = Path(f"/proc/{left.pid()}/environ").read_bytes()
                self.assertNotIn(sentinel.encode(), environ)
            finally:
                left.close()
                right.close()
        finally:
            if previous is None:
                os.environ.pop("SHOPASSIST_CA_API_KEY", None)
            else:
                os.environ["SHOPASSIST_CA_API_KEY"] = previous


class ConfigAndRedactionTest(unittest.TestCase):
    def test_mock_eval_does_not_open_sockets_or_leak(self):
        import socket

        from shopassist.cards import all_cards
        from shopassist.harness import evaluate_card

        sentinel = "unit-test-sentinel-value"
        previous = os.environ.get("SHOPASSIST_CA_API_KEY")
        os.environ["SHOPASSIST_CA_API_KEY"] = sentinel
        original = socket.create_connection
        socket.create_connection = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network"))
        try:
            result = evaluate_card(next(card for card in all_cards() if card["card_id"] == "policy-returns"))
            blob = json.dumps(result)
            self.assertNotIn(sentinel, blob)
            self.assertEqual(result["score"]["passed"], True)
            self.assertEqual(result["trace"]["redaction_count"] if "redaction_count" in result["trace"] else 0, 0)
        finally:
            socket.create_connection = original
            if previous is None:
                os.environ.pop("SHOPASSIST_CA_API_KEY", None)
            else:
                os.environ["SHOPASSIST_CA_API_KEY"] = previous

    def test_configured_mode_missing_is_not_a_mock_pass(self):
        from shopassist.config import ConfigError, load_config, require_configured_ready

        previous = {key: os.environ.get(key) for key in ("SHOPASSIST_MODE", "SHOPASSIST_OPT_IN_CONFIGURED_MODEL", "SHOPASSIST_CA_BASE_URL", "SHOPASSIST_CA_MODEL", "SHOPASSIST_CA_API_KEY", "SHOPASSIST_PA_BASE_URL", "SHOPASSIST_PA_MODEL", "SHOPASSIST_PA_API_KEY")}
        os.environ["SHOPASSIST_MODE"] = "configured_endpoint"
        os.environ["SHOPASSIST_OPT_IN_CONFIGURED_MODEL"] = "yes"
        os.environ["SHOPASSIST_CA_BASE_URL"] = "https://models.invalid/v1"
        os.environ["SHOPASSIST_CA_MODEL"] = "tiny-generic"
        os.environ.pop("SHOPASSIST_CA_API_KEY", None)
        os.environ["SHOPASSIST_PA_BASE_URL"] = "https://models.invalid/v1"
        os.environ["SHOPASSIST_PA_MODEL"] = "tiny-generic"
        os.environ["SHOPASSIST_PA_API_KEY"] = "unit-test-sentinel-value"
        try:
            with self.assertRaises(ConfigError) as caught:
                require_configured_ready(load_config())
            public = caught.exception.public()
            self.assertEqual(public["mock_fallback"], False)
            self.assertNotIn("unit-test-sentinel-value", json.dumps(public))
            self.assertIn("SHOPASSIST_CA_API_KEY", json.dumps(public))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_adapter_redacts_transport_errors(self):
        from shopassist.config import ConfigError
        from shopassist.model_client import ChatCompletionsClient

        sentinel = "unit-test-sentinel-value"

        def transport(url, headers, body, timeout):
            del url, body, timeout
            self.assertTrue(headers["Authorization"].endswith(sentinel))
            raise ConnectionError("HTTP 401 Bearer " + sentinel)

        client = ChatCompletionsClient(base_url="http://127.0.0.1:9/v1", model="tiny-generic", api_key=sentinel, transport=transport)
        with self.assertRaises(ConfigError) as caught:
            client.complete([{"role": "user", "content": "hello"}])
        self.assertNotIn(sentinel, json.dumps(caught.exception.public()))
        self.assertNotIn(sentinel, repr(client))

    def test_credentials_in_url_rejected(self):
        from shopassist.config import ConfigError, validate_role

        with self.assertRaises(ConfigError):
            validate_role("ca", {"base_url": "http://user:unit-test-sentinel-value@127.0.0.1/v1", "model": "tiny-generic", "api_key": "", "api_key_env": "SHOPASSIST_CA_API_KEY"})


class TrainingAndGatesTest(unittest.TestCase):
    def test_dry_run_does_not_claim_gpu(self):
        from shopassist.training import load_training_config, preference_training_status, run_preference_training, run_sft

        smoke = load_training_config(ROOT / "configs" / "sft_ca_smoke.json")
        larger = load_training_config(ROOT / "configs" / "sft_pa_larger.json")
        self.assertLess(smoke["lora"]["r"], larger["lora"]["r"])
        manifest = run_sft(ROOT / "configs" / "sft_ca_smoke.json", execute=False, corpus_dir=ROOT / "missing-corpus")
        self.assertEqual(manifest["status"], "dry_run_only")
        self.assertEqual(manifest["optimizer_steps_executed"], 0)
        self.assertFalse(manifest["gpu_training_verified"])
        missing = run_sft(ROOT / "configs" / "sft_pa_smoke.json", execute=True, corpus_dir=ROOT / "missing-corpus")
        self.assertEqual(missing["status"], "not_run_model_unavailable")
        self.assertFalse(missing["gpu_training_verified"])
        self.assertEqual(preference_training_status()["implemented"], False)
        with self.assertRaises(NotImplementedError):
            run_preference_training()

    def test_bad_candidate_fails_gates_and_missing_baseline_blocks(self):
        from shopassist.eval.compare import aggregate, apply_gates, compare_reports, degrade_report

        gates = json.loads((ROOT / "configs" / "gates_demo.json").read_text(encoding="utf-8"))
        report = {
            "evaluation_kind": "mock_demo",
            "identifiers": {"mode": "mock"},
            "cases": [
                {"case_id": "a", "scenario_id": "demo", "passed": True, "unsupported_claim_count": 0, "hard_constraint_violations": 0, "unnecessary_planner": False, "schema_ok": True}
            ],
        }
        blocked = compare_reports(None, report, gates)
        self.assertEqual(blocked["status"], "blocked_missing_baseline")
        self.assertFalse(blocked["passed"])
        bad = degrade_report(report)
        compared = compare_reports(report, bad, gates)
        self.assertFalse(compared["passed"])
        self.assertTrue(compared["regressions"])
        metrics = aggregate(bad["cases"])
        forced = json.loads(json.dumps(gates))
        forced["gates"] = [dict(gates["gates"][-1], enabled=True)]
        judge_gate = apply_gates({"judge_score": None}, forced["gates"])
        self.assertEqual(judge_gate[0]["status"], "unavailable")
        self.assertFalse(judge_gate[0]["passed"])
        self.assertEqual(metrics["unsupported_claim_count"], 2)


class WebAndExtensionTest(unittest.TestCase):
    def test_ui_runs_case_and_escapes_text(self):
        import urllib.request

        from shopassist.web.server import make_server

        server = make_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode("utf-8")
            self.assertIn("Shopping assistant lab", page)
            script = urllib.request.urlopen(f"http://127.0.0.1:{port}/static/app.js").read().decode("utf-8")
            self.assertNotIn("innerHTML", script)
            self.assertNotIn("localStorage", script)
            import socket
            probe = socket.create_connection(("127.0.0.1", port))
            probe.sendall(b"GET /static/../server.py HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            body = probe.recv(8192)
            probe.close()
            self.assertNotIn(b"def make_server", body)
            scenarios = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/scenarios"))
            self.assertGreater(len(scenarios["scenarios"]), 10)
            self.assertNotIn("expected", json.dumps(scenarios))
            created = json.load(urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port}/api/sessions",
                data=json.dumps({"mode": "mock"}).encode(),
                headers={"Content-Type": "application/json"},
            )))
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/sessions/{created['session_id']}/run",
                data=json.dumps({"card_id": "policy-returns"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            payload = json.load(urllib.request.urlopen(request))
            self.assertTrue(payload["passed"])
            self.assertIn("30", payload["turns"][0]["reply_text"])
            self.assertTrue(any(tool["tool"] == "kb.get_policy" for tool in payload["turns"][0]["tools"]))
            self.assertEqual(payload["evaluation_kind"], "mock_demo")
            message = json.load(urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port}/api/sessions/{created['session_id']}/message",
                data=json.dumps({"text": "<script>alert(1)</script> Thanks, that is all I needed."}).encode(),
                headers={"Content-Type": "application/json"},
            )))
            self.assertIn("<script>", json.dumps(message))
            bad = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/sessions",
                data=json.dumps({"mode": "configured_endpoint"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(bad)
                self.fail("configured mode should not succeed")
            except urllib.error.HTTPError as exc:
                error = json.loads(exc.read().decode())
                self.assertFalse(error.get("mock_fallback", False))
                self.assertNotIn("unit-test-sentinel-value", json.dumps(error))
        finally:
            server.shutdown()
            server.server_close()
            server.app.close_all()

    def test_added_skill_and_catalog_item(self):
        from shopassist.catalog import Catalog
        from shopassist.sandbox import SessionSandbox
        from shopassist.session_runner import _run_planner
        from shopassist.skills.registry import SkillSpec
        from shopassist.planner_policy import PlanRegistry, _step

        box = SessionSandbox.start(seed=9)
        try:
            def handle(arguments, call):
                raw = call("catalog.search", {"filters": arguments["filters"], "limit": 10})
                return {"status": raw["data"]["status"], "items": raw["data"].get("items") or [], "facts": [], "evidence": [raw["evidence"]], "count": len(raw["data"].get("items") or [])}

            box.registry.register(SkillSpec("skill.item_count", "1.0.0", "count", ("catalog.search",), handle, ("filters",)))
            counted = box.invoke("skill.item_count", {"filters": {"category": "stationery", "in_stock": True, "price_op": "lt", "max_price": 100}})
            self.assertEqual(counted["count"], 1)
            registry = PlanRegistry()
            registry.register("count_items", lambda task: [_step("c1", "product_search", [], {"filters": task["query"], "backend": "primary", "limit": 5})])
            from shopassist.loop_control import Budget
            from shopassist.planner_policy import act
            from shopassist.settings import DEFAULTS

            outcome, _state, _steps = _run_planner(
                {"task_id": "t-ext", "goal_type": "count_items", "query": {"category": "stationery", "max_price": 100, "price_op": "lt"}, "confirmed_constraints": [], "assumptions": [], "missing": []},
                box,
                registry,
                decide=lambda task, state: act(task, state, registry),
                budget=Budget(DEFAULTS["loop"]),
                limits={"max_retries_per_step": 1, "max_replans": 1, "max_fallbacks": 1},
            )
            self.assertEqual(outcome["action"], "finish")
            self.assertEqual(outcome["terminal"]["status"], "COMPLETED")
        finally:
            box.close()
        extra = Catalog("synthetic-catalog-extra", [{
            "product_id": "prod-extra-cup",
            "sku": "SKU-EXTRA-CUP",
            "name": "Plain cup",
            "currency": "USD",
            "static_attributes": {"category": "drinkware", "color": "sand", "size": "200ml", "material": "ceramic", "warranty_months": 1},
            "dynamic": {"price": 4, "stock": 3, "freshness": "current", "as_of": "synthetic-catalog-extra"},
        }])
        found = extra.search({"category": "drinkware", "max_price": 5, "price_op": "lt", "in_stock": True})
        self.assertEqual(found["items"][0]["product_id"], "prod-extra-cup")


class HygieneTest(unittest.TestCase):
    def test_tracked_files_have_no_sample_credentials(self):
        import re

        patterns = [
            re.compile(r"sk-[A-Za-z0-9]{8,}"),
            re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
            re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
            re.compile(r"Bearer\s+[A-Za-z0-9_\-]{12,}"),
            re.compile(r"\"api_key\"\s*:\s*\"[^\"]+\""),
        ]
        skip = {".git", "__pycache__", "reports", "artifacts", ".venv"}
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in skip for part in path.parts):
                continue
            if path.suffix.lower() in {".png", ".jpg", ".pyc"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in patterns:
                if pattern.search(text):
                    offenders.append(str(path.relative_to(ROOT)))
                    break
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
