"""Session context, seven-day memory, and durable summary behavior."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from shopassist.context_engine import Clock, ContextEngine, needs_summary_refresh
from shopassist.storage.errors import CacheUnavailable, StaleWriteError, StoreUnavailable
from shopassist.storage.caches import LocalMemoryCache
from shopassist.storage.sqlite_store import SqliteStore


def _memory():
    return {
        "confirmed_constraints": [{"field": "budget", "value": 20, "status": "confirmed"}],
        "assumptions": [{"field": "material", "value": "ceramic", "status": "assumed"}],
        "missing": [],
        "contradictions": [],
        "task_summaries": [],
        "active_task_id": None,
    }


class ContextRulesTest(unittest.TestCase):
    def test_lag_keeps_tail_and_correction_is_authoritative(self):
        from shopassist.eval.catalog import _long_correction, _summary_lag

        self.assertEqual(_summary_lag(), [])
        self.assertEqual(_long_correction(), [])

    def test_seven_day_boundary_and_isolation(self):
        from shopassist.eval.catalog import _isolation, _seven_day

        self.assertEqual(_seven_day(), [])
        self.assertEqual(_isolation(), [])

    def test_cache_loss_and_restart_and_idempotent_message(self):
        from shopassist.eval.catalog import _cache_restore

        self.assertEqual(_cache_restore(), [])
        folder = tempfile.mkdtemp()
        path = str(Path(folder) / "lab.sqlite")
        store = SqliteStore(path)
        store.append_message("t", "u", "s", "user", "hello", "2026-01-15T00:00:00Z", message_id=1)
        store.append_message("t", "u", "s", "user", "hello", "2026-01-15T00:00:00Z", message_id=1)
        reopened = SqliteStore(path)
        self.assertEqual(len(reopened.list_messages("t", "s")), 1)
        store.save_summary("t", "s", 0, 1, "background", "mock", {"source_kind": "mock"}, "2026-01-15T00:00:00Z")
        with self.assertRaises(StaleWriteError):
            store.save_summary("t", "s", 0, 1, "other", "mock", {"source_kind": "mock"}, "2026-01-15T00:00:00Z")

    def test_concurrent_cache_writer_one_wins(self):
        cache = LocalMemoryCache()
        barrier = threading.Barrier(2)
        outcomes = []

        def write():
            barrier.wait()
            outcomes.append(cache.compare_and_set("k", 0, {"summary_text": "v", "watermark_message_id": 1, "source_kind": "mock"}))

        threads = [threading.Thread(target=write) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), [False, True])

    def test_summary_schedule_uses_new_messages_not_total_only(self):
        messages = [{"message_id": index, "body": "x" * 40} for index in range(1, 5)]
        self.assertFalse(needs_summary_refresh(messages, 0, min_new=5, token_budget=1000))
        self.assertTrue(needs_summary_refresh(messages, 0, min_new=4, token_budget=1000))
        self.assertTrue(needs_summary_refresh(messages, 3, min_new=100, token_budget=5))

    def test_selected_backends_fail_closed(self):
        from shopassist.context_engine import open_cache_from_env, open_store_from_env

        previous = {key: __import__("os").environ.get(key) for key in ("SHOPASSIST_STORE", "SHOPASSIST_CACHE", "SHOPASSIST_MYSQL_HOST", "SHOPASSIST_MYSQL_PORT")}
        os_env = __import__("os").environ
        os_env["SHOPASSIST_STORE"] = "mysql"
        os_env["SHOPASSIST_MYSQL_HOST"] = "127.0.0.1"
        os_env["SHOPASSIST_MYSQL_PORT"] = "1"
        try:
            with self.assertRaises(StoreUnavailable) as caught:
                open_store_from_env()
            self.assertIn("local_sqlite", str(caught.exception))
            self.assertIn("Refusing", str(caught.exception))
            import sys
            from unittest.mock import patch

            with patch.dict(sys.modules, {"pymysql": None}):
                with self.assertRaises(StoreUnavailable) as missing:
                    open_store_from_env()
            self.assertIn("local_sqlite", str(missing.exception))
            self.assertIn("Refusing", str(missing.exception))
        finally:
            for key, value in previous.items():
                if value is None:
                    os_env.pop(key, None)
                else:
                    os_env[key] = value
        os_env["SHOPASSIST_CACHE"] = "redis"
        os_env["SHOPASSIST_REDIS_URL"] = "redis://127.0.0.1:1/0"
        try:
            with self.assertRaises(CacheUnavailable):
                open_cache_from_env(SqliteStore(":memory:"))
        finally:
            os_env.pop("SHOPASSIST_CACHE", None)
            os_env.pop("SHOPASSIST_REDIS_URL", None)

    def test_assumption_is_not_confirmed(self):
        engine = ContextEngine(sync_catchup_message_budget=0)
        memory = _memory()
        view = engine.record_exchange("t", "u", "s", "Recommend a drinkware item I can buy for less than 20 dollars, maybe ceramic.", "ok", memory)
        self.assertNotIn("material", view["authoritative_confirmed"])
        self.assertEqual(view["structured_state"]["assumptions"][0]["value"], "ceramic")
        self.assertEqual(view["authoritative_confirmed"]["budget"], 20)


class EvalCatalogTest(unittest.TestCase):
    def test_overlap_is_not_additive_and_release_executes(self):
        from shopassist.eval.catalog import LEGACY_PURPOSE_FILTER, SUITES, build_catalog, coverage_matrix, run_eval_catalog

        cases = build_catalog()
        matrix = coverage_matrix(cases)
        self.assertGreater(matrix["membership_sum"], matrix["unique_case_count"])
        self.assertEqual(matrix["missing_capabilities"], [])
        self.assertEqual(matrix["missing_tags"], [])
        self.assertEqual(matrix["online_sample_count"], 0)
        self.assertIsNone(matrix["production_kpi"])
        self.assertIsNone(matrix["online_metrics"])
        self.assertEqual(set(SUITES), {"development", "regression", "online_sample"})
        self.assertEqual(set(LEGACY_PURPOSE_FILTER), {"capability_acceptance", "release_regression", "robustness_challenge", "distributional_sample", "sealed_generalization", "workload_cost"})
        report = run_eval_catalog(include_development=True)
        self.assertTrue(report["release_passed"], [item for item in report["results"] if item["critical"] and not item["passed"]])
        self.assertFalse(report["production_release"])
        self.assertIsNone(report["weighted_aggregate"]["value"])
        self.assertIsNone(report["significance_test"])
        self.assertIn("regression", report["suites"])
        self.assertIn("development", report["suites"])
        sealed = next(item for item in cases if item["case_id"] == "sealed-notebook")
        self.assertEqual(sealed["suites"], ["regression"])
        self.assertIn("novel_combination", sealed["tags"])
        self.assertFalse(sealed["critical"])
        self.assertIn("sealed_generalization", sealed["legacy_purposes"])
        self.assertEqual(sealed["split"], "sealed")
        self.assertNotIn(sealed["lineage_id"], {card["lineage_id"] for card in __import__("shopassist.cards", fromlist=["all_cards"]).all_cards() if card["split"] == "train"})

    def test_bad_runtime_is_caught_and_distinct_from_release(self):
        import shopassist.communication as communication
        from shopassist.cards import all_cards
        from shopassist.eval.catalog import apply_suite_gates, build_catalog
        from shopassist.harness import evaluate_card

        critical_ids = {case["case_id"] for case in build_catalog() if case["critical"]}
        demo_ids = {case["case_id"] for case in build_catalog() if case.get("synthetic_demo")}
        self.assertTrue(critical_ids)
        self.assertNotIn("thanks-a", critical_ids)
        self.assertTrue(demo_ids)
        self.assertTrue(all(case.get("synthetic_demo") for case in build_catalog() if case["case_id"] in demo_ids))
        self.assertFalse(any("online_sample" in case["suites"] for case in build_catalog()))
        original = communication.decide_route

        def always_plan(features, memory):
            del memory
            return {
                "route": "complex_handoff",
                "route_reason_code": "forced",
                "intent": features["intent"],
                "must_not_invoke_planner": False,
                "skill_id": None,
                "skill_args": {},
                "goal_type": "search",
                "query": {},
                "missing": [],
                "contradicted": [],
            }

        communication.decide_route = always_plan
        try:
            thanks = next(card for card in all_cards() if card["card_id"] == "thanks-a")
            scored = evaluate_card(thanks)
            self.assertFalse(scored["score"]["passed"])
            self.assertTrue(any(item["check"] in {"route", "unnecessary_planner", "planner_turns"} for item in scored["score"]["failures"]))
        finally:
            communication.decide_route = original

        baseline = {
            "catalog_version": "eval-catalog-v2",
            "snapshot_id": "synthetic-catalog-v1",
            "results": [
                {"case_id": "crit", "suites": ["regression"], "critical": True, "passed": True, "unknown": False},
                {"case_id": "issue", "suites": ["regression"], "critical": False, "passed": False, "unknown": False},
            ],
        }
        candidate = [
            {"case_id": "crit", "suites": ["regression"], "critical": True, "passed": False, "unknown": False, "latency_ms": 1, "token_usage": None},
            {"case_id": "issue", "suites": ["regression"], "critical": False, "passed": True, "unknown": False, "latency_ms": 1, "token_usage": None},
        ]
        gates = apply_suite_gates(candidate, baseline, {"allowed_pass_rate_drop": 0.0, "target_issue_ids": ["issue"], "snapshot_id": "synthetic-catalog-v1"})
        self.assertEqual(gates["status"], "fail")
        self.assertFalse(gates["passed"])
        self.assertFalse(gates["production_release"])
        self.assertIn("crit", gates["critical"]["failed_case_ids"])

        unknown_target = apply_suite_gates(
            [{"case_id": "issue", "suites": ["regression"], "critical": True, "passed": False, "unknown": True, "latency_ms": None, "token_usage": None}],
            baseline,
            {"allowed_pass_rate_drop": 0.0, "target_issue_ids": ["missing-issue"], "snapshot_id": "synthetic-catalog-v1"},
        )
        self.assertEqual(unknown_target["status"], "unknown")
        self.assertFalse(unknown_target["passed"])
        self.assertIsNone(unknown_target["latency_cost"]["p95_latency_ms"])

    def test_ingest_rejects_unlabeled_samples(self):
        from shopassist.eval.catalog import (
            archive_online_batch,
            blocked_training_session_ids,
            build_catalog,
            copy_failure_to_development,
            ingest_deidentified,
            promote_to_regression,
        )

        empty = ingest_deidentified({"schema": "deidentified-sample-v1", "records": [], "production_kpi": None})
        self.assertEqual(empty["sample_count"], 0)
        self.assertIsNone(empty["production_kpi"])
        with self.assertRaises(ValueError):
            ingest_deidentified({"schema": "deidentified-sample-v1", "records": [{"text": "hello", "source": {"kind": "synthetic"}}], "production_kpi": None})
        online = ingest_deidentified({"schema": "deidentified-session-v1", "records": [], "production_kpi": None, "online_metrics": None})
        self.assertEqual(online["sample_count"], 0)
        self.assertIsNone(online["online_metrics"])
        archived = archive_online_batch({"schema": "deidentified-session-v1", "records": [], "batch_id": "empty"}, archived_at="2026-01-15T00:00:00Z")
        self.assertEqual(archived["status"], "archived")
        self.assertIsNone(archived["online_metrics"])
        record = {
            "case_id": "batch-1",
            "lineage_id": "lin-online-1",
            "session_id": "sess-online-1",
            "session": [],
            "tool_evidence": [],
            "window": {"start": "2026-01-01", "end": "2026-01-02"},
            "sampling": {"design": "stratified", "weight": None},
            "tags": ["ca"],
            "source": {"kind": "deidentified_sample", "deidentified": True, "refs": ["batch-1"]},
        }
        copied = copy_failure_to_development(record)
        self.assertEqual(copied["suites"], ["development"])
        self.assertFalse(copied["training_row"])
        with self.assertRaises(ValueError):
            promote_to_regression(copied, {"decision": "hold"}, new_version="eval-catalog-v3")
        promoted = promote_to_regression(
            copied,
            {
                "decision": "promote",
                "reviewer": "fixture-review",
                "reviewed_at": "2026-01-16",
                "expected_predicates": ["route == direct_answer"],
                "catalog_version": "eval-catalog-v3",
                "critical": True,
                "source_refs": ["batch-1"],
            },
            new_version="eval-catalog-v3",
        )
        self.assertIn("regression", promoted["suites"])
        self.assertTrue(promoted["critical"])
        self.assertFalse(promoted["training_row"])
        blocked = blocked_training_session_ids(build_catalog() + [promoted])
        self.assertIn(promoted["session_id"], blocked)
        self.assertIn("sess-mem-long", blocked)
        self.assertNotIn("sess-online-1", {case["session_id"] for case in build_catalog()})
