from __future__ import annotations

import json
import hmac
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from server.store import Store


DEFAULT_DB_PATH = "./data/agentrelay.sqlite3"


class AgentRelayHandler(BaseHTTPRequestHandler):
    store: Store
    auth_identities: dict[str, dict[str, str]] = {}

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
        auth = self.require_auth()
        if auth is None:
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
            agent_id = match.group(1)
            if not self.require_agent(auth, agent_id):
                return
            task = self.store.claim_task(agent_id)
            if not task:
                self.respond_json({"task": None})
                return
            self.respond_json({"task": task})
            return
        self.respond_error(404, "not found")

    def route_post(self) -> None:
        path = clean_path(self.path)
        auth = self.require_auth()
        if auth is None:
            return
        payload = self.read_json()
        if path == "/agentrelay/tasks":
            if not self.require_agent(auth, payload.get("from")):
                return
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
            if not self.require_agent(auth, payload.get("from")):
                return
            task = self.store.submit_artifact(match.group(1), payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_json({"task": task}, status=201)
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/close", path):
            if not self.require_agent(auth, payload.get("closedByAgentId")):
                return
            task = self.store.close_task(match.group(1), payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_json({"task": task})
            return
        if match := re.fullmatch(r"/agentrelay/tasks/([^/]+)/deliveries", path):
            if not self.require_agent(auth, payload.get("deliveredByAgentId")):
                return
            task = self.store.mark_delivery(match.group(1), payload)
            if not task:
                self.respond_error(404, "task not found")
                return
            self.respond_json({"task": task})
            return
        if match := re.fullmatch(r"/agentrelay/workers/([^/]+)/tasks/([^/]+)/thread", path):
            agent_id, task_id = match.groups()
            if not self.require_agent(auth, agent_id):
                return
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

    def require_auth(self) -> dict[str, str] | None:
        if not self.auth_identities:
            return {"username": "", "agent_id": ""}
        authorization = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            self.respond_error(401, "missing bearer token")
            return None
        token = authorization[len(prefix):]
        identity = self.auth_identities.get(token)
        if not identity:
            # Avoid leaking whether a username or agent id exists.
            self.respond_error(401, "invalid bearer token")
            return None
        username = self.headers.get("X-AgentRelay-Username", "")
        agent_id = self.headers.get("X-AgentRelay-Agent-Id", "")
        if username and not hmac.compare_digest(username, identity["username"]):
            self.respond_error(403, "username does not match token")
            return None
        if agent_id and not hmac.compare_digest(agent_id, identity["agent_id"]):
            self.respond_error(403, "agent id does not match token")
            return None
        return identity

    def require_agent(self, auth: dict[str, str], requested_agent_id: Any) -> bool:
        if not self.auth_identities:
            return True
        if not isinstance(requested_agent_id, str) or not requested_agent_id:
            self.respond_error(400, "missing agent id for authenticated action")
            return False
        if not hmac.compare_digest(auth["agent_id"], requested_agent_id):
            self.respond_error(403, "token cannot act as requested agent")
            return False
        return True

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
    clean = parsed.path.rstrip("/") or "/"
    if clean == "/agentrelay/api":
        return "/agentrelay"
    if clean.startswith("/agentrelay/api/"):
        return "/agentrelay/" + clean[len("/agentrelay/api/"):]
    return clean


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
    AgentRelayHandler.auth_identities = load_auth_identities()
    return ThreadingHTTPServer((host, port), AgentRelayHandler)


def load_auth_identities() -> dict[str, dict[str, str]]:
    auth_file = os.environ.get("AGENTRELAY_AUTH_FILE", "")
    if auth_file:
        with open(auth_file, "r", encoding="utf-8") as handle:
            raw_identities = json.load(handle)
        if not isinstance(raw_identities, list):
            raise ValueError("AGENTRELAY_AUTH_FILE must contain a JSON array")
        return parse_auth_identities(raw_identities)

    raw_tokens = os.environ.get("AGENTRELAY_TOKENS", "")
    if not raw_tokens:
        return {}
    identities = []
    for entry in raw_tokens.split(","):
        if not entry.strip():
            continue
        parts = entry.split(":", 2)
        if len(parts) != 3:
            raise ValueError("AGENTRELAY_TOKENS entries must be username:agent_id:token")
        username, agent_id, token = parts
        identities.append({"username": username, "agent_id": agent_id, "token": token})
    return parse_auth_identities(identities)


def parse_auth_identities(raw_identities: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    identities: dict[str, dict[str, str]] = {}
    for identity in raw_identities:
        username = str(identity.get("username", "")).strip()
        agent_id = str(identity.get("agent_id", "")).strip()
        token = str(identity.get("token", "")).strip()
        if not username or not agent_id or not token:
            raise ValueError("each auth identity needs username, agent_id, and token")
        identities[token] = {"username": username, "agent_id": agent_id}
    return identities


def main() -> None:
    server = create_server()
    host, port = server.server_address
    print(f"AgentRelay listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
