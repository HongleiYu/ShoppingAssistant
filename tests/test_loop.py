import unittest

from shopassist.loop_control import FakeClock, classify_failure, validate_finish
from shopassist.planner_policy import new_planner_state
from shopassist.sandbox import SessionSandbox
from shopassist.session_runner import AgentSession
from shopassist.settings import load_settings


class CompletionTest(unittest.TestCase):
    def test_ungrounded_finish_and_constraint_and_no_match(self):
        state = new_planner_state()
        state["observations"] = [
            {"status": "ok", "skill_id": "product_search", "items": [{"product_id": "prod-mug-sand", "price": 12, "category": "drinkware"}]}
        ]
        task = {"goal_type": "search", "query": {"category": "drinkware", "max_price": 20, "price_op": "lt"}}
        bad = validate_finish(task, state, {"result": {"items": [{"product_id": "prod-missing", "price": 1}]}, "verification": {"passed": True}})
        self.assertEqual(bad["terminal"]["status"], "FAILED")
        self.assertEqual(bad["terminal"]["reason_code"], "ungrounded_finish")
        over = validate_finish(
            task,
            state,
            {"result": {"items": [{"product_id": "prod-mug-sand", "price": 50, "category": "drinkware"}]}},
        )
        self.assertEqual(over["terminal"]["reason_code"], "constraint_violation")
        empty = new_planner_state()
        empty["observations"] = [{"status": "empty", "skill_id": "product_search", "items": []}]
        none = validate_finish(task, empty, {"result": None, "verification": {"passed": True}})
        self.assertEqual(none["terminal"]["status"], "COMPLETED_NO_MATCH")
        outage = new_planner_state()
        outage["observations"] = [{"status": "error", "skill_id": "product_search", "items": [], "error_code": "backend_unavailable"}]
        failed = classify_failure({"failure": {"reason_code": "no_match", "escalate": False}}, outage)
        self.assertEqual(failed["status"], "FAILED")
        self.assertNotEqual(failed["reason_code"], "no_match")

    def test_ask_resume_and_supersede_keep_the_session(self):
        box = SessionSandbox.start(seed=2)
        calls = {"n": 0}

        def decider(task, state, registry):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "action": "ask_user",
                    "ask": {"text": "Which color?", "missing": ["color"]},
                    "skill_call": None,
                    "plan": [],
                    "failure": None,
                    "result": None,
                    "recovery": None,
                }
            from shopassist.planner_policy import act

            return act(task, state, registry)

        try:
            session = AgentSession(box, "resume", settings=load_settings(), decider=decider)
            first = session.user_message("Find a drinkware item under 20 dollars.")
            self.assertEqual(first["terminal"]["status"], "WAITING_USER")
            self.assertIsNone(session.memory.get("active_task_id"))
            self.assertIsNotNone(session.paused_task)
            second = session.user_message("Show me drinkware in sand for under 20 dollars.")
            self.assertEqual(second["terminal"]["status"], "COMPLETED")
            self.assertTrue(second.get("resumed"))
            self.assertEqual(len(session.trace()["task_ids"]), 1)
            self.assertIsNone(session.paused_task)
            thanks = session.user_message("Thanks, that is all I needed.")
            self.assertEqual(thanks["route"], "direct_answer")
            self.assertGreaterEqual(len(session.history), 3)
        finally:
            box.close()

    def test_wall_clock_and_repeat_stop(self):
        clock = FakeClock(0.0)
        box = SessionSandbox.start(seed=3)
        try:
            session = AgentSession(
                box,
                "clock",
                settings=load_settings(overrides={"loop": {"wall_clock_seconds": 0}}, environ={}),
                clock=clock,
            )
            stopped = session.user_message("Find a drinkware item under 20 dollars.")
            self.assertEqual(stopped["terminal"]["reason_code"], "wall_clock")
            self.assertEqual(stopped["tools"], [])
        finally:
            box.close()

    def test_invoke_timeout_is_the_remaining_budget(self):
        clock = FakeClock(0.0)
        box = SessionSandbox.start(seed=4)
        seen = {}
        original = box.process.client.call_tool

        def wrapped(name, arguments, timeout=None):
            seen["timeout"] = timeout
            return original(name, arguments, timeout=timeout)

        box.process.client.call_tool = wrapped
        try:
            session = AgentSession(
                box,
                "timeout",
                settings=load_settings(overrides={"loop": {"wall_clock_seconds": 0.4}, "mcp": {"tool_timeout_seconds": 2}}, environ={}),
                clock=clock,
            )
            session.user_message("What is the return window?")
            self.assertIsNotNone(seen["timeout"])
            self.assertLessEqual(seen["timeout"], 0.4)
        finally:
            box.close()


if __name__ == "__main__":
    unittest.main()
