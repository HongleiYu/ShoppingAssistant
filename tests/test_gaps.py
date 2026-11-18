"""Configured-model path, local optimizer smoke, and MCP SDK interop."""

from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "unit-test-sentinel-value"


class StubEndpoint:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.invalid = False

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
                with outer.lock:
                    outer.calls.append(
                        {
                            "path": self.path,
                            "model": body.get("model"),
                            "authorization": self.headers.get("Authorization"),
                            "messages": body.get("messages"),
                        }
                    )
                if outer.invalid:
                    content = "NOPE"
                else:
                    content = json.dumps(_answer(body), sort_keys=True)
                payload = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _answer(body: dict) -> dict:
    blob = json.dumps(body.get("messages") or [])
    model = str(body.get("model") or "")
    if model.startswith("pa"):
        payload = json.loads(body["messages"][-1]["content"])
        observations = payload.get("observations") or []
        query = (payload.get("task") or {}).get("query") or {}
        filters = {"in_stock": True, "price_op": query.get("price_op") or "lt"}
        for key in ("category", "color", "size", "max_price"):
            if query.get(key) is not None:
                filters[key] = query[key]
        if not observations:
            return {
                "action": "plan",
                "plan": [
                    {
                        "step_id": "s1",
                        "depends_on": [],
                        "kind": "skill",
                        "skill_id": "product_search",
                        "arguments": {"filters": filters, "backend": "primary", "limit": 10},
                        "intent": "product_search",
                    },
                    {
                        "step_id": "s2",
                        "depends_on": ["s1"],
                        "kind": "skill",
                        "skill_id": "budget_attribute_validate",
                        "arguments": {"use_previous_items": True},
                        "intent": "budget_attribute_validate",
                    },
                ],
                "skill_call": {
                    "step_id": "s1",
                    "skill_id": "product_search",
                    "arguments": {"filters": filters, "backend": "primary", "limit": 10},
                },
                "recovery": None,
                "failure": None,
            }
        if not any(item.get("skill_id") == "budget_attribute_validate" for item in observations):
            return {
                "action": "tool_call",
                "plan": None,
                "skill_call": {"step_id": "s2", "skill_id": "budget_attribute_validate", "arguments": {}},
                "recovery": None,
                "failure": None,
            }
        return {"action": "finish", "plan": None, "skill_call": None, "recovery": None, "failure": None}
    if "return window" in blob:
        return {
            "route": "simple_skill",
            "route_reason_code": "policy_lookup",
            "skill_id": "policy_lookup",
            "skill_args": {"topic": "returns", "include_stale": False},
            "must_not_invoke_planner": True,
        }
    if "Find a drinkware item in sand" in blob:
        return {
            "route": "complex_handoff",
            "route_reason_code": "needs_plan",
            "goal_type": "search",
            "must_not_invoke_planner": False,
            "query": {"category": "drinkware", "color": "sand", "max_price": 15, "price_op": "lt"},
            "confirmed_constraints": [
                {"field": "category", "value": "drinkware", "status": "confirmed"},
                {"field": "color", "value": "sand", "status": "confirmed"},
                {"field": "budget", "value": 15, "status": "confirmed"},
            ],
        }
    return {
        "route": "direct_answer",
        "route_reason_code": "configured_model",
        "reply_text": "Noted.",
        "must_not_invoke_planner": True,
    }


class ConfiguredModeTest(unittest.TestCase):
    def setUp(self):
        self.stub = StubEndpoint()
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "SHOPASSIST_MODE",
                "SHOPASSIST_OPT_IN_CONFIGURED_MODEL",
                "SHOPASSIST_CA_BASE_URL",
                "SHOPASSIST_CA_MODEL",
                "SHOPASSIST_CA_API_KEY",
                "SHOPASSIST_PA_BASE_URL",
                "SHOPASSIST_PA_MODEL",
                "SHOPASSIST_PA_API_KEY",
            )
        }
        os.environ["SHOPASSIST_MODE"] = "configured_endpoint"
        os.environ["SHOPASSIST_OPT_IN_CONFIGURED_MODEL"] = "yes"
        os.environ["SHOPASSIST_CA_BASE_URL"] = self.stub.base_url
        os.environ["SHOPASSIST_CA_MODEL"] = "ca-stub"
        os.environ["SHOPASSIST_CA_API_KEY"] = SENTINEL
        os.environ["SHOPASSIST_PA_BASE_URL"] = self.stub.base_url
        os.environ["SHOPASSIST_PA_MODEL"] = "pa-stub"
        os.environ["SHOPASSIST_PA_API_KEY"] = SENTINEL

    def tearDown(self):
        self.stub.close()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _clients(self):
        from shopassist.model_client import ChatCompletionsClient

        return {
            "ca": ChatCompletionsClient(base_url=self.stub.base_url, model="ca-stub", api_key=SENTINEL, timeout=5),
            "pa": ChatCompletionsClient(base_url=self.stub.base_url, model="pa-stub", api_key=SENTINEL, timeout=5),
        }

    def test_policy_and_search_use_adapter_not_mock_policy(self):
        import shopassist.communication as communication
        import shopassist.session_runner as runner
        from shopassist.cards import all_cards
        from shopassist.harness import evaluate_card

        cards = {card["card_id"]: card for card in all_cards()}
        original_decide = communication.decide_route
        original_act = runner.act

        def banned(*_args, **_kwargs):
            raise AssertionError("mock policy used")

        communication.decide_route = banned
        runner.act = banned
        try:
            clients = self._clients()
            policy = evaluate_card(cards["policy-returns"], mode="configured_endpoint", model_clients=clients)
            self.assertTrue(policy["score"]["passed"], policy["score"]["failures"])
            self.assertEqual(policy["trace"]["evaluation_kind"], "configured_model")
            self.assertGreater(policy["trace"]["model_call_count"], 0)
            self.assertTrue(any(call["model"] == "ca-stub" for call in self.stub.calls))
            self.assertFalse(any(call["model"] == "pa-stub" for call in self.stub.calls))
            self.assertNotIn(SENTINEL, json.dumps(policy["trace"]))
            search = evaluate_card(cards["search-sand"], mode="configured_endpoint", model_clients=clients)
            self.assertTrue(search["score"]["passed"], search["score"]["failures"])
            self.assertTrue(any(call["model"] == "pa-stub" for call in self.stub.calls))
            self.assertTrue(any(tool["tool"] == "catalog.search" for tool in search["trace"]["turns"][0]["tools"]))
            self.assertNotIn(SENTINEL, json.dumps(search["trace"]))
        finally:
            communication.decide_route = original_decide
            runner.act = original_act

    def test_chat_api_configured_mode_and_invalid_model_output(self):
        import urllib.error
        import urllib.request

        from shopassist.web.server import make_server

        server = make_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            created = json.load(urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port}/api/sessions",
                data=json.dumps({"mode": "configured_endpoint"}).encode(),
                headers={"Content-Type": "application/json"},
            )))
            self.assertEqual(created["evaluation_kind"], "configured_model")
            self.assertNotIn(SENTINEL, json.dumps(created))
            self.assertNotIn("base_url", json.dumps(created))
            ran = json.load(urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port}/api/sessions/{created['session_id']}/run",
                data=json.dumps({"card_id": "policy-returns"}).encode(),
                headers={"Content-Type": "application/json"},
            )))
            self.assertTrue(ran["passed"])
            self.assertEqual(ran["evaluation_kind"], "configured_model")
            self.assertIn("30", ran["turns"][0]["reply_text"])
            self.assertNotIn(SENTINEL, json.dumps(ran))
            self.assertTrue(any(call["authorization"] == "Bearer " + SENTINEL for call in self.stub.calls))
            self.stub.invalid = True
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/sessions/{created['session_id']}/message",
                    data=json.dumps({"text": "Hello."}).encode(),
                    headers={"Content-Type": "application/json"},
                ))
                self.fail("invalid model output should not succeed")
            except urllib.error.HTTPError as exc:
                error = json.loads(exc.read().decode())
                self.assertFalse(error.get("mock_fallback", True))
                self.assertNotIn(SENTINEL, json.dumps(error))
                self.assertNotIn("You are welcome", json.dumps(error))
        finally:
            server.shutdown()
            server.server_close()
            server.app.close_all()


class LocalTrainingSmokeTest(unittest.TestCase):
    def test_ca_and_pa_optimizer_steps_save_and_reload(self):
        from shopassist.training import predict_local, run_sft

        corpus = ROOT / "data" / "demo_corpus"
        dry = run_sft(ROOT / "configs" / "sft_ca_local_random.json", execute=False, corpus_dir=corpus)
        self.assertEqual(dry["optimizer_steps_executed"], 0)
        self.assertEqual(dry["status"], "dry_run_only")
        ca = run_sft(ROOT / "configs" / "sft_ca_local_random.json", execute=True, corpus_dir=corpus)
        pa = run_sft(ROOT / "configs" / "sft_pa_local_random.json", execute=True, corpus_dir=corpus)
        self.assertEqual(ca["status"], "optimizer_steps_finished")
        self.assertEqual(pa["status"], "optimizer_steps_finished")
        self.assertEqual(ca["optimizer_steps_executed"], 2)
        self.assertEqual(pa["optimizer_steps_executed"], 2)
        self.assertTrue(ca["weight_delta_nonzero"])
        self.assertTrue(pa["weight_delta_nonzero"])
        self.assertTrue(ca["reloaded"])
        self.assertTrue(pa["reloaded"])
        self.assertFalse(ca["gpu_training_verified"])
        self.assertFalse(ca["quality_validation"])
        ca_adapter = ROOT / "artifacts" / "adapters" / "ca-random"
        pa_adapter = ROOT / "artifacts" / "adapters" / "pa-random"
        self.assertTrue((ca_adapter / "adapter.json").is_file())
        self.assertTrue((pa_adapter / "adapter.json").is_file())
        self.assertNotEqual(
            (ca_adapter / "adapter.json").read_text(encoding="utf-8"),
            (pa_adapter / "adapter.json").read_text(encoding="utf-8"),
        )
        ca_saved = predict_local(ca_adapter, "same-text")
        again = run_sft(ROOT / "configs" / "sft_pa_local_random.json", execute=True, corpus_dir=corpus)
        self.assertEqual(again["role"], "pa")
        self.assertAlmostEqual(predict_local(ca_adapter, "same-text"), ca_saved, places=6)
        hf = run_sft(ROOT / "configs" / "sft_ca_smoke.json", execute=True, corpus_dir=corpus)
        self.assertEqual(hf["status"], "not_run_model_unavailable")
        self.assertEqual(hf["optimizer_steps_executed"], 0)
        self.assertFalse(hf["gpu_training_verified"])


class McpSdkInteropTest(unittest.TestCase):
    def test_official_client_calls_server(self):
        import asyncio
        from tempfile import mkdtemp

        import mcp
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters

        from shopassist.catalog import export_catalog
        from shopassist.knowledge import export_kb

        root = Path(mkdtemp(prefix="mcp-sdk-"))
        export_catalog(root / "catalog.json")
        export_kb(root / "kb.json")
        (root / "profile.json").write_text(json.dumps({"seed": 1, "faults": [], "max_calls": 8}), encoding="utf-8")
        (root / "workspace").mkdir()
        params = StdioServerParameters(
            command="python3",
            args=[
                "-m",
                "shopassist.mcp.server",
                "--catalog",
                str(root / "catalog.json"),
                "--kb",
                str(root / "kb.json"),
                "--profile",
                str(root / "profile.json"),
                "--workspace",
                str(root / "workspace"),
            ],
            cwd=str(ROOT),
            env={
                "PYTHONPATH": str(ROOT),
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )

        async def run():
            async with mcp.stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    names = {tool.name for tool in listed.tools}
                    self.assertIn("catalog.quote", names)
                    self.assertIn("kb.get_policy", names)
                    result = await session.call_tool("catalog.quote", {"product_id": "prod-mug-sand", "fresh": False})
                    self.assertFalse(result.isError)
                    payload = result.structuredContent
                    self.assertEqual(payload["price"], 12)
                    self.assertEqual(payload["status"], "ok")

        asyncio.run(run())
