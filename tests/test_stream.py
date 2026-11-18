import json
import unittest

from shopassist.model_client import ChatCompletionsClient
from shopassist.progress import ProgressPump, iter_turn_events, parse_sse
from shopassist.sandbox import SessionSandbox
from shopassist.session_runner import AgentSession
from shopassist.settings import load_settings
from shopassist.stream_parse import ChatDeltaParser, StreamParseError, StreamTruncated
from shopassist.web.server import ChatApp


def _sse(text: str) -> list[bytes]:
    payload = json.dumps({"choices": [{"delta": {"content": text}}]}, separators=(",", ":"))
    return [f"data: {payload}\n".encode(), b"data: [DONE]\n"]


class ParserTest(unittest.TestCase):
    def test_split_unicode_usage_and_done(self):
        parser = ChatDeltaParser()
        word = "你好".encode()
        events = []
        events.extend(parser.feed(b"data: {\"choices\":[{\"delta\":{\"content\":\""))
        events.extend(parser.feed(word[:1]))
        events.extend(parser.feed(word[1:]))
        events.extend(parser.feed("\"}}]}\n".encode()))
        events.extend(parser.feed(b"data: {\"usage\":{\"total_tokens\":3}}\n"))
        events.extend(parser.finish() if False else parser.feed(b"data: [DONE]\n"))
        events.extend(parser.finish())
        text = "".join(item["text"] for item in events if item["kind"] == "delta")
        self.assertEqual(text, "你好")
        self.assertEqual(parser.usage["total_tokens"], 3)
        self.assertTrue(parser.done)

    def test_malformed_and_truncated(self):
        parser = ChatDeltaParser()
        with self.assertRaises(StreamParseError):
            parser.feed(b"data: {not-json}\n")
        empty = ChatDeltaParser()
        empty.feed(b"data: {\"choices\":[{\"delta\":{\"content\":\"a\"}}]}\n")
        with self.assertRaises(StreamTruncated):
            empty.finish()


class PumpTest(unittest.TestCase):
    def test_fast_path_skips_pending_and_coalesces_milestones(self):
        pump = ProgressPump(0.2, 0.15)
        pump.start(0.0)
        self.assertEqual(pump.poll(0.1, True), [])
        first = pump.push({"type": "progress", "kind": "retrieving", "text": "正在检索"}, 0.1)
        second = pump.push({"type": "progress", "kind": "comparing", "text": "正在比较"}, 0.2)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(pump.poll(1.0, True), [])

    def test_slow_path_emits_one_pending(self):
        pump = ProgressPump(0.2, 0.15)
        pump.start(0.0)
        pending = pump.poll(0.2, True)
        self.assertEqual(pending[0]["text"], "我正在处理中")
        self.assertEqual(pump.poll(0.5, True), [])


class StreamTurnTest(unittest.TestCase):
    def test_mock_is_deterministic_and_done_once(self):
        box = SessionSandbox.start(seed=1)
        try:
            session = AgentSession(box, "s-mock", settings=load_settings())
            blob = "".join(iter_turn_events(session, "Thanks, that is all I needed.", session.settings, request_id="r1"))
        finally:
            box.close()
        events = parse_sse(blob)
        self.assertEqual([item["event"] for item in events if item["event"] in {"done", "error"}], ["done"])
        deltas = [item for item in events if item["event"] == "text_delta"]
        self.assertEqual(deltas[0]["data"]["source"], "deterministic")
        self.assertNotIn("provider", {item["data"].get("source") for item in deltas})
        self.assertTrue(all(item["data"]["session_id"] == "s-mock" for item in events))
        self.assertEqual([item["data"]["sequence"] for item in events], list(range(1, len(events) + 1)))

    def test_provider_deltas_must_match_validated_reply(self):
        reply = "Hello there"
        frames = []
        for piece in ("He", "llo ", "there"):
            frames.append(b"data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}).encode() + b"\n")
        frames.append(b"data: [DONE]\n")

        def transport(url, headers, body, timeout):
            del url, headers, timeout
            content = json.dumps(
                {
                    "route": "direct_answer",
                    "route_reason_code": "configured_model",
                    "reply_text": reply,
                    "must_not_invoke_planner": True,
                }
            )
            return json.dumps({"choices": [{"message": {"content": content}}]})

        def stream_transport(url, headers, body, timeout):
            del url, headers, body, timeout
            return iter(frames)

        client = ChatCompletionsClient(base_url="http://127.0.0.1:9/v1", model="ca", transport=transport)
        client._stream_transport = stream_transport
        box = SessionSandbox.start(seed=1)
        try:
            session = AgentSession(
                box,
                "s-provider",
                mode="configured_endpoint",
                model_clients={"ca": client, "pa": client},
                settings=load_settings(),
            )
            blob = "".join(iter_turn_events(session, "Hello", session.settings, request_id="r2"))
        finally:
            box.close()
        events = parse_sse(blob)
        deltas = [item["data"] for item in events if item["event"] == "text_delta"]
        self.assertTrue(deltas)
        self.assertTrue(all(item["source"] == "provider" for item in deltas))
        self.assertEqual("".join(item["text"] for item in deltas), reply)
        self.assertEqual([item["event"] for item in events if item["event"] in {"done", "error"}], ["done"])
        self.assertNotIn("Repeat the user message", blob)

    def test_diverging_provider_is_labeled_fallback(self):
        frames = [b"data: " + json.dumps({"choices": [{"delta": {"content": "Nope"}}]}).encode() + b"\n", b"data: [DONE]\n"]

        def transport(url, headers, body, timeout):
            del url, headers, timeout
            content = json.dumps(
                {
                    "route": "direct_answer",
                    "route_reason_code": "configured_model",
                    "reply_text": "Hello there",
                    "must_not_invoke_planner": True,
                }
            )
            return json.dumps({"choices": [{"message": {"content": content}}]})

        client = ChatCompletionsClient(base_url="http://127.0.0.1:9/v1", model="ca", transport=transport)
        client._stream_transport = lambda *args: iter(frames)
        box = SessionSandbox.start(seed=1)
        try:
            session = AgentSession(box, "s-fallback", mode="configured_endpoint", model_clients={"ca": client, "pa": client}, settings=load_settings())
            blob = "".join(iter_turn_events(session, "Hello", session.settings, request_id="r3"))
        finally:
            box.close()
        sources = [item["data"]["source"] for item in parse_sse(blob) if item["event"] == "text_delta"]
        self.assertIn("buffered_fallback", sources)
        self.assertNotIn("api_key", blob)

    def test_replay_does_not_run_the_turn_twice(self):
        app = ChatApp()
        created = app.create("mock")
        calls = {"n": 0}
        session = app.sessions[created["session_id"]]["session"]
        original = session.user_message

        def counting(text, on_progress=None):
            calls["n"] += 1
            return original(text, on_progress=on_progress)

        session.user_message = counting
        first = "".join(app.iter_stream(created["session_id"], "Thanks, that is all I needed.", "same"))
        second = "".join(app.iter_stream(created["session_id"], "Thanks, that is all I needed.", "same"))
        self.assertEqual(first, second)
        self.assertEqual(calls["n"], 1)
        session.sandbox.close()


class SettingsTest(unittest.TestCase):
    def test_precedence_unknown_and_redaction(self):
        base = load_settings(overrides={"loop": {"max_plan_steps": 3}}, environ={})
        self.assertEqual(base["loop"]["max_plan_steps"], 3)
        self.assertEqual(base["progress"]["pending_after_ms"], 200)
        env = load_settings(overrides={"loop": {"max_plan_steps": 3}}, environ={"SHOPASSIST_MAX_PLAN_STEPS": "4"})
        self.assertEqual(env["loop"]["max_plan_steps"], 3)
        from_env = load_settings(environ={"SHOPASSIST_MAX_PLAN_STEPS": "4", "SHOPASSIST_CA_API_KEY": "unit-test-sentinel-value"})
        self.assertEqual(from_env["loop"]["max_plan_steps"], 4)
        public = json.dumps(__import__("shopassist.settings", fromlist=["public_settings"]).public_settings(from_env))
        self.assertNotIn("unit-test-sentinel-value", public)
        self.assertIn("[redacted]", public)
        with self.assertRaises(Exception):
            load_settings(overrides={"loop": {"not_a_setting": 1}}, environ={})


if __name__ == "__main__":
    unittest.main()
