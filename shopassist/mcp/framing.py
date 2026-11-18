"""Newline-delimited JSON-RPC framing used by the MCP stdio transport.

Each message is one UTF-8 JSON object followed by a newline. Embedded newlines
are not emitted; string contents are escaped by the JSON encoder.
"""

from __future__ import annotations

import json
import os
import select
import time

MAX_MESSAGE_BYTES = 1_000_000

_BUFFERS: dict[int, bytes] = {}


class FramingError(RuntimeError):
    pass


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise FramingError("mcp_write_failed")
        view = view[written:]


def write_message(fd: int, payload: dict) -> None:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body) > MAX_MESSAGE_BYTES:
        raise FramingError("mcp_message_too_large")
    if b"\n" in body or b"\r" in body:
        raise FramingError("mcp_message_contains_newline")
    write_all(fd, body + b"\n")


def read_message(fd: int, timeout: float | None = None) -> dict:
    deadline = None if timeout is None else time.monotonic() + timeout
    buf = _BUFFERS.get(fd, b"")
    while b"\n" not in buf:
        if len(buf) > MAX_MESSAGE_BYTES:
            _BUFFERS[fd] = b""
            raise FramingError("mcp_message_too_large")
        wait = None
        if deadline is not None:
            wait = deadline - time.monotonic()
            if wait <= 0:
                _BUFFERS[fd] = buf
                raise TimeoutError("mcp_read_timeout")
        ready, _, _ = select.select([fd], [], [], wait)
        if not ready:
            _BUFFERS[fd] = buf
            raise TimeoutError("mcp_read_timeout")
        chunk = os.read(fd, 65536)
        if not chunk:
            _BUFFERS[fd] = b""
            raise EOFError("mcp_closed")
        buf += chunk
    line, _, rest = buf.partition(b"\n")
    _BUFFERS[fd] = rest
    if line.endswith(b"\r"):
        line = line[:-1]
    if not line.strip():
        return read_message(fd, timeout=None if deadline is None else max(0.0, deadline - time.monotonic()))
    try:
        message = json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise FramingError("mcp_bad_json") from exc
    if not isinstance(message, dict):
        raise FramingError("mcp_message_not_object")
    return message
