"""Runtime mode and optional model configuration.

Mock mode is the default and does not read credentials for a call.
Configured mode is opt-in. Credential values stay in the process environment
or a gitignored local file and are not returned to callers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from shopassist.redact import redact_text

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONFIG_PATH = REPO_ROOT / "config" / "local_model.json"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config" / "model_mode.example.json"

MODES = ("mock", "configured_endpoint", "local_weights")
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


class ConfigError(RuntimeError):
    def __init__(self, code: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(code)

    def public(self) -> dict:
        text, _ = redact_text(json.dumps(self.details, sort_keys=True))
        try:
            safe_details = json.loads(text)
        except json.JSONDecodeError:
            safe_details = {"note": "details_redacted"}
        return {
            "error_code": self.code,
            "evaluation_kind": None,
            "mock_fallback": False,
            "details": safe_details,
        }


def _blank_role(env_name: str) -> dict:
    return {
        "base_url": "",
        "model": "",
        "api_key": "",
        "api_key_env": env_name,
        "timeout_seconds": 30,
    }


def default_config() -> dict:
    return {
        "mode": "mock",
        "opt_in_configured_model": False,
        "opt_in_local_weights": False,
        "ca": _blank_role("SHOPASSIST_CA_API_KEY"),
        "pa": _blank_role("SHOPASSIST_PA_API_KEY"),
        "judge": {
            "enabled": False,
            "base_url": "",
            "model": "",
            "api_key_env": "SHOPASSIST_JUDGE_API_KEY",
            "api_key": "",
        },
    }


def _load_local_file() -> dict:
    if not LOCAL_CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError("local_config_unreadable", {"path": "config/local_model.json"}) from exc
    if not isinstance(data, dict):
        raise ConfigError("local_config_unreadable", {"path": "config/local_model.json"})
    return data


def _merge_role(base: dict, patch: dict | None, env_url: str, env_model: str, env_key: str) -> dict:
    role = dict(base)
    for key in ("base_url", "model", "api_key_env", "timeout_seconds", "api_key"):
        if patch and patch.get(key) not in (None, ""):
            role[key] = patch[key]
    if os.environ.get(env_url):
        role["base_url"] = os.environ[env_url]
    if os.environ.get(env_model):
        role["model"] = os.environ[env_model]
    env_value = os.environ.get(env_key) or ""
    if env_value:
        role["api_key"] = env_value
        role["api_key_env"] = env_key
    role["credential_present"] = bool(role.get("api_key"))
    return role


def load_config() -> dict:
    from shopassist.settings import SettingsError, legacy_model_dict, load_settings

    try:
        return legacy_model_dict(load_settings())
    except SettingsError as exc:
        raise ConfigError(exc.code, exc.details) from exc


def _endpoint_scope(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in LOCAL_HOSTS:
        return "local"
    return "remote"


def validate_role(role_name: str, role: dict, *, require_key_for_remote: bool = True) -> list[str]:
    missing = []
    url = (role.get("base_url") or "").strip()
    model = (role.get("model") or "").strip()
    if not url:
        missing.append(f"{role_name}.base_url")
    if not model:
        missing.append(f"{role_name}.model")
    if url:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            raise ConfigError(
                "credentials_in_url_not_allowed",
                {"role": role_name, "endpoint_scope": "rejected"},
            )
        if parsed.scheme not in {"http", "https"}:
            missing.append(f"{role_name}.base_url_scheme")
        elif require_key_for_remote and _endpoint_scope(url) == "remote" and not role.get("api_key"):
            missing.append(f"{role_name}.api_key ({role.get('api_key_env')})")
    return missing


def require_configured_ready(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    if cfg["mode"] != "configured_endpoint":
        raise ConfigError("configured_mode_not_selected", {"mode": cfg["mode"]})
    if not cfg.get("opt_in_configured_model"):
        raise ConfigError(
            "opt_in_required",
            {
                "hint": "Set SHOPASSIST_OPT_IN_CONFIGURED_MODEL=yes and SHOPASSIST_MODE=configured_endpoint.",
                "mock_fallback": False,
            },
        )
    missing = validate_role("ca", cfg["ca"]) + validate_role("pa", cfg["pa"])
    if missing:
        raise ConfigError(
            "configured_model_not_ready",
            {
                "missing": missing,
                "hint": "Set separate CA and PA endpoint, model, and credential environment variables, or leave mode as mock.",
                "mock_fallback": False,
            },
        )
    return cfg


def public_capabilities(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    def role_view(name: str, role: dict) -> dict:
        missing = []
        try:
            missing = validate_role(name, role)
        except ConfigError as exc:
            missing = [exc.code]
        return {
            "model_configured": bool(role.get("model")),
            "endpoint_configured": bool(role.get("base_url")),
            "credential_present": bool(role.get("credential_present")),
            "endpoint_scope": _endpoint_scope(role["base_url"]) if role.get("base_url") else "unset",
            "missing": missing,
        }

    configured_ready = False
    configured_error = None
    probe = dict(cfg)
    probe["mode"] = "configured_endpoint"
    try:
        require_configured_ready(probe)
        configured_ready = True
    except ConfigError as exc:
        configured_error = exc.code
    return {
        "default_mode": "mock",
        "selected_mode": cfg["mode"],
        "mock": {"available": True, "network": False, "credentials_used": False},
        "configured_endpoint": {
            "opt_in": bool(cfg.get("opt_in_configured_model")),
            "ready": configured_ready,
            "error_code": configured_error,
            "ca": role_view("ca", cfg["ca"]),
            "pa": role_view("pa", cfg["pa"]),
        },
        "local_weights": {
            "opt_in": bool(cfg.get("opt_in_local_weights")),
            "ready": False,
            "reason": "local weight files are not loaded by the default runtime",
        },
        "credentials_used_by_mock": False,
    }


def assert_mock_runtime(mode: str) -> None:
    if mode != "mock":
        raise ConfigError(
            "mock_runtime_required",
            {"mode": mode, "hint": "This path refuses to run a configured model."},
        )
