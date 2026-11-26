"""One settings object for the lab.

Precedence is defaults, then a config file, then environment variables, then
explicit overrides. Unknown keys fail. Secret fields are redacted from the
public view. Protocol enums stay in code, not in this file.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONFIG_PATH = REPO_ROOT / "config" / "local_shopassist.json"
LEGACY_CONFIG_PATH = REPO_ROOT / "config" / "local_model.json"

MODES = ("mock", "configured_endpoint", "local_weights")
STORE_KINDS = ("local_sqlite", "mysql")
CACHE_KINDS = ("local_memory", "redis", "mysql_fallback")
SUMMARY_MODES = ("mock", "configured_model")

DEFAULTS = {
    "mode": "mock",
    "opt_in_configured_model": False,
    "opt_in_local_weights": False,
    "seed": 0,
    "loop": {
        "max_plan_steps": 8,
        "max_tool_calls": 30,
        "max_model_calls": 16,
        "max_retries_per_step": 1,
        "max_dag_concurrency": 4,
        "max_replans": 1,
        "max_fallbacks": 1,
        "repeat_limit": 1,
        "wall_clock_seconds": 30,
        "token_budget": None,
        "missing_usage_tokens": 32,
        "chars_per_token": 4,
    },
    "model": {
        "ca": {
            "base_url": "",
            "model": "",
            "api_key": "",
            "api_key_env": "SHOPASSIST_CA_API_KEY",
            "timeout_seconds": 30,
        },
        "pa": {
            "base_url": "",
            "model": "",
            "api_key": "",
            "api_key_env": "SHOPASSIST_PA_API_KEY",
            "timeout_seconds": 30,
        },
        "judge": {
            "enabled": False,
            "base_url": "",
            "model": "",
            "api_key": "",
            "api_key_env": "SHOPASSIST_JUDGE_API_KEY",
        },
        "stream_final": True,
    },
    "mcp": {"tool_timeout_seconds": 2},
    "context": {
        "latest_messages": 10,
        "recent_memory_days": 7,
        "summary_min_new_messages": 4,
        "summary_token_budget": 200,
        "sync_catchup_messages": 8,
        "summary_mode": "mock",
    },
    "store": {"kind": "local_sqlite", "sqlite_path": ":memory:"},
    "cache": {
        "kind": "local_memory",
        "redis_url": "",
        "fallback": "",
        "socket_timeout_seconds": 2,
    },
    "mysql": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "",
        "password": "",
        "database": "shopassist",
        "connect_timeout_seconds": 3,
    },
    "progress": {
        "pending_after_ms": 200,
        "milestone_min_interval_ms": 150,
    },
    "recovery": {
        "backoff_base_seconds": 0.05,
        "backoff_cap_seconds": 0.25,
        "jitter_ratio": 0.2,
        "honor_retry_after": True,
    },
    "ui": {"host": "127.0.0.1", "port": 8765, "max_body_bytes": 100000},
    "paths": {
        "corpus": "data/demo_corpus",
        "reports": "reports",
        "gates": "configs/gates_demo.json",
        "release_gates": "configs/release_gates.json",
        "training_ca": "configs/sft_ca_smoke.json",
        "training_pa": "configs/sft_pa_smoke.json",
    },
    "knowledge": {
        "policy_count": 50,
        "product_count": 1000,
        "seed": 0,
        "policy_path": "data/synthetic_kb/policies.json",
        "product_path": "data/synthetic_kb/products.json",
        "index_dir": "data/kb_index",
        "encoder": "hashing_lexical_baseline",
        "embedding_model": "",
        "embedding_dim": 128,
        "encoder_version": "hash-v1",
        "top_k": 5,
        "max_hits": 3,
        "dedup_cosine": 0.92,
        "score_threshold": 0.2,
        "chunk_chars": 700,
        "index_version": "kb-index-v1",
    },
}

_SECRET_KEYS = {"api_key", "password", "secret", "authorization"}

_ENV_STRINGS = {
    "SHOPASSIST_MODE": "mode",
    "SHOPASSIST_CA_BASE_URL": "model.ca.base_url",
    "SHOPASSIST_CA_MODEL": "model.ca.model",
    "SHOPASSIST_CA_API_KEY": "model.ca.api_key",
    "SHOPASSIST_PA_BASE_URL": "model.pa.base_url",
    "SHOPASSIST_PA_MODEL": "model.pa.model",
    "SHOPASSIST_PA_API_KEY": "model.pa.api_key",
    "SHOPASSIST_JUDGE_API_KEY": "model.judge.api_key",
    "SHOPASSIST_STORE": "store.kind",
    "SHOPASSIST_SQLITE_PATH": "store.sqlite_path",
    "SHOPASSIST_CACHE": "cache.kind",
    "SHOPASSIST_REDIS_URL": "cache.redis_url",
    "SHOPASSIST_CACHE_FALLBACK": "cache.fallback",
    "SHOPASSIST_MYSQL_HOST": "mysql.host",
    "SHOPASSIST_MYSQL_USER": "mysql.user",
    "SHOPASSIST_MYSQL_PASSWORD": "mysql.password",
    "SHOPASSIST_MYSQL_DATABASE": "mysql.database",
    "SHOPASSIST_SUMMARY_MODE": "context.summary_mode",
}

_ENV_INTS = {
    "SHOPASSIST_SEED": "seed",
    "SHOPASSIST_MAX_PLAN_STEPS": "loop.max_plan_steps",
    "SHOPASSIST_MAX_TOOL_CALLS": "loop.max_tool_calls",
    "SHOPASSIST_MAX_MODEL_CALLS": "loop.max_model_calls",
    "SHOPASSIST_MAX_RETRIES": "loop.max_retries_per_step",
    "SHOPASSIST_MAX_DAG_CONCURRENCY": "loop.max_dag_concurrency",
    "SHOPASSIST_MAX_REPLANS": "loop.max_replans",
    "SHOPASSIST_MAX_FALLBACKS": "loop.max_fallbacks",
    "SHOPASSIST_REPEAT_LIMIT": "loop.repeat_limit",
    "SHOPASSIST_TOKEN_BUDGET": "loop.token_budget",
    "SHOPASSIST_MISSING_USAGE_TOKENS": "loop.missing_usage_tokens",
    "SHOPASSIST_LATEST_MESSAGES": "context.latest_messages",
    "SHOPASSIST_RECENT_MEMORY_DAYS": "context.recent_memory_days",
    "SHOPASSIST_SUMMARY_MIN_NEW": "context.summary_min_new_messages",
    "SHOPASSIST_SUMMARY_TOKEN_BUDGET": "context.summary_token_budget",
    "SHOPASSIST_SYNC_CATCHUP": "context.sync_catchup_messages",
    "SHOPASSIST_MYSQL_PORT": "mysql.port",
    "SHOPASSIST_UI_PORT": "ui.port",
    "SHOPASSIST_UI_MAX_BODY_BYTES": "ui.max_body_bytes",
    "SHOPASSIST_PROGRESS_AFTER_MS": "progress.pending_after_ms",
    "SHOPASSIST_MILESTONE_MIN_INTERVAL_MS": "progress.milestone_min_interval_ms",
    "SHOPASSIST_KB_POLICY_COUNT": "knowledge.policy_count",
    "SHOPASSIST_KB_PRODUCT_COUNT": "knowledge.product_count",
    "SHOPASSIST_KB_SEED": "knowledge.seed",
    "SHOPASSIST_KB_DIM": "knowledge.embedding_dim",
    "SHOPASSIST_KB_TOP_K": "knowledge.top_k",
    "SHOPASSIST_KB_MAX_HITS": "knowledge.max_hits",
    "SHOPASSIST_KB_CHUNK_CHARS": "knowledge.chunk_chars",
}

_ENV_FLOATS = {
    "SHOPASSIST_WALL_CLOCK_SECONDS": "loop.wall_clock_seconds",
    "SHOPASSIST_CA_TIMEOUT_SECONDS": "model.ca.timeout_seconds",
    "SHOPASSIST_PA_TIMEOUT_SECONDS": "model.pa.timeout_seconds",
    "SHOPASSIST_TOOL_TIMEOUT_SECONDS": "mcp.tool_timeout_seconds",
    "SHOPASSIST_CACHE_SOCKET_TIMEOUT": "cache.socket_timeout_seconds",
    "SHOPASSIST_MYSQL_CONNECT_TIMEOUT": "mysql.connect_timeout_seconds",
    "SHOPASSIST_KB_THRESHOLD": "knowledge.score_threshold",
    "SHOPASSIST_KB_DEDUP": "knowledge.dedup_cosine",
}

_ENV_STRINGS.update(
    {
        "SHOPASSIST_KB_ENCODER": "knowledge.encoder",
        "SHOPASSIST_KB_EMBEDDING_MODEL": "knowledge.embedding_model",
        "SHOPASSIST_KB_ENCODER_VERSION": "knowledge.encoder_version",
        "SHOPASSIST_KB_POLICY_PATH": "knowledge.policy_path",
        "SHOPASSIST_KB_PRODUCT_PATH": "knowledge.product_path",
        "SHOPASSIST_KB_INDEX": "knowledge.index_dir",
        "SHOPASSIST_KB_INDEX_VERSION": "knowledge.index_version",
    }
)

_ENV_BOOLS = {
    "SHOPASSIST_OPT_IN_CONFIGURED_MODEL": "opt_in_configured_model",
    "SHOPASSIST_OPT_IN_LOCAL_WEIGHTS": "opt_in_local_weights",
    "SHOPASSIST_JUDGE_ENABLED": "model.judge.enabled",
    "SHOPASSIST_STREAM_FINAL": "model.stream_final",
}


class SettingsError(RuntimeError):
    def __init__(self, code: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


def load_settings(path: str | None = None, overrides: dict | None = None, environ: dict | None = None, *, defaults_only: bool = False) -> dict:
    """Return a fresh settings dict. Callers keep the copy they receive.

    ``defaults_only`` skips the config file and the process environment.
    The browser demo uses that so a developer's local file cannot change the page.
    """
    if defaults_only:
        data = deepcopy(DEFAULTS)
        if overrides:
            _merge(data, overrides, "")
        _validate(data)
        return data
    env = os.environ if environ is None else environ
    data = deepcopy(DEFAULTS)
    file_data = _read_config_file(path, env)
    if file_data:
        if _looks_unified(file_data):
            _merge(data, file_data, "")
        else:
            _merge(data, _from_legacy(file_data), "")
    _apply_env(data, env)
    if overrides:
        _merge(data, overrides, "")
    _validate(data)
    return data


def public_settings(settings: dict | None = None) -> dict:
    return _redact(settings if settings is not None else load_settings())


def legacy_model_dict(settings: dict) -> dict:
    def role(name: str) -> dict:
        source = settings["model"][name]
        return {
            "base_url": source.get("base_url", ""),
            "model": source.get("model", ""),
            "api_key": source.get("api_key", ""),
            "api_key_env": source.get("api_key_env", ""),
            "timeout_seconds": source.get("timeout_seconds", 30),
            "credential_present": bool(source.get("api_key")),
        }

    judge = dict(settings["model"]["judge"])
    judge["credential_present"] = bool(judge.get("api_key"))
    return {
        "mode": settings["mode"],
        "opt_in_configured_model": settings["opt_in_configured_model"],
        "opt_in_local_weights": settings["opt_in_local_weights"],
        "ca": role("ca"),
        "pa": role("pa"),
        "judge": judge,
    }


def _read_config_file(path: str | None, env: dict) -> dict:
    chosen = path or env.get("SHOPASSIST_CONFIG") or ""
    if chosen:
        return _load_json(Path(chosen), "config_unreadable")
    if LOCAL_CONFIG_PATH.is_file():
        return _load_json(LOCAL_CONFIG_PATH, "config_unreadable")
    if LEGACY_CONFIG_PATH.is_file():
        return _load_json(LEGACY_CONFIG_PATH, "local_config_unreadable")
    return {}


def _load_json(path: Path, code: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(code, {"path": path.name}) from exc
    if not isinstance(data, dict):
        raise SettingsError(code, {"path": path.name})
    return data


def _looks_unified(data: dict) -> bool:
    return any(key in data for key in ("loop", "mcp", "context", "paths", "ui", "progress", "recovery"))


def _from_legacy(data: dict) -> dict:
    allowed = {
        "mode",
        "opt_in_configured_model",
        "opt_in_local_weights",
        "ca",
        "pa",
        "judge",
        "store",
        "cache",
        "summary_mode",
        "mysql",
        "redis_url",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise SettingsError("unknown_setting", {"path": unknown[0]})
    mapped: dict = {}
    for key in ("mode", "opt_in_configured_model", "opt_in_local_weights"):
        if key in data:
            mapped[key] = data[key]
    model = {}
    for name in ("ca", "pa", "judge"):
        if isinstance(data.get(name), dict):
            model[name] = data[name]
    if model:
        mapped["model"] = model
    if isinstance(data.get("store"), str):
        mapped["store"] = {"kind": data["store"]}
    if isinstance(data.get("cache"), str):
        mapped["cache"] = {"kind": data["cache"]}
    if data.get("redis_url"):
        mapped.setdefault("cache", {})["redis_url"] = data["redis_url"]
    if data.get("summary_mode"):
        mapped["context"] = {"summary_mode": data["summary_mode"]}
    if isinstance(data.get("mysql"), dict):
        mapped["mysql"] = data["mysql"]
    return mapped


def _merge(base: dict, patch: dict, path: str) -> None:
    if not isinstance(patch, dict):
        raise SettingsError("invalid_setting", {"path": path or "<root>"})
    for key, value in patch.items():
        here = f"{path}.{key}" if path else key
        if key not in base:
            raise SettingsError("unknown_setting", {"path": here})
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise SettingsError("invalid_setting", {"path": here})
            _merge(base[key], value, here)
        else:
            base[key] = value


def _apply_env(data: dict, env: dict) -> None:
    for name, target in _ENV_STRINGS.items():
        value = env.get(name)
        if value:
            _set_path(data, target, value)
    for name, target in _ENV_INTS.items():
        value = env.get(name)
        if value is None or value == "":
            continue
        if str(value).lower() in {"none", "null"}:
            _set_path(data, target, None)
            continue
        try:
            _set_path(data, target, int(value))
        except ValueError as exc:
            raise SettingsError("invalid_setting", {"path": target}) from exc
    for name, target in _ENV_FLOATS.items():
        value = env.get(name)
        if not value:
            continue
        try:
            _set_path(data, target, float(value))
        except ValueError as exc:
            raise SettingsError("invalid_setting", {"path": target}) from exc
    for name, target in _ENV_BOOLS.items():
        value = env.get(name)
        if value is None or value == "":
            continue
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes"}:
            _set_path(data, target, True)
        elif lowered in {"0", "false", "no"}:
            _set_path(data, target, False)
        else:
            raise SettingsError("invalid_setting", {"path": target})


def _set_path(data: dict, path: str, value) -> None:
    cursor = data
    parts = path.split(".")
    for part in parts[:-1]:
        cursor = cursor[part]
    cursor[parts[-1]] = value


def _validate(data: dict) -> None:
    if data["mode"] not in MODES:
        raise SettingsError("unknown_mode", {"mode": str(data["mode"])})
    if data["store"]["kind"] not in STORE_KINDS:
        raise SettingsError("invalid_setting", {"path": "store.kind"})
    if data["cache"]["kind"] not in CACHE_KINDS:
        raise SettingsError("invalid_setting", {"path": "cache.kind"})
    if data["cache"]["fallback"] not in {"", "mysql"}:
        raise SettingsError("invalid_setting", {"path": "cache.fallback"})
    if data["context"]["summary_mode"] not in SUMMARY_MODES:
        raise SettingsError("invalid_setting", {"path": "context.summary_mode"})
    if not isinstance(data["model"]["stream_final"], bool):
        raise SettingsError("invalid_setting", {"path": "model.stream_final"})
    _positive("loop.max_plan_steps", data["loop"]["max_plan_steps"], minimum=1)
    _positive("loop.max_tool_calls", data["loop"]["max_tool_calls"], minimum=1)
    _positive("loop.max_model_calls", data["loop"]["max_model_calls"], minimum=1)
    _positive("loop.max_retries_per_step", data["loop"]["max_retries_per_step"], minimum=0)
    _positive("loop.max_dag_concurrency", data["loop"]["max_dag_concurrency"], minimum=1)
    _positive("loop.max_replans", data["loop"]["max_replans"], minimum=0)
    _positive("loop.max_fallbacks", data["loop"]["max_fallbacks"], minimum=0)
    _positive("loop.repeat_limit", data["loop"]["repeat_limit"], minimum=0)
    _positive("loop.wall_clock_seconds", data["loop"]["wall_clock_seconds"], minimum=0)
    _positive("loop.missing_usage_tokens", data["loop"]["missing_usage_tokens"], minimum=1)
    _positive("loop.chars_per_token", data["loop"]["chars_per_token"], minimum=1)
    if data["loop"]["token_budget"] is not None:
        _positive("loop.token_budget", data["loop"]["token_budget"], minimum=0)
    _positive("mcp.tool_timeout_seconds", data["mcp"]["tool_timeout_seconds"], minimum=0.01)
    _positive("model.ca.timeout_seconds", data["model"]["ca"]["timeout_seconds"], minimum=0.01)
    _positive("model.pa.timeout_seconds", data["model"]["pa"]["timeout_seconds"], minimum=0.01)
    _positive("context.latest_messages", data["context"]["latest_messages"], minimum=1)
    _positive("context.recent_memory_days", data["context"]["recent_memory_days"], minimum=1)
    _positive("context.summary_min_new_messages", data["context"]["summary_min_new_messages"], minimum=1)
    _positive("context.summary_token_budget", data["context"]["summary_token_budget"], minimum=1)
    _positive("context.sync_catchup_messages", data["context"]["sync_catchup_messages"], minimum=0)
    _positive("mysql.port", data["mysql"]["port"], minimum=1)
    _positive("mysql.connect_timeout_seconds", data["mysql"]["connect_timeout_seconds"], minimum=0.01)
    _positive("cache.socket_timeout_seconds", data["cache"]["socket_timeout_seconds"], minimum=0.01)
    _positive("ui.port", data["ui"]["port"], minimum=0)
    _positive("ui.max_body_bytes", data["ui"]["max_body_bytes"], minimum=1)
    _positive("progress.pending_after_ms", data["progress"]["pending_after_ms"], minimum=0)
    _positive("progress.milestone_min_interval_ms", data["progress"]["milestone_min_interval_ms"], minimum=0)
    _positive("recovery.backoff_base_seconds", data["recovery"]["backoff_base_seconds"], minimum=0)
    _positive("recovery.backoff_cap_seconds", data["recovery"]["backoff_cap_seconds"], minimum=0)
    _positive("recovery.jitter_ratio", data["recovery"]["jitter_ratio"], minimum=0)
    if data["recovery"]["jitter_ratio"] > 1 or not isinstance(data["recovery"]["honor_retry_after"], bool):
        raise SettingsError("invalid_setting", {"path": "recovery"})
    knowledge = data["knowledge"]
    _positive("knowledge.policy_count", knowledge["policy_count"], minimum=1)
    _positive("knowledge.product_count", knowledge["product_count"], minimum=1)
    _positive("knowledge.embedding_dim", knowledge["embedding_dim"], minimum=8)
    _positive("knowledge.top_k", knowledge["top_k"], minimum=1)
    _positive("knowledge.max_hits", knowledge["max_hits"], minimum=1)
    _positive("knowledge.dedup_cosine", knowledge["dedup_cosine"], minimum=0)
    if knowledge["dedup_cosine"] > 1:
        raise SettingsError("invalid_setting", {"path": "knowledge.dedup_cosine"})
    if knowledge["max_hits"] > knowledge["top_k"]:
        raise SettingsError("invalid_setting", {"path": "knowledge.max_hits"})
    _positive("knowledge.chunk_chars", knowledge["chunk_chars"], minimum=80)
    _positive("knowledge.score_threshold", knowledge["score_threshold"], minimum=0)
    if knowledge["score_threshold"] > 1:
        raise SettingsError("invalid_setting", {"path": "knowledge.score_threshold"})
    if knowledge["encoder"] not in {"hashing_lexical_baseline", "sentence_transformers"}:
        raise SettingsError("invalid_setting", {"path": "knowledge.encoder"})
    if knowledge["encoder"] == "hashing_lexical_baseline" and knowledge["embedding_model"]:
        raise SettingsError("invalid_setting", {"path": "knowledge.embedding_model"})
    if knowledge["encoder"] == "sentence_transformers" and not knowledge["embedding_model"]:
        raise SettingsError("invalid_setting", {"path": "knowledge.embedding_model"})
    if not isinstance(knowledge["seed"], int) or isinstance(knowledge["seed"], bool):
        raise SettingsError("invalid_setting", {"path": "knowledge.seed"})


def _positive(path: str, value, minimum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise SettingsError("invalid_setting", {"path": path})


def _redact(node):
    if isinstance(node, dict):
        cleaned = {}
        for key, value in node.items():
            if key in _SECRET_KEYS or key.endswith("_password"):
                cleaned[key] = "[redacted]" if value else ""
            elif key == "redis_url" and isinstance(value, str) and "@" in value:
                cleaned[key] = "[redacted]"
            else:
                cleaned[key] = _redact(value)
        return cleaned
    if isinstance(node, list):
        return [_redact(item) for item in node]
    return node
