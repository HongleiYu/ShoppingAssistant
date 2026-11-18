"""Local chat UI and JSON API. Binds to loopback by default and stores nothing on disk."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from shopassist.cards import all_cards, public_gallery
from shopassist.config import ConfigError, load_config, public_capabilities, require_configured_ready
from shopassist.harness import evaluate_card, public_result
from shopassist.redact import scrub_object
from shopassist.sandbox import SessionSandbox
from shopassist.session_runner import AgentSession

STATIC_ROOT = Path(__file__).resolve().parent / "static"
_MAX_BODY = 100_000


class ChatApp:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()

    def create(self, mode: str, card_id: str | None = None) -> dict:
        clients = None
        if mode == "configured_endpoint":
            cfg = load_config()
            cfg["mode"] = "configured_endpoint"
            require_configured_ready(cfg)
            from shopassist.model_client import ChatCompletionsClient

            clients = {
                "ca": ChatCompletionsClient(
                    base_url=cfg["ca"]["base_url"],
                    model=cfg["ca"]["model"],
                    api_key=cfg["ca"].get("api_key") or "",
                    timeout=float(cfg["ca"].get("timeout_seconds") or 30),
                ),
                "pa": ChatCompletionsClient(
                    base_url=cfg["pa"]["base_url"],
                    model=cfg["pa"]["model"],
                    api_key=cfg["pa"].get("api_key") or "",
                    timeout=float(cfg["pa"].get("timeout_seconds") or 30),
                ),
            }
        elif mode != "mock":
            raise ConfigError("unknown_mode", {"mode": mode, "mock_fallback": False})
        card = _card(card_id) if card_id else None
        sandbox = SessionSandbox.start(
            seed=0 if card is None else card["seed"],
            faults=[] if card is None else card["faults"],
        )
        session = AgentSession(
            sandbox,
            session_id=f"ui-{len(self.sessions)+1}",
            mode=mode,
            model_clients=clients,
        )
        record = {"session": session, "card_id": card_id, "lock": threading.Lock()}
        with self.lock:
            self.sessions[session.session_id] = record
        kind = "configured_model" if mode == "configured_endpoint" else "mock_demo"
        return {
            "session_id": session.session_id,
            "evaluation_kind": kind,
            "mode": mode,
            "credential_present": bool(getattr(session, "_credentials_used", False)),
        }

    def message(self, session_id: str, text: str) -> dict:
        record = self._record(session_id)
        with record["lock"]:
            turn = record["session"].user_message(text)
            kind = "configured_model" if record["session"].mode == "configured_endpoint" else "mock_demo"
            payload = {
                "session_id": session_id,
                "evaluation_kind": kind,
                "mode": record["session"].mode,
                "user_text": text,
                "reply_text": turn["reply_text"],
                "route": turn["route"],
                "route_reason_code": turn["route_reason_code"],
                "planner_invoked": turn["planner_invoked"],
                "skills": turn["skills"],
                "tools": [
                    {"tool": tool.get("tool"), "status": tool.get("status"), "evidence_id": tool.get("evidence_id")}
                    for tool in turn["tools"]
                ],
                "recovery": turn["recovery"],
                "limitations": turn["limitations"],
                "evidence": [
                    {
                        "evidence_id": item.get("evidence_id"),
                        "tool_name": item.get("tool_name"),
                        "status": item.get("status"),
                        "untrusted_text": item.get("untrusted_text") or [],
                    }
                    for item in turn["evidence"]
                ],
            }
        cleaned, count = scrub_object(payload)
        cleaned["redaction_count"] = count
        return cleaned

    def run_case(self, card_id: str, session_id: str) -> dict:
        card = _card(card_id)
        if card is None:
            raise KeyError(card_id)
        record = self._record(session_id)
        session = record["session"]
        if session.mode == "configured_endpoint" and not session.model_clients:
            raise ConfigError("configured_model_not_ready", {"missing": ["model_client"], "mock_fallback": False})
        result = public_result(
            evaluate_card(card, mode=session.mode, model_clients=session.model_clients)
        )
        cleaned, count = scrub_object(result)
        cleaned["redaction_count"] = count
        return cleaned

    def reset(self, session_id: str) -> dict:
        record = self._record(session_id)
        with record["lock"]:
            record["session"].reset()
        kind = "configured_model" if record["session"].mode == "configured_endpoint" else "mock_demo"
        return {"session_id": session_id, "reset": True, "evaluation_kind": kind, "mode": record["session"].mode}

    def close(self, session_id: str) -> None:
        with self.lock:
            record = self.sessions.pop(session_id, None)
        if record:
            record["session"].sandbox.close()

    def close_all(self) -> None:
        with self.lock:
            records = list(self.sessions.values())
            self.sessions.clear()
        for record in records:
            record["session"].sandbox.close()

    def _record(self, session_id: str) -> dict:
        with self.lock:
            if session_id not in self.sessions:
                raise KeyError(session_id)
            return self.sessions[session_id]


def _card(card_id: str | None):
    if not card_id:
        return None
    for card in all_cards():
        if card["card_id"] == card_id:
            return card
    return None


def make_server(host: str = "127.0.0.1", port: int = 0, app: ChatApp | None = None):
    application = app or ChatApp()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                return self._send(200, {"status": "ok", "default_mode": "mock"})
            if parsed.path == "/api/capabilities":
                return self._send(200, public_capabilities())
            if parsed.path == "/api/scenarios":
                return self._send(200, {"scenarios": public_gallery()})
            if parsed.path == "/" or parsed.path == "/index.html":
                return self._static("index.html")
            if parsed.path.startswith("/static/"):
                return self._static(parsed.path[len("/static/") :])
            return self._send(404, {"error_code": "not_found"})

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY:
                return self._send(413, {"error_code": "body_too_large"})
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return self._send(400, {"error_code": "bad_json"})
            try:
                if parsed.path == "/api/sessions":
                    return self._send(200, application.create(body.get("mode") or "mock", body.get("card_id")))
                if parsed.path.endswith("/message") and parsed.path.startswith("/api/sessions/"):
                    session_id = parsed.path.split("/")[3]
                    return self._send(200, application.message(session_id, str(body.get("text") or "")))
                if parsed.path.endswith("/run") and parsed.path.startswith("/api/sessions/"):
                    session_id = parsed.path.split("/")[3]
                    return self._send(200, application.run_case(str(body.get("card_id") or ""), session_id))
                if parsed.path.endswith("/reset") and parsed.path.startswith("/api/sessions/"):
                    session_id = parsed.path.split("/")[3]
                    return self._send(200, application.reset(session_id))
            except ConfigError as exc:
                return self._send(409, exc.public())
            except KeyError:
                return self._send(404, {"error_code": "not_found"})
            except Exception:  # noqa: BLE001
                return self._send(500, {"error_code": "request_failed", "mock_fallback": False})
            return self._send(404, {"error_code": "not_found"})

        def _send(self, status: int, payload: dict) -> None:
            body, _ = scrub_object(payload)
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _static(self, relative: str) -> None:
            target = (STATIC_ROOT / relative).resolve()
            if not str(target).startswith(str(STATIC_ROOT.resolve())) or not target.is_file():
                self._send(404, {"error_code": "not_found"})
                return
            kind = "text/plain; charset=utf-8"
            if target.suffix == ".html":
                kind = "text/html; charset=utf-8"
            elif target.suffix == ".css":
                kind = "text/css; charset=utf-8"
            elif target.suffix == ".js":
                kind = "text/javascript; charset=utf-8"
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer((host, port), Handler)
    server.app = application  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Local shopping assistant chat")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"http://{args.host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    finally:
        server.app.close_all()


if __name__ == "__main__":
    main()
