from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.protocol_v06 import PROTOCOL_V06
from server.store_v06 import V06Store


REQUESTER = "zac-agent"
TARGET = "vivi-agent"
PORT = 8805
BASE = f"http://127.0.0.1:{PORT}/agentrelay/api"
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
        v06_db = root / "v06.sqlite3"
        seed_agents(v06_db)
        process = start_server(root / "legacy.sqlite3", v06_db)
        try:
            wait_health()
            run_flow(v06_db)
        finally:
            stop_server(process)
    print("protocol v0.6 HTTP conformance passed (6/6)")


def seed_agents(db_path: Path) -> None:
    store = V06Store(str(db_path))
    for agent_id in (REQUESTER, TARGET):
        store.upsert_agent(
            agent_id,
            name=agent_id,
            owner=agent_id,
            enabled=True,
            protocol_capabilities=[PROTOCOL_V06],
        )


def run_flow(db_path: Path) -> None:
    health = request("GET", "/health", None, {}, 200)
    assert health["protocol"]["version"] == PROTOCOL_V06
    assert health["protocol"]["write_mode"] == "v06"
    manifest = request(
        "GET", "/protocols/agent-collab/v0.6/manifest", None, {}, 200
    )
    assert manifest["version"] == PROTOCOL_V06
    assert manifest["constants"]["max_agent_unacked_events"] == 1000
    assert manifest["constants"]["default_max_inflight"] == 1
    assert manifest["constants"]["max_configured_inflight"] == 100
    bundle = request(
        "GET", "/protocols/agent-collab/v0.6/bundle", None, {}, 200
    )
    adapter_json = json.dumps(bundle["adapters"], sort_keys=True)
    assert "-v05.schema.json" not in adapter_json
    assert PROTOCOL_V06 in adapter_json

    created = create_task("offline-create")
    task = created["task"]
    assert task["status"] == "open"
    assert created["messages"][0]["delivery_status"] == "pending"
    visibility = request(
        "GET",
        f"/tasks/{task['task_id']}/visibility",
        None,
        HEADERS[REQUESTER],
        200,
    )
    assert visibility["diagnosis"] == "waiting_listener"
    assert visibility["outbox"]["outbox_status"] == "parked"

    listener_one = register_and_ready("listener-vivi-one")
    event = recover(listener_one)
    assert event["outbox_status"] == "inflight"
    assert event["inflight_via"] == "recovery"
    assert event["outbox_attempts"] == 0
    parked = request(
        "POST",
        f"/workers/{TARGET}/messages/{task['current_message_id']}/delivery-fail",
        {
            **context(task, "persistence-nack"),
            "task_id": task["task_id"],
            "event_id": event["event_id"],
            "listener_instance_id": listener_one[0],
            "readiness_epoch": listener_one[1],
            "reason": "listener_persistence_failed",
        },
        HEADERS[TARGET],
        200,
    )
    assert parked["task"]["status"] == "open"
    assert parked["messages"][0]["delivery_status"] == "pending"
    visibility = request(
        "GET",
        f"/tasks/{task['task_id']}/visibility",
        None,
        HEADERS[REQUESTER],
        200,
    )
    assert visibility["outbox"]["outbox_status"] == "parked"
    assert visibility["diagnosis"] == "waiting_listener"

    event = recover(listener_one)
    delivered = ack(task, event, listener_one, "recovered-ack", 200)
    assert delivered["task"]["status"] == "open"
    assert delivered["messages"][0]["delivery_status"] == "delivered"

    stale_at = int(time.time()) - 301
    V06Store(str(db_path)).publish_readiness(
        TARGET,
        listener_instance_id=listener_one[0],
        readiness_epoch=listener_one[1],
        ready=True,
        now=stale_at,
    )
    listener_two = register_and_ready("listener-vivi-two", recover_if_stale=True)
    assert listener_two[1] == listener_one[1] + 1

    fenced = create_task("old-epoch-fence")["task"]
    fenced_event = recover(listener_two)
    error = ack(fenced, fenced_event, listener_one, "old-epoch-ack", 409)
    assert error["code"] == "stale_readiness_epoch"
    ack(fenced, fenced_event, listener_two, "current-epoch-ack", 200)

    expires_at = int(time.time()) + 10
    expiring = create_task("expired-notice", expires_at=expires_at)["task"]
    assert V06Store(str(db_path)).expire_tasks(now=expires_at) == 1
    terminal_event = recover(listener_two)
    assert terminal_event["task_id"] == expiring["task_id"]
    assert terminal_event["event_type"] == "task.status_changed"
    expired = request(
        "GET", f"/tasks/{expiring['task_id']}", None, HEADERS[TARGET], 200
    )
    assert expired["task"]["status"] == "expired"
    acked_notice = request(
        "POST",
        f"/workers/{TARGET}/events/{terminal_event['event_id']}/ack",
        {
            "idempotency_key": "expired-notice-ack",
            "listener_instance_id": listener_two[0],
            "readiness_epoch": listener_two[1],
        },
        HEADERS[TARGET],
        200,
    )
    assert acked_notice["event"]["outbox_status"] == "acked"


def create_task(key: str, *, expires_at: int | None = None) -> dict:
    payload = {
        "protocol_version": PROTOCOL_V06,
        "idempotency_key": key,
        "requester_agent_id": REQUESTER,
        "target_agent_id": TARGET,
        "done_criteria": "target receives the durable message",
        "message": {
            "subject": key,
            "parts": [{"kind": "text", "text": key}],
        },
    }
    if expires_at is not None:
        payload["task_expires_at"] = expires_at
    return request("POST", "/tasks", payload, HEADERS[REQUESTER], 201)


def register_and_ready(
    instance_id: str, *, recover_if_stale: bool = False
) -> tuple[str, int]:
    registered = request(
        "POST",
        f"/workers/{TARGET}/readiness/register",
        {
            "listener_instance_id": instance_id,
            "client_version": "0.6.0",
            "workspace_version": "2",
            "transport": "websocket",
            "recover_if_stale": recover_if_stale,
        },
        HEADERS[TARGET],
        201,
    )["readiness"]
    epoch = registered["readiness_epoch"]
    request(
        "POST",
        f"/workers/{TARGET}/readiness",
        {
            "listener_instance_id": instance_id,
            "readiness_epoch": epoch,
            "ready": True,
        },
        HEADERS[TARGET],
        200,
    )
    return instance_id, epoch


def recover(listener: tuple[str, int]) -> dict:
    query = urllib.parse.urlencode(
        {"listener_instance_id": listener[0], "readiness_epoch": listener[1]}
    )
    events = request(
        "GET",
        f"/workers/{TARGET}/events?{query}",
        None,
        HEADERS[TARGET],
        200,
    )["events"]
    assert len(events) == 1
    return events[0]


def ack(
    task: dict,
    event: dict,
    listener: tuple[str, int],
    key: str,
    status: int,
) -> dict:
    return request(
        "POST",
        f"/workers/{TARGET}/messages/{task['current_message_id']}/ack",
        {
            **context(task, key),
            "task_id": task["task_id"],
            "event_id": event["event_id"],
            "listener_instance_id": listener[0],
            "readiness_epoch": listener[1],
        },
        HEADERS[TARGET],
        status,
    )


def context(task: dict, key: str) -> dict:
    return {
        "message_id": task["current_message_id"],
        "turn_sequence": task["turn_sequence"],
        "expected_task_version": task["task_version"],
        "idempotency_key": key,
    }


def start_server(legacy_db: Path, v06_db: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "AGENTRELAY_HOST": "127.0.0.1",
        "AGENTRELAY_PORT": str(PORT),
        "AGENTRELAY_DB_PATH": str(legacy_db),
        "AGENTRELAY_V06_DB_PATH": str(v06_db),
        "AGENTRELAY_MUTATION_MODE": "v06",
        "AGENTRELAY_TOKENS": (
            "zac:zac-agent:requester-token,vivi:vivi-agent:target-token"
        ),
    }
    return subprocess.Popen(
        ["python3", "-m", "server.app"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_health() -> None:
    for _ in range(60):
        try:
            request("GET", "/health", None, {}, 200)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("v0.6 HTTP server did not become healthy")


def stop_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def request(
    method: str,
    path: str,
    payload: dict | None,
    headers: dict[str, str],
    expected_status: int,
) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=body,
        method=method,
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            status = response.status
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        result = json.loads(exc.read())
    if status != expected_status:
        raise AssertionError(
            f"{method} {path}: expected {expected_status}, got {status}: {result}"
        )
    return result


if __name__ == "__main__":
    main()
