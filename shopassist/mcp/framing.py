"""LSP-style content-length framing for MCP JSON-RPC messages."""

from __future__ import annotations

import json
import os
import select
import time

MAX_MESSAGE_BYTES = 1_000_000
MAX_HEADER_BYTES = 64 * 1024


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
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    write_all(fd, header + body)


def _read_exact(fd: int, size: int, deadline: float | None) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        timeout = None
        if deadline is not None:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("mcp_read_timeout")
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            raise TimeoutError("mcp_read_timeout")
        chunk = os.read(fd, remaining)
        if not chunk:
            raise EOFError("mcp_closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(fd: int, timeout: float | None = None) -> dict:
    deadline = None if timeout is None else time.monotonic() + timeout
    header = b""
    while b"\r\n\r\n" not in header:
        if len(header) > MAX_HEADER_BYTES:
            raise FramingError("mcp_header_too_large")
        header += _read_exact(fd, 1, deadline)
    header_text, _, _ = header.partition(b"\r\n\r\n")
    length = None
    for line in header_text.decode("ascii", errors="replace").split("\r\n"):
        if not line.strip():
            continue
        if ":" not in line:
            raise FramingError("mcp_bad_header")
        key, value = line.split(":", 1)
        if key.strip().lower() == "content-length":
            try:
                length = int(value.strip())
            except ValueError as exc:
                raise FramingError("mcp_bad_length") from exc
    if length is None:
        raise FramingError("mcp_missing_length")
    if length < 0 or length > MAX_MESSAGE_BYTES:
        raise FramingError("mcp_message_too_large")
    body = _read_exact(fd, length, deadline)
    try:
        message = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise FramingError("mcp_bad_json") from exc
    if not isinstance(message, dict):
        raise FramingError("mcp_message_not_object")
    return message
