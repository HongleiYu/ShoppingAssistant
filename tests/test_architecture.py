"""FAQ front layer, retrieval limits, and one-shot planner DAG."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shopassist.embeddings import HashingEncoder
from shopassist.faq_rules import match_faq
from shopassist.harness import evaluate_card
from shopassist.pages_demo import DemoApp
from shopassist.plan_dag import validate_dag
from shopassist.planner_policy import PlanRegistry, act
from shopassist.retrieval import expand_query
from shopassist.sandbox import SessionSandbox
from shopassist.session_runner import AgentSession, _run_planner
from shopassist.settings import load_settings
from shopassist.vector_index import build_index, open_index


def _policy(doc_id: str, text: str) -> dict:
    return {
        "doc_id": doc_id,
        "title": text,
        "topic": "demo",
        "freshness": "current",
        "sections": [{"section_id": "s1", "heading": text, "text": text, "facts": {}}],
    }


def _product(product_id: str, name: str) -> dict:
    return {
        "product_id": product_id,
        "name": name,
        "description": name,
        "static_attributes": {"category": "home", "color": "sand", "material": "cotton"},
        "dynamic": {"price": 10, "stock": 2, "freshness": "current"},
    }


class FaqFrontLayerTest(unittest.TestCase):
    def test_exact_faq_skips_both_agents(self):
        app = DemoApp()
        try:
            app.consult("prod-shoes-court")
            for text in ("What can you help with?", "你能帮什么忙？"):
                turn = app.turn(text)
                self.assertEqual(turn["route_reason_code"], "faq_rule")
                self.assertEqual(turn["model_invocations"], [])
                self.assertEqual(turn["rule"]["kind"], "faq")
            returns = app.turn("What is the return window?")
            self.assertEqual(returns["route"], "simple_skill")
            self.assertEqual(returns["route_reason_code"], "faq_rule")
            self.assertEqual(returns["model_invocations"], [])
            self.assertTrue(any(tool["tool"] == "kb.get_policy" for tool in returns["tools"]))
            self.assertIsNone(match_faq("Hello"))
            self.assertIsNone(match_faq("How much is this item?"))
        finally:
            app.close()

    def test_price_stays_on_the_communication_agent(self):
        app = DemoApp()
        try:
            app.consult("prod-shoes-court")
            turn = app.turn("How much is this item?")
            agents = [item["agent"] for item in turn["model_invocations"]]
            self.assertEqual(agents, ["ca", "ca"])
            self.assertEqual(turn["cards"][0]["fields"]["price"], 48)
            self.assertNotIn("faq_rule", turn["route_reason_code"])
        finally:
            app.close()


class RetrievalLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rag-arch-"))
        self.encoder = HashingEncoder()
        policies = [
            _policy("pol-lamp", "lamp care is a global policy"),
            _policy("pol-copy-a", "same duplicate care sentence"),
            _policy("pol-copy-b", "same duplicate care sentence"),
            _policy("pol-other", "unrelated quartz note"),
        ]
        products = [
            _product("prod-alpha", "alpha lamp shade"),
            _product("prod-beta", "beta lamp shade"),
        ]
        self.index_dir = self.tmp / "idx"
        build_index(policies, products, self.encoder, self.index_dir)
        self.index = open_index(self.index_dir)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_expansion_keeps_the_original_query(self):
        rewritten = expand_query("refund this mug")
        self.assertEqual(rewritten["original"], "refund this mug")
        self.assertIn("return", rewritten["added_tokens"])
        self.assertIn("drinkware", rewritten["added_tokens"])
        self.assertFalse(rewritten["semantic"])
        found = self.index.search(self.encoder, "refund this mug", corpus="policy", top_k=5, threshold=0.0)
        self.assertEqual(found["query"], "refund this mug")
        self.assertEqual(found["query_rewrite"]["method"], "lexical_alias")
        self.assertFalse(found["semantic"])
        self.assertEqual(found["score_kind"], "hashing_cosine")

    def test_product_filter_runs_before_ranking(self):
        hits = self.index.search(
            self.encoder,
            "lamp",
            corpus="product",
            top_k=5,
            threshold=0.0,
            product_id="prod-alpha",
        )
        self.assertTrue(hits["hits"])
        self.assertTrue(all(hit["product_id"] == "prod-alpha" for hit in hits["hits"]))
        self.assertGreater(hits["filtered_other_products"], 0)
        mixed = self.index.search(self.encoder, "lamp", top_k=5, threshold=0.0, product_id="prod-alpha")
        product_ids = {hit["product_id"] for hit in mixed["hits"] if hit["corpus"] == "product"}
        self.assertEqual(product_ids, {"prod-alpha"})
        self.assertTrue(any(hit["corpus"] == "policy" and hit["scope"] == "global" for hit in mixed["hits"]))

    def test_duplicates_threshold_and_cap(self):
        dupes = self.index.search(self.encoder, "same duplicate care sentence", corpus="policy", top_k=5, threshold=0.0)
        copy_ids = [hit["chunk_id"] for hit in dupes["hits"] if hit["chunk_id"].startswith("pol-copy")]
        self.assertEqual(copy_ids, ["pol-copy-a#s1"])
        self.assertGreaterEqual(dupes["dedup_dropped"], 1)
        self.assertEqual(dupes["dedup_kind"], "lexical_near_duplicate")
        dropped = self.index.search(self.encoder, "lamp care is a global policy", corpus="policy", top_k=5, threshold=1.01)
        self.assertEqual(dropped["hits"], [])
        self.assertEqual(dropped["status"], "empty")
        one = self.index.search(self.encoder, "unrelated quartz note", corpus="policy", top_k=5, threshold=0.0, max_hits=3)
        self.assertEqual(one["returned"], 1)
        capped = self.index.search(self.encoder, "lamp", top_k=5, threshold=0.0, max_hits=2)
        self.assertLessEqual(capped["returned"], 2)
        self.assertGreater(len(self.index.search(self.encoder, "lamp", top_k=5, threshold=0.0)["hits"]), capped["returned"])


class OneShotPlannerTest(unittest.TestCase):
    def test_complex_turn_calls_the_planner_once(self):
        app = DemoApp()
        try:
            app.consult("prod-mug-sand")
            for text in (
                "browse drinkware under 20",
                "find drinkware under 20",
                "compare Matte ceramic mug 350 ml and Blue stoneware mug 350 ml",
            ):
                turn = app.turn(text)
                pa = [item for item in turn["model_invocations"] if item["agent"] == "pa"]
                ca = [item for item in turn["model_invocations"] if item["agent"] == "ca"]
                self.assertEqual(len(pa), 1, text)
                self.assertEqual(pa[0]["phase"], "plan")
                self.assertEqual(len(ca), 2, text)
                self.assertEqual(turn["terminal"]["status"], "COMPLETED")
            compared = app.turn("compare Matte ceramic mug 350 ml and Blue stoneware mug 350 ml")
            self.assertEqual(compared["dag_waves"][0], ["q1", "q2"])
        finally:
            app.close()

    def test_invalid_plan_does_not_call_the_planner_again(self):
        calls = {"n": 0}

        def bad(task, state, registry):
            del task, state, registry
            calls["n"] += 1
            return {"action": "tool_call", "plan": None, "skill_call": {"step_id": "s1", "skill_id": "product_search", "arguments": {}}, "failure": None, "recovery": None}

        box = SessionSandbox.start(seed=1)
        try:
            session = AgentSession(box, "bad-plan", settings=load_settings(defaults_only=True), decider=bad)
            turn = session.user_message("find drinkware under 20")
            self.assertEqual(calls["n"], 1)
            self.assertEqual(turn["terminal"]["reason_code"], "invalid_plan")
        finally:
            box.close()

    def test_dag_validation_rejects_cycles_and_unknown_skills(self):
        self.assertIn("cycle", validate_dag([
            {"step_id": "a", "skill_id": "product_search", "depends_on": ["b"], "arguments": {}},
            {"step_id": "b", "skill_id": "product_search", "depends_on": ["a"], "arguments": {}},
        ], max_nodes=8))
        self.assertIn("skill_not_allowlisted", validate_dag([
            {"step_id": "a", "skill_id": "drop_database", "depends_on": [], "arguments": {}},
        ], max_nodes=8))
        self.assertIn("missing_dependency", validate_dag([
            {"step_id": "a", "skill_id": "product_search", "depends_on": ["missing"], "arguments": {}},
        ], max_nodes=8))
        self.assertEqual(validate_dag([
            {"step_id": "a", "skill_id": "product_search", "depends_on": [], "arguments": {"filters": {}}},
        ], max_nodes=8), [])

    def test_timeout_retries_once_without_a_second_planner_call(self):
        from shopassist.cards import all_cards

        card = next(item for item in all_cards() if item["card_id"] == "timeout-search")
        result = evaluate_card(card)
        self.assertTrue(result["score"]["passed"], result["score"]["failures"])
        turn = result["trace"]["turns"][0]
        pa = [item for item in turn.get("model_invocations") or [] if item["agent"] == "pa"]
        self.assertEqual(len(pa), 1)
        self.assertIn("retry", [item.get("strategy") for item in turn["recovery"]])
        statuses = [tool.get("status") for tool in turn["tools"]]
        self.assertIn("timeout", statuses)
        self.assertIn("ok", statuses)

    def test_extension_goal_still_finishes_from_one_plan(self):
        from shopassist.loop_control import Budget
        from shopassist.settings import DEFAULTS

        box = SessionSandbox.start(seed=9)
        try:
            registry = PlanRegistry()
            registry.register("count_items", lambda task: [{"step_id": "c1", "skill_id": "product_search", "depends_on": [], "arguments": {"filters": {"category": "stationery", "in_stock": True, "price_op": "lt", "max_price": 100}, "backend": "primary", "limit": 5}}])
            outcome, _state, _steps = _run_planner(
                {"task_id": "t-ext", "goal_type": "count_items", "query": {"category": "stationery", "max_price": 100, "price_op": "lt"}, "confirmed_constraints": [], "assumptions": [], "missing": []},
                box,
                registry,
                decide=lambda task, state: act(task, state, registry),
                budget=Budget(DEFAULTS["loop"]),
                limits={"max_retries_per_step": 1, "max_replans": 1, "max_fallbacks": 1, "max_plan_steps": 8},
            )
            self.assertEqual(outcome["action"], "finish")
            self.assertEqual(outcome["terminal"]["status"], "COMPLETED")
        finally:
            box.close()


if __name__ == "__main__":
    unittest.main()
