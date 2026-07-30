from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.protocol_v05 import PROTOCOL_V05
from server.protocol_v06 import PROTOCOL_V06
from server.app import validate_protocol_drain_stores
from server.store_v05 import V05Store
from server.store_v06 import V06Store


PORT = 8816
BASE = f"http://127.0.0.1:{PORT}/agentrelay/api"
REQUESTER = "zac-agent"
TARGET = "vivi-agent"
HEADERS = {
    REQUESTER: {
        "Authorization": "Bearer requester-token",
        "X-AgentRelay-Agent-Id": REQUESTER,
    },
    TARGET: {
        "Authorization": "Bearer target-token",
        "X-AgentRelay-Agent-Id": TARGET,
    },
}


def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        v05_path = root / "v05.sqlite3"
        v06_path = root / "v06.sqlite3"
        v05_store = seed_store(V05Store(str(v05_path)), [PROTOCOL_V05, PROTOCOL_V06])
        v06_store = seed_store(V06Store(str(v06_path)), [PROTOCOL_V06, PROTOCOL_V05])
        legacy = create_delivered_v05_task(v05_store)
        process = start_server(root / "legacy.sqlite3", v05_path, v06_path)
        try:
            wait_health()
            run_flow(legacy, v05_store, v06_store)
        finally:
            stop_server(process)
        assert_overlapping_task_ids_fail_closed(v05_store, v06_store, legacy["task"]["task_id"])
    print("protocol upgrade drain smoke passed")


def seed_store(store, capabilities: list[str]):
    for agent_id in (REQUESTER, TARGET):
        store.upsert_agent(
            agent_id,
            name=agent_id,
            owner=agent_id,
            enabled=True,
            protocol_capabilities=capabilities,
        )
    return store


def create_delivered_v05_task(store: V05Store) -> dict:
    listeners = {}
    for agent_id in (REQUESTER, TARGET):
        listener = store.register_listener(
            agent_id,
            listener_instance_id=f"listener-{agent_id}-v05-drain",
            client_version="0.5.1",
            workspace_version="2",
            transport="websocket",
        )
        store.publish_readiness(
            agent_id,
            listener_instance_id=listener["listener_instance_id"],
            readiness_epoch=listener["readiness_epoch"],
            ready=True,
        )
        listeners[agent_id] = listener
    detail = store.create_task(
        {
            "protocol_version": PROTOCOL_V05,
            "idempotency_key": "seed-v05-open",
            "requester_agent_id": REQUESTER,
            "target_agent_id": TARGET,
            "done_criteria": "reply after Server activates v0.6",
            "message": {"subject": "drain", "parts": [{"kind": "text", "text": "reply"}]},
        }
    )
    task = detail["task"]
    event = store.claim_due_event(TARGET)
    assert event is not None
    return store.ack_message(
        TARGET,
        {
            "task_id": task["task_id"],
            "event_id": event["event_id"],
            "message_id": task["current_message_id"],
            "turn_sequence": task["turn_sequence"],
            "expected_task_version": task["task_version"],
            "idempotency_key": "seed-v05-ack",
            "listener_instance_id": listeners[TARGET]["listener_instance_id"],
            "readiness_epoch": listeners[TARGET]["readiness_epoch"],
        },
    )


def assert_overlapping_task_ids_fail_closed(
    v05_store: V05Store,
    v06_store: V06Store,
    v05_task_id: str,
) -> None:
    with v06_store.connect() as conn:
        v06_task_id = conn.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()["task_id"]
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "UPDATE tasks SET task_id = ? WHERE task_id = ?",
            (v05_task_id, v06_task_id),
        )
    try:
        validate_protocol_drain_stores(v05_store, v06_store)
    except ValueError as exc:
        assert "overlapping Task IDs" in str(exc)
    else:
        raise AssertionError("protocol drain accepted overlapping Task IDs")


def run_flow(legacy: dict, v05_store: V05Store, v06_store: V06Store) -> None:
    health = request("GET", "/health", None, {}, 200)
    assert health["protocol"]["version"] == PROTOCOL_V06
    assert health["protocol_drain"]["enabled"] is True
    summary = request(
        "GET", "/admin/api/summary", None,
        {"Authorization": "Bearer admin-token"}, 200,
    )
    assert PROTOCOL_V05 in summary["protocol_drain"]["protocols"]

    task = legacy["task"]
    fetched = request("GET", f"/tasks/{task['task_id']}", None, HEADERS[TARGET], 200)
    assert fetched["task"]["protocol_version"] == PROTOCOL_V05

    runtime_headers = {
        **HEADERS[TARGET],
        "X-AgentRelay-Task-Protocol": PROTOCOL_V05,
        "X-AgentRelay-Bundle-Revision": "5",
        "X-AgentRelay-Bundle-Digest": f"sha256:{'a' * 64}",
        "X-AgentRelay-Adapter-Contract": "2",
        "X-AgentRelay-Runtime-Version": "2.0.0",
    }
    replied = request(
        "POST",
        f"/tasks/{task['task_id']}/messages",
        {
            "actor_agent_id": TARGET,
            "message_id": task["current_message_id"],
            "turn_sequence": task["turn_sequence"],
            "expected_task_version": task["task_version"],
            "idempotency_key": "reply-v05-after-v06",
            "parts": [{"kind": "text", "text": "v0.5 reply remains valid"}],
        },
        runtime_headers,
        201,
    )
    assert replied["task"]["protocol_version"] == PROTOCOL_V05
    with v05_store.connect() as conn:
        audit = conn.execute(
            "SELECT payload_json FROM task_audit_events "
            "WHERE task_id = ? AND event_type = 'protocol.client_runtime' "
            "ORDER BY created_at DESC LIMIT 1",
            (task["task_id"],),
        ).fetchone()
    assert audit is not None
    assert json.loads(audit["payload_json"])["trust"] == "client_reported"

    mismatch = request(
        "POST",
        f"/tasks/{task['task_id']}/complete",
        {
            "actor_agent_id": TARGET,
            "message_id": replied["task"]["current_message_id"],
            "turn_sequence": replied["task"]["turn_sequence"],
            "expected_task_version": replied["task"]["task_version"],
            "idempotency_key": "wrong-runtime",
            "completed_against_message_id": replied["task"]["current_message_id"],
        },
        {**HEADERS[TARGET], "X-AgentRelay-Task-Protocol": PROTOCOL_V06},
        409,
    )
    assert mismatch["code"] == "INVALID_TASK_TRANSITION"
    assert mismatch["detail"]["retryable_with_stable_tool"] is True

    before_tasks, before_idempotency = mutation_counts(v06_store)
    legacy_client_create = request(
        "POST",
        "/tasks",
        {
            "protocol_version": PROTOCOL_V05,
            "idempotency_key": "old-client-v05-after-v06",
            "requester_agent_id": REQUESTER,
            "target_agent_id": TARGET,
            "done_criteria": "must be rejected",
            "message": {"subject": "old", "parts": [{"kind": "text", "text": "old"}]},
        },
        HEADERS[REQUESTER],
        426,
    )
    assert legacy_client_create["error"]["code"] == "client_upgrade_required"
    assert "deterministic_semantic_retry_v1" in legacy_client_create["error"]["detail"]["upgrade"]["required_client_capabilities"]

    patchable_create = request(
        "POST",
        "/tasks",
        {
            "protocol_version": PROTOCOL_V05,
            "idempotency_key": "capable-client-v05-after-v06",
            "requester_agent_id": REQUESTER,
            "target_agent_id": TARGET,
            "done_criteria": "must be deterministically rebuilt",
            "message": {"subject": "old", "parts": [{"kind": "text", "text": "old"}]},
        },
        {
            **HEADERS[REQUESTER],
            "X-AgentRelay-Runtime-Capabilities": "deterministic_semantic_retry_v1",
        },
        426,
    )
    negotiation_error = patchable_create["error"]
    assert negotiation_error["type"] == "protocol_negotiation"
    assert negotiation_error["code"] == "protocol_patch_required"
    detail = negotiation_error["detail"]
    assert detail["server_protocol"]["version"] == PROTOCOL_V06
    assert detail["upgrade"]["bundle_url"].endswith("/protocols/agent-collab/v0.6/bundle")
    assert "task_create" in detail["redraft_policy"]["safe_to_auto_redraft"]
    assert detail["retry_policy"] == {
        "max_automatic_retries": 1,
        "preserve_idempotency_key": True,
    }
    assert mutation_counts(v06_store) == (before_tasks, before_idempotency)

    current = request(
        "POST",
        "/tasks",
        {
            "protocol_version": PROTOCOL_V06,
            "idempotency_key": "new-v06",
            "requester_agent_id": REQUESTER,
            "target_agent_id": TARGET,
            "done_criteria": "new Tasks use v0.6",
            "message": {"subject": "new", "parts": [{"kind": "text", "text": "new"}]},
        },
        {**HEADERS[REQUESTER], "X-AgentRelay-Task-Protocol": PROTOCOL_V06},
        201,
    )
    assert current["task"]["protocol_version"] == PROTOCOL_V06


def mutation_counts(store: V06Store) -> tuple[int, int]:
    with store.connect() as conn:
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        idempotency_count = conn.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0]
    return task_count, idempotency_count


def start_server(legacy_path: Path, v05_path: Path, v06_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "AGENTRELAY_HOST": "127.0.0.1",
        "AGENTRELAY_PORT": str(PORT),
        "AGENTRELAY_DB_PATH": str(legacy_path),
        "AGENTRELAY_V05_DB_PATH": str(v05_path),
        "AGENTRELAY_V06_DB_PATH": str(v06_path),
        "AGENTRELAY_MUTATION_MODE": "v06",
        "AGENTRELAY_V05_DRAIN_ENABLED": "1",
        "AGENTRELAY_TOKENS": "zac:zac-agent:requester-token,vivi:vivi-agent:target-token",
        "AGENTRELAY_ADMIN_TOKEN": "admin-token",
    }
    return subprocess.Popen(
        ["python3", "-m", "server.app"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_health() -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            request("GET", "/health", None, {}, 200)
            return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("drain HTTP server did not become healthy")


def request(method: str, path: str, payload: dict | None, headers: dict, expected: int) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            status = response.status
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        result = json.loads(exc.read())
    if status != expected:
        raise AssertionError(f"{method} {path}: expected {expected}, got {status}: {result}")
    return result


def stop_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


if __name__ == "__main__":
    main()
