"""Speak a reply that is already validated.

Provider deltas are emitted only while they match that reply. A provider that
does not speak the validated text, or that has no stream, is labeled as a
buffered fallback. Mock mode does not imitate token streaming.
"""

from __future__ import annotations

from shopassist.config import ConfigError
from shopassist.progress import CancelledTurn


def iter_validated_text(session, reply_text: str):
    expected = reply_text or ""
    if session.mode != "configured_endpoint" or not session.settings["model"].get("stream_final", True):
        yield {
            "type": "text_delta",
            "source": "deterministic",
            "text": expected,
            "buffered": True,
            "factual_guarantee": "rendered_from_validated_turn",
        }
        return
    client = (session.model_clients or {}).get("ca")
    if client is None or not hasattr(client, "complete_stream"):
        yield _fallback(expected, "provider_stream_unavailable")
        return
    messages = [
        {
            "role": "system",
            "content": "Repeat the user message exactly and add nothing.",
        },
        {"role": "user", "content": expected},
    ]
    accumulated = ""
    try:
        timeout = session.budget.call_timeout(session.settings["model"]["ca"]["timeout_seconds"])
        if timeout is None:
            raise CancelledTurn()
        for delta in client.complete_stream(messages, timeout=timeout):
            session._raise_if_cancelled()
            if not isinstance(delta, str) or not expected.startswith(accumulated + delta):
                yield _fallback(expected, "provider_diverged_from_validated_reply")
                return
            accumulated += delta
            session.budget.partial_streamed = True
            yield {
                "type": "text_delta",
                "source": "provider",
                "text": delta,
                "buffered": False,
                "factual_guarantee": "prefix_of_validated_reply_not_every_token_prechecked",
            }
    except CancelledTurn:
        raise
    except ConfigError as exc:
        yield _fallback(expected, exc.code)
        return
    if accumulated != expected:
        yield _fallback(expected, "provider_stream_incomplete")


def _fallback(expected: str, reason: str) -> dict:
    return {
        "type": "text_delta",
        "source": "buffered_fallback",
        "text": expected,
        "buffered": True,
        "reason": reason,
        "factual_guarantee": "validated_reply_not_a_provider_delta",
    }
