from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from server.store import Store


DEFAULT_DB_PATH = "./data/agentrelay.sqlite3"


class AgentRelayHandler(BaseHTTPRequestHandler):
    store: Store

    def do_GET(self) -> None:
        try:
            self.route_get()
        except ValueError as exc:
            self.respond_error(400, str(exc))
        except Exception as exc:
            self.respond_error(500, f"internal error: {exc}")

    def do_POST(self) -> None:
        try:
            self.route_post()
        except ValueError as exc:
            self.respond_error(400, str(exc))
        except Exception as exc:
            self.respond_error(500, f"internal error: {exc}")

    def route_get(self) -> None:
        path = clean_path(self.path)
        if path == "/health":
            self.respond_json({"ok": True, "service": "agentrelay"})
            return
        if path == "/agentrelay/health":
            self.respond_json({"ok": True, "service": "agentrelay"})
            return
        if path == "/agentrelay/agents":
            self.respond_json({"agents": self.store.list_agents()})
            return
        if match := re.fullmatch(r"/agentrelay/agents/([^/]+)/card", path):
            agent_id = match.group(1)
            agent = self.store.get_agent(agent_id)
            if not agent:
                self.respond_error(404, "agent not found")
                return
            self.respond_json(agent_card(agent))
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)", path):
            task = self.store.get_task(match.group(1))
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_json({"task": task})
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/events", path):
            events = self.store.get_events(match.group(1))
            if events is None:
                self.respond_error(404, "task not found")
                return
            self.respond_json({"events": events})
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/claim", path):
            task = self.store.claim_task(match.group(1))
            if not task:
                self.respond_json({"task": None})
                return
            self.respond_json({"task": task})
            return
        self.respond_error(404, "not found")

    def route_post(self) -> None:
        path = clean_path(self.path)
        payload = self.read_json()
        if path == "/agentrelay/tasks":
            task = self.store.create_task(payload)
            self.respond_json({"task": task}, status=201)
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/status", path):
            status = payload.get("status")
            if not status:
                raise ValueError("missing required field: status")
            task = self.store.update_status(match.group(1), status, payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_json({"task": task})
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/artifacts", path):
            task = self.store.submit_artifact(match.group(1), payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_json({"task": task}, status=201)
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/tasks/([^/]+)/thread", path):
            agent_id, task_id = match.groups()
            thread_id = payload.get("threadId")
            if not thread_id:
                raise ValueError("missing required field: threadId")
            task = self.store.set_thread(agent_id, task_id, thread_id)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_json({"task": task})
            return
        self.respond_error(404, "not found")

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def respond_json(self, payload: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def respond_error(self, status: int, message: str) -> None:
        self.respond_json({"error": message}, status=status)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def clean_path(path: str) -> str:
    parsed = urlparse(path)
    return parsed.path.rstrip("/") or "/"


def agent_card(agent: dict[str, Any]) -> dict[str, Any]:
    agent_id = agent["agent_id"]
    return {
        "protocolVersion": "agentrelay-phase1-a2a-shaped",
        "name": agent["name"],
        "description": agent["description"],
        "url": f"/agentrelay/agents/{agent_id}/a2a",
        "provider": {"organization": agent["owner"]},
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "skills": [
            {
                "id": "meeting-coordination",
                "name": "Meeting coordination",
                "description": "Ask the human owner for availability and return approved candidate times.",
            }
        ],
    }


def create_server() -> ThreadingHTTPServer:
    host = os.environ.get("AGENTRELAY_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENTRELAY_PORT", "8787"))
    db_path = os.environ.get("AGENTRELAY_DB_PATH", DEFAULT_DB_PATH)
    store = Store(db_path)
    AgentRelayHandler.store = store
    return ThreadingHTTPServer((host, port), AgentRelayHandler)


def main() -> None:
    server = create_server()
    host, port = server.server_address
    print(f"AgentRelay listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

