"""Parse provider chat-completion SSE frames.

This accepts fragmented UTF-8 and withholds nothing by itself. Callers decide
whether a delta is safe to show.
"""

from __future__ import annotations

import codecs
import json


class StreamParseError(ValueError):
    pass


class StreamTruncated(StreamParseError):
    pass


class ChatDeltaParser:
    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""
        self.done = False
        self.usage = None

    def feed(self, data: bytes) -> list[dict]:
        if self.done:
            return []
        self._buffer += self._decoder.decode(data or b"", final=False)
        return self._drain(final=False)

    def finish(self) -> list[dict]:
        self._buffer += self._decoder.decode(b"", final=True)
        events = self._drain(final=True)
        if not self.done:
            raise StreamTruncated("provider_stream_ended_without_done")
        return events

    def _drain(self, *, final: bool) -> list[dict]:
        events = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            parsed = self._line(line)
            if parsed is not None:
                events.append(parsed)
        if final and self._buffer.strip():
            parsed = self._line(self._buffer)
            self._buffer = ""
            if parsed is not None:
                events.append(parsed)
        return events

    def _line(self, line: str) -> dict | None:
        text = line.strip()
        if not text or text.startswith(":"):
            return None
        if not text.startswith("data:"):
            raise StreamParseError("provider_stream_malformed")
        payload = text[len("data:") :].strip()
        if payload == "[DONE]":
            self.done = True
            return {"kind": "done"}
        try:
            body = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StreamParseError("provider_stream_malformed") from exc
        if not isinstance(body, dict):
            raise StreamParseError("provider_stream_malformed")
        if isinstance(body.get("usage"), dict):
            self.usage = body["usage"]
        choices = body.get("choices") or []
        if not choices:
            return {"kind": "usage", "usage": self.usage} if self.usage else None
        delta = (choices[0] or {}).get("delta") or {}
        content = delta.get("content")
        if content:
            return {"kind": "delta", "text": content}
        if choices[0].get("finish_reason"):
            return None
        return None
