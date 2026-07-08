from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8802/agentrelay/api"
HEADERS = {
    "Authorization": "Bearer zac-token",
    "X-AgentRelay-Agent-Id": "zac-agent",
    "X-AgentRelay-Username": "zac",
    "X-AgentRelay-Envelope": "v0.3",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_HOST": "127.0.0.1",
                "AGENTRELAY_PORT": "8802",
                "AGENTRELAY_DB_PATH": f"{tmpdir}/agentrelay-protocol-negotiation.sqlite3",
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

            health = get_json(f"{BASE_URL}/health")
            protocol = health["protocol"]
            if protocol["version"] != "agent-collab-v0.3":
                raise AssertionError(f"health did not publish current protocol: {protocol}")
            if not protocol["schema_digest"].startswith("sha256:"):
                raise AssertionError("health protocol digest missing sha256 prefix")

            manifest = get_json(f"{BASE_URL}/protocols/current")
            if manifest["urls"]["bundle"] != "https://example.test/agentrelay/api/protocols/agent-collab/v0.3/bundle":
                raise AssertionError("manifest bundle URL should use public base URL")
            if "agent-collab-v0.2" not in manifest["accepted_versions"]:
                raise AssertionError("manifest should publish the compatibility window")
            if "dynamic_protocol_bundle_v0.1" not in manifest["required_client_capabilities"]:
                raise AssertionError("manifest should declare the dynamic protocol bundle capability")

            bundle = get_json(f"{BASE_URL}/protocols/agent-collab/v0.3/bundle")
            if "task-create.schema.json" not in bundle["schemas"]:
                raise AssertionError("protocol bundle missing task-create schema")
            if "meeting-task-create.json" not in bundle["examples"]:
                raise AssertionError("protocol bundle missing meeting task create example")
            if "AgentRelay Protocol v0.3" not in bundle["docs"]["protocol-v03.md"]:
                raise AssertionError("protocol bundle missing protocol doc")

            valid = post_json(
                f"{BASE_URL}/protocols/validate",
                {"operation": "task_create", "payload": valid_task_payload()},
                HEADERS,
            )
            if not valid["data"]["valid"]:
                raise AssertionError("protocol validate should accept current v0.3 payload")

            rejected = post_json(
                f"{BASE_URL}/protocols/validate",
                {"operation": "task_create", "payload": {**valid_task_payload(), "protocol_version": "agent-collab-v0.1"}},
                HEADERS,
                expected_status=426,
            )
            assert_protocol_negotiation_error(rejected, "protocol_patch_required")

            create_rejected = post_json(
                f"{BASE_URL}/tasks",
                {**valid_task_payload(), "protocol_version": "agent-collab-v0.1"},
                HEADERS,
                expected_status=426,
            )
            assert_protocol_negotiation_error(create_rejected, "protocol_patch_required")

            future_rejected = post_json(
                f"{BASE_URL}/tasks",
                {**valid_task_payload(), "protocol_version": "agent-collab-v9.0"},
                HEADERS,
                expected_status=426,
            )
            assert_protocol_negotiation_error(future_rejected, "client_upgrade_required")

            print(json.dumps({"ok": True, "protocol": protocol["version"], "digest": protocol["schema_digest"]}, indent=2))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def valid_task_payload() -> dict:
    return {
        "protocol_version": "agent-collab-v0.3",
        "idempotency_key": "protocol-negotiation-valid-create",
        "task_type": "agent.task",
        "subject": "Protocol negotiation smoke task",
        "requester_agent_id": "zac-agent",
        "target_agent_id": "frank-agent",
        "requesterThreadId": "protocol-negotiation-thread",
        "done_criteria": "Frank agent replies with ACK.",
        "completion_owner_agent_id": "zac-agent",
        "pending_on_agent_id": "frank-agent",
        "next_action": "Frank agent should reply with ACK.",
        "message": {
            "actor_agent_id": "zac-agent",
            "intent": "connectivity_check",
            "parts": [{"kind": "text", "text": "Please ACK this protocol check."}],
        },
    }


def assert_protocol_negotiation_error(payload: dict, code: str) -> None:
    if payload.get("ok") is not False:
        raise AssertionError(f"expected error envelope: {payload}")
    error = payload["error"]
    if error["code"] != code:
        raise AssertionError(f"expected {code}, got {error}")
    detail = error.get("detail") or {}
    if detail.get("server_protocol", {}).get("version") != "agent-collab-v0.3":
        raise AssertionError(f"error should include server protocol: {error}")
    if not detail.get("upgrade", {}).get("bundle_url"):
        raise AssertionError(f"error should include bundle URL: {error}")


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


def post_json(url: str, payload: dict, headers: dict[str, str], expected_status: int = 200) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            status = response.status
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8")
    if status != expected_status:
        raise AssertionError(f"expected HTTP {expected_status}, got {status}: {body}")
    return json.loads(body)


if __name__ == "__main__":
    main()
