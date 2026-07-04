from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8797/agentrelay/api"
HEADERS = {
    "Authorization": "Bearer frank-token",
    "X-AgentRelay-Agent-Id": "frank-agent",
    "X-AgentRelay-Username": "frank",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_HOST": "127.0.0.1",
                "AGENTRELAY_PORT": "8797",
                "AGENTRELAY_DB_PATH": f"{tmpdir}/agentrelay-agent-card.sqlite3",
                "AGENTRELAY_TOKENS": "zac:zac-agent:zac-token,frank:frank-agent:frank-token",
                "AGENTRELAY_PUBLIC_BASE_URL": "https://example.test/agentrelay",
            }
        )
        proc = subprocess.Popen(
            ["python3", "-m", "server.app"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            wait_for_health()
            card = get_json(f"{BASE_URL}/agents/frank-agent/card", HEADERS)
            if card["protocolVersion"] != "agentrelay-agent-card-v0.3":
                raise AssertionError("agent card protocolVersion did not upgrade")
            if card["a2aProtocolVersion"] != "0.3":
                raise AssertionError("agent card should declare A2A protocol version")
            if card["url"] != "https://example.test/agentrelay/api/a2a/frank-agent":
                raise AssertionError("agent card url should use public base URL")
            if not card["capabilities"]["pushNotifications"]:
                raise AssertionError("agent card should advertise push notification support")
            if not card["capabilities"]["stateTransitionHistory"]:
                raise AssertionError("agent card should advertise state history support")
            if card["capabilities"]["extendedAgentCard"]:
                raise AssertionError("extendedAgentCard should remain false until implemented")
            if "bearerAuth" not in card["securitySchemes"]:
                raise AssertionError("agent card should describe bearer auth")
            relay = card["agentRelay"]
            if relay["agent_id"] != "frank-agent":
                raise AssertionError("agentRelay metadata missing agent id")
            if "meeting.schedule" not in relay["accepted_task_types"]:
                raise AssertionError("agent card should include accepted task types")
            if "agent:frank-agent:events:ack" not in relay["scopes"]:
                raise AssertionError("agent card should include event ack scope")
            if relay["human_approval_policy"]["private_owner_agent_conversation"] != "not_relayed_by_default":
                raise AssertionError("approval policy should keep private owner-agent conversation local")
            if not any(skill["id"] == "meeting-coordination" for skill in card["skills"]):
                raise AssertionError("agent card should include meeting coordination skill")

            cards = get_json(f"{BASE_URL}/agents/cards", HEADERS)
            ids = {item["agentRelay"]["agent_id"] for item in cards["agentCards"]}
            if {"zac-agent", "frank-agent"} - ids:
                raise AssertionError("agent cards endpoint should list all seed agents")

            mapping = get_json(f"{BASE_URL}/agents/frank-agent/a2a-map", HEADERS)
            if mapping["compatibility"]["full_a2a_runtime"]:
                raise AssertionError("A2A mapping should not claim full runtime compatibility")
            if not mapping["compatibility"]["agent_card_discovery"]:
                raise AssertionError("A2A mapping should claim card discovery support")
            if mapping["operation_map"]["message/send"]["agentrelay"] != "POST /agentrelay/api/tasks":
                raise AssertionError("A2A message/send should map to task create")

            task_event_schema = get_json("http://127.0.0.1:8797/agentrelay/schemas/task-event.schema.json")
            if task_event_schema["title"] != "AgentRelay Protocol v0.3 Task Event":
                raise AssertionError("task-event schema endpoint did not serve the public schema")
            schema_index = get_text("http://127.0.0.1:8797/agentrelay/schemas/")
            if "AgentRelay Protocol v0.3 Schemas" not in schema_index:
                raise AssertionError("schema catalog endpoint did not serve README.md")

            print(json.dumps({"ok": True, "agentId": relay["agent_id"], "skills": len(card["skills"])}, indent=2))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def wait_for_health() -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            payload = get_json(f"{BASE_URL}/health")
            if payload.get("ok"):
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def get_text(url: str, headers: dict[str, str] | None = None) -> str:
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.read().decode("utf-8")


if __name__ == "__main__":
    main()
