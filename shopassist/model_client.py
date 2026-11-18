"""Chat-completions client for an explicitly configured endpoint.

The default runtime never constructs this client. Credential values are not
included in errors or reprs.
"""

from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request, urlopen

from shopassist.config import ConfigError
from shopassist.redact import redact_text


class StreamUnsupported(ConfigError):
    def __init__(self):
        super().__init__("provider_stream_unsupported", {"stream_source": "buffered_fallback"})


class ChatCompletionsClient:
    def __init__(self, *, base_url: str, model: str, api_key: str = "", timeout: float = 30, transport=None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key or ""
        self.timeout = timeout
        self._transport = transport or _urllib_transport
        self.completed_calls = 0
        self.last_usage = None

    @property
    def credential_present(self) -> bool:
        return bool(self._api_key)

    def __repr__(self) -> str:
        return (
            f"ChatCompletionsClient(model={self.model!r}, credential_present={bool(self._api_key)})"
        )

    def complete(self, messages: list[dict], timeout: float | None = None) -> str:
        self.completed_calls += 1
        url = self.base_url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        body = {"model": self.model, "messages": messages, "temperature": 0}
        try:
            raw = self._transport(url, headers, body, self.timeout if timeout is None else timeout)
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface a sanitized code only
            message, _ = redact_text(str(exc), [self._api_key] if self._api_key else [])
            raise ConfigError(
                "configured_endpoint_unreachable",
                {"model": self.model, "reason": "endpoint_error", "endpoint_scope": _scope(self.base_url)},
            ) from None
        if self._api_key and self._api_key in raw:
            raw = raw.replace(self._api_key, "[redacted]")
        try:
            payload = json.loads(raw)
            self.last_usage = payload.get("usage")
            return payload["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(
                "configured_endpoint_bad_response",
                {"model": self.model, "reason": type(exc).__name__},
            ) from None

    def complete_stream(self, messages: list[dict], timeout: float | None = None):
        """Yield provider text deltas. Non-stream responses are not relabeled as deltas."""
        from shopassist.stream_parse import ChatDeltaParser, StreamParseError

        self.completed_calls += 1
        url = self.base_url + "/chat/completions"
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        transport = getattr(self, "_stream_transport", None) or _default_stream_transport
        try:
            chunks = transport(url, headers, body, self.timeout if timeout is None else timeout)
        except ConfigError:
            raise
        except Exception:
            raise ConfigError(
                "configured_endpoint_unreachable",
                {"model": self.model, "reason": "endpoint_error", "endpoint_scope": _scope(self.base_url)},
            ) from None
        parser = ChatDeltaParser()
        try:
            for chunk in chunks:
                if isinstance(chunk, str):
                    raise StreamUnsupported()
                for event in parser.feed(chunk):
                    if event["kind"] == "delta":
                        text = event["text"]
                        if self._api_key and self._api_key in text:
                            text = text.replace(self._api_key, "[redacted]")
                        yield text
                    elif event["kind"] == "usage" and event.get("usage"):
                        self.last_usage = event["usage"]
            for event in parser.finish():
                if event["kind"] == "delta":
                    yield event["text"]
                elif event["kind"] == "usage" and event.get("usage"):
                    self.last_usage = event["usage"]
        except StreamParseError as exc:
            raise ConfigError(str(exc), {"model": self.model, "stream_source": "provider"}) from exc
        if parser.usage:
            self.last_usage = parser.usage


def _default_stream_transport(url: str, headers: dict, body: dict, timeout: float):
    request = Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        response = urlopen(request, timeout=timeout)  # noqa: S310 — operator-configured URL
    except URLError as exc:
        raise ConnectionError("endpoint_unreachable") from exc
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "text/event-stream" not in content_type:
        response.close()
        raise StreamUnsupported()

    def chunks():
        try:
            while True:
                block = response.read(32)
                if not block:
                    break
                yield block
        finally:
            response.close()

    return chunks()


def _scope(url: str) -> str:
    if "127.0.0.1" in url or "localhost" in url:
        return "local"
    return "remote"


def _urllib_transport(url: str, headers: dict, body: dict, timeout: float) -> str:
    request = Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 — operator-configured URL
            return response.read().decode("utf-8")
    except URLError as exc:
        raise ConnectionError("endpoint_unreachable") from exc
