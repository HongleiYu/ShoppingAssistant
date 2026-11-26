import json
import random
import unittest

from shopassist.evidence_gate import gate_turn, guard_items, guard_text
from shopassist.loop_control import FakeClock
from shopassist.model_client import ChatCompletionsClient
from shopassist.progress import iter_turn_events, parse_sse
from shopassist.recovery import TransientEndpointError, choose, wait_bounded
from shopassist.sandbox import SessionSandbox
from shopassist.session_runner import AgentSession
from shopassist.settings import load_settings


class EvidenceGateTest(unittest.TestCase):
    def test_invented_logistics_is_not_treated_as_no_shipment(self):
        turn = {
            "reply_text": "Your package is delivered.",
            "claims": [{"field": "logistics_status", "value": "delivered", "evidence_ids": []}],
            "limitations": [],
            "evidence": [],
        }
        gated = gate_turn(turn)
        self.assertNotIn("delivered", gated["reply_text"].lower())
        self.assertIn("unavailable", gated["reply_text"].lower())
        self.assertIn("logistics_not_queried", {item["code"] for item in gated["limitations"]})
        self.assertNotIn("logistics_not_in_snapshot", {item["code"] for item in gated["limitations"]})

    def test_successful_empty_is_distinct_from_tool_failure(self):
        box = SessionSandbox.start(seed=1)
        try:
            session = AgentSession(box, "ship", settings=load_settings(environ={}))
            empty = session.user_message("Where is tracking TRK-EMPTY?")
            failed = session.user_message("Where is tracking TRK-OK?")
        finally:
            box.close()
        self.assertIn("no record", empty["reply_text"].lower())
        self.assertNotIn("unavailable", empty["reply_text"].lower())
        self.assertEqual(failed["terminal"]["status"], "COMPLETED")
        self.assertIn("in_transit", failed["reply_text"])

    def test_stale_dynamic_is_not_presented_as_live(self):
        items, limitations = guard_items(
            [{"product_id": "prod-lamp", "price": 40, "stock": 2, "freshness": "stale", "price_supported": False}]
        )
        self.assertNotIn("price", items[0])
        self.assertIn("dynamic_unverified", {item["code"] for item in limitations})

    def test_supported_price_sentence_stays(self):
        text = guard_text(
            "Plain mug (prod-mug-sand) is 12 USD with stock 4.",
            [{"field": "price", "value": 12, "product_id": "prod-mug-sand"}],
            [],
        )
        self.assertIn("12", text)


class RecoveryPolicyTest(unittest.TestCase):
    def test_simple_and_planner_share_classification(self):
        limits = {"max_retries_per_step": 1, "max_fallbacks": 1, "max_replans": 1}
        simple = choose(status="timeout", skill_id="product_quote", attempt=0, limits=limits)
        planner = choose(status="timeout", skill_id="product_search", attempt=0, limits=limits, goal_type="search")
        self.assertEqual(simple["action"], planner["action"])
        self.assertEqual(choose(status=None, http_status=401, limits=limits)["action"], "stop")
        self.assertEqual(choose(status=None, http_status=400, limits=limits)["reason"], "invalid_request")
        self.assertEqual(choose(status="malformed", limits=limits)["reason"], "malformed_evidence")
        self.assertEqual(
            choose(status="error", skill_id="product_search", fallback_count=0, limits=limits)["action"],
            "cached_fallback",
        )
        self.assertEqual(choose(status=None, http_status=503, attempt=0, limits=limits)["action"], "retry")
        self.assertEqual(choose(status=None, http_status=503, attempt=1, limits=limits)["action"], "stop")
        self.assertEqual(choose(status="timeout", attempt=0, idempotent=False, limits=limits)["reason"], "side_effect_not_retried")
        self.assertEqual(choose(status="timeout", attempt=0, partial_streamed=True, limits=limits)["reason"], "partial_output")

    def test_retry_after_then_success_and_budget_and_cancel(self):
        calls = {"n": 0}

        def transport(url, headers, body, timeout):
            del url, headers, timeout
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransientEndpointError(429, retry_after=0.01)
            content = json.dumps(
                {
                    "route": "direct_answer",
                    "route_reason_code": "configured_model",
                    "reply_text": "Hello there",
                    "must_not_invoke_planner": True,
                }
            )
            return json.dumps({"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 4}})

        clock = FakeClock(0)
        settings = load_settings(overrides={"recovery": {"backoff_base_seconds": 0.05}}, environ={})
        box = SessionSandbox.start(seed=1)
        try:
            client = ChatCompletionsClient(base_url="http://127.0.0.1:9/v1", model="ca", transport=transport)
            session = AgentSession(
                box,
                "retry",
                mode="configured_endpoint",
                model_clients={"ca": client, "pa": client},
                settings=settings,
                clock=clock,
            )
            session.sleeper = lambda seconds, clock_: clock_.advance(seconds)
            turn = session.user_message("What is the weather like today?")
            self.assertEqual(calls["n"], 2)
            self.assertIn("Hello", turn["reply_text"])
            self.assertGreater(session.budget.session_model_calls, 0)
        finally:
            box.close()

        clock = FakeClock(0)
        settings = load_settings(overrides={"loop": {"wall_clock_seconds": 0.2}}, environ={})
        box = SessionSandbox.start(seed=1)
        try:
            client = ChatCompletionsClient(
                base_url="http://127.0.0.1:9/v1",
                model="ca",
                transport=lambda *args: (_ for _ in ()).throw(TransientEndpointError(503, retry_after=5)),
            )
            session = AgentSession(box, "late", mode="configured_endpoint", model_clients={"ca": client, "pa": client}, settings=settings, clock=clock)
            session.sleeper = lambda seconds, clock_: clock_.advance(seconds)
            stopped = session.user_message("What is the weather like today?")
            self.assertEqual(stopped["terminal"]["reason_code"], "wall_clock")
        finally:
            box.close()

    def test_cancel_during_wait_and_auth_has_no_second_call(self):
        clock = FakeClock(0)
        from shopassist.loop_control import Budget
        from shopassist.settings import DEFAULTS

        budget = Budget(DEFAULTS["loop"], clock=clock)
        budget.begin_request()
        cancel = __import__("threading").Event()

        def sleeper(seconds, clock_):
            cancel.set()
            clock_.advance(seconds)

        self.assertEqual(wait_bounded(0.01, budget, cancel, sleeper), "cancelled")
        calls = {"n": 0}

        def transport(url, headers, body, timeout):
            del url, headers, body, timeout
            calls["n"] += 1
            from shopassist.recovery import PermanentEndpointError

            raise PermanentEndpointError(401)

        box = SessionSandbox.start(seed=1)
        try:
            client = ChatCompletionsClient(base_url="http://127.0.0.1:9/v1", model="ca", transport=transport)
            session = AgentSession(box, "auth", mode="configured_endpoint", model_clients={"ca": client, "pa": client}, settings=load_settings(environ={}), clock=clock)
            with self.assertRaises(Exception):
                session.user_message("What is the weather like today?")
            self.assertEqual(calls["n"], 1)
        finally:
            box.close()

    def test_later_turn_is_not_blocked_by_session_age(self):
        clock = FakeClock(0)
        box = SessionSandbox.start(seed=3)
        try:
            session = AgentSession(box, "aged", settings=load_settings(environ={}), clock=clock)
            session.sleeper = lambda seconds, clock_: clock_.advance(seconds)
            first = session.user_message("Thanks, that is all I needed.")
            self.assertEqual(first["terminal"]["status"], "COMPLETED")
            clock.advance(31)
            second = session.user_message("Where is tracking TRK-OK?")
            self.assertEqual(second["terminal"]["status"], "COMPLETED")
            clock.advance(31)
            third = session.user_message("Where is tracking TRK-OK?")
            self.assertEqual(third["terminal"]["status"], "COMPLETED")
            self.assertEqual(session.budget.tool_calls, 1)
            self.assertGreaterEqual(session.budget.session_tool_calls, 2)
        finally:
            box.close()

    def test_stream_does_not_emit_blocked_logistics_before_done(self):
        def transport(url, headers, body, timeout):
            del url, headers, timeout
            content = json.dumps(
                {
                    "route": "direct_answer",
                    "route_reason_code": "configured_model",
                    "reply_text": "Your package is delivered.",
                    "must_not_invoke_planner": True,
                }
            )
            return json.dumps({"choices": [{"message": {"content": content}}]})

        box = SessionSandbox.start(seed=1)
        try:
            client = ChatCompletionsClient(base_url="http://127.0.0.1:9/v1", model="ca", transport=transport)
            session = AgentSession(
                box,
                "stream",
                mode="configured_endpoint",
                model_clients={"ca": client, "pa": client},
                settings=load_settings(overrides={"model": {"stream_final": False}}, environ={}),
            )
            blob = "".join(iter_turn_events(session, "Hello", session.settings, request_id="req"))
        finally:
            box.close()
        events = parse_sse(blob)
        progress = [item["data"].get("text", "") for item in events if item["event"] == "progress"]
        self.assertFalse(any("delivered" in text.lower() for text in progress))
        rendered = " ".join(item["data"].get("text", "") for item in events if item["event"] == "text_delta")
        self.assertNotIn("delivered", rendered.lower())
        self.assertEqual([item["event"] for item in events if item["event"] in {"done", "error"}], ["done"])


if __name__ == "__main__":
    unittest.main()
