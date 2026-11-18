"""Redact credential values before they reach logs, traces, or responses."""

from __future__ import annotations

import json
import os
import re
from typing import Any

_ENV_NAMES = (
    "SHOPASSIST_CA_API_KEY",
    "SHOPASSIST_PA_API_KEY",
)
_BEARER = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_USERINFO = re.compile(r"(https?://)([^/\s:@]+):([^/\s@]+)@")


def collect_secret_values(extra: list[str] | None = None) -> list[str]:
    values: list[str] = []
    for name in _ENV_NAMES:
        raw = os.environ.get(name) or ""
        if len(raw) >= 8:
            values.append(raw)
    for item in extra or []:
        if item and len(item) >= 8:
            values.append(item)
    # Stable unique order, longest first so shorter pieces do not leave fragments.
    unique = sorted(set(values), key=len, reverse=True)
    return unique


def redact_text(text: str, secrets: list[str] | None = None) -> tuple[str, int]:
    if text is None:
        return "", 0
    count = 0
    updated = str(text)
    for secret in secrets if secrets is not None else collect_secret_values():
        if secret and secret in updated:
            count += updated.count(secret)
            updated = updated.replace(secret, "[redacted]")
    bearer_hits = _BEARER.findall(updated)
    if bearer_hits:
        count += len(bearer_hits)
        updated = _BEARER.sub("Bearer [redacted]", updated)
    if _USERINFO.search(updated):
        updated, n = _USERINFO.subn(r"\1[redacted]@", updated)
        count += n
    return updated, count


def scrub_object(obj: Any, secrets: list[str] | None = None) -> tuple[Any, int]:
    """Return a JSON-safe copy with credential strings removed."""
    secrets = collect_secret_values() if secrets is None else secrets
    encoded = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    cleaned, count = redact_text(encoded, secrets)
    if count == 0:
        return obj, 0
    return json.loads(cleaned), count


def assert_clean(obj: Any, secrets: list[str] | None = None) -> None:
    secrets = collect_secret_values() if secrets is None else secrets
    blob = json.dumps(obj, ensure_ascii=False, default=str)
    for secret in secrets:
        if secret and secret in blob:
            raise RuntimeError("credential value appeared in an output object")
