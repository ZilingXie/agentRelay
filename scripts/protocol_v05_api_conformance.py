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

from server.protocol_v05 import PROTOCOL_V05
from server.store_v05 import V05Store


A = "zac-agent"
B = "frank-agent"
C = "vivi-agent"
HEADERS = {
    A: {"Authorization": "Bearer a-token", "X-AgentRelay-Agent-Id": A},
    B: {"Authorization": "Bearer b-token", "X-AgentRelay-Agent-Id": B},
    C: {"Authorization": "Bearer c-token", "X-AgentRelay-Agent-Id": C},
}
ADMIN_HEADERS = {"Authorization": "Bearer admin-token"}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run_v05_flow(root)
        run_closed_gate(root)
    print("protocol v0.5 HTTP conformance passed (20/20)")


def seed_registry(db_path: Path) -> None:
    store = V05Store(str(db_path))
    for agent_id in (A, B, C):
        store.upsert_agent(
            agent_id,
            name=agent_id,
            owner=agent_id,
            enabled=True,
            protocol_capabilities=[PROTOCOL_V05],
        )


def run_v05_flow(root: Path) -> None:
    v05_db = root / "api-v05.sqlite3"
    seed_registry(v05_db)
    base = "http://127.0.0.1:8798/agentrelay/api"
    process = start_server(root / "api-legacy.sqlite3", v05_db, "v05", 8798)
    try:
        wait_health(base)
        manifest = request(base, "GET", "/protocols/agent-collab/v0.5/manifest", None, {}, 200)
        assert manifest["write_mode"] == "v05"
        listeners = {
            agent_id: register_and_ready(base, agent_id, f"listener-{agent_id}")
            for agent_id in (A, B, C)
        }
        created = request(
            base,
            "POST",
            "/tasks",
            {
                "protocol_version": PROTOCOL_V05,
                "idempotency_key": "api-create",
                "requester_agent_id": A,
                "target_agent_id": B,
                "done_criteria": "accepted response",
                "max_turns": 2,
                "message": {"parts": [{"kind": "text", "text": "ping"}]},
            },
            HEADERS[A],
            201,
        )
        task = created["task"]
        task_id = task["task_id"]
        assert task["status"] == "open" and created["messages"][0]["delivery_status"] == "pending"

        request(base, "GET", f"/tasks/{task_id}", None, HEADERS[C], 403)
        event = recover(base, B, listeners[B])
        request(base, "POST", f"/workers/{B}/messages/msg_wrong/ack", {
            **context(task, "wrong-path"),
            "task_id": task_id,
            "event_id": event["event_id"],
            "listener_instance_id": listeners[B][0],
            "readiness_epoch": listeners[B][1],
        }, HEADERS[B], 400)
        detail = ack(base, B, task, event, listeners[B], "ack-request")
        task = detail["task"]
        assert task["task_version"] == 2
        assert request(base, "GET", f"/tasks/{task_id}/visibility", None, HEADERS[A], 200)["diagnosis"] == "waiting_target_response"

        task_before_info_ack = dict(task)
        info_event = recover(base, A, listeners[A])
        assert info_event["event_type"] == "message.delivery_changed"
        assert info_event["can_transition_message"] is False
        event_count = len(
            request(
                base,
                "GET",
                f"/admin/api/events?task_id={task_id}",
                None,
                ADMIN_HEADERS,
                200,
            )["events"]
        )
        info_ack = ack_info(base, A, info_event, listeners[A], "ack-delivery-info")
        assert info_ack["outbox_status"] == "acked"
        duplicate_info_ack = ack_info(
            base, A, info_event, listeners[A], "ack-delivery-info"
        )
        assert duplicate_info_ack["event_id"] == info_event["event_id"]
        assert request(base, "GET", f"/tasks/{task_id}", None, HEADERS[A], 200)["task"] == task_before_info_ack
        assert len(
            request(
                base,
                "GET",
                f"/admin/api/events?task_id={task_id}",
                None,
                ADMIN_HEADERS,
                200,
            )["events"]
        ) == event_count

        detail = request(
            base,
            "POST",
            f"/tasks/{task_id}/messages",
            {
                **context(task, "api-response"),
                "actor_agent_id": B,
                "parts": [{"kind": "text", "text": "pong"}],
            },
            HEADERS[B],
            201,
        )
        task = detail["task"]
        response_event = recover(base, A, listeners[A])
        detail = ack(base, A, task, response_event, listeners[A], "ack-response")
        task = detail["task"]
        assert task["task_version"] == 4 and len(detail["messages"]) == 2

        batch = request(
            base,
            "POST",
            "/task-visibility/batch",
            {"task_ids": [task_id, "task_missing"]},
            HEADERS[A],
            200,
        )
        assert len(batch["items"]) == 1 and batch["errors"] == [
            {"task_id": "task_missing", "code": "task_not_found"}
        ]
        unauthorized_batch = request(
            base,
            "POST",
            "/task-visibility/batch",
            {"task_ids": [task_id]},
            HEADERS[C],
            200,
        )
        assert unauthorized_batch["items"] == []
        assert unauthorized_batch["errors"][0]["code"] == "task_participant_required"

        completed = request(
            base,
            "POST",
            f"/tasks/{task_id}/complete",
            {
                **context(task, "api-complete"),
                "actor_agent_id": A,
                "completed_against_message_id": task["current_message_id"],
            },
            HEADERS[A],
            200,
        )
        assert completed["task"]["status"] == "completed"
        followup = request(
            base,
            "POST",
            f"/tasks/{task_id}/followups",
            {
                "idempotency_key": "api-followup",
                "done_criteria": "another accepted response",
                "message": {"parts": [{"kind": "text", "text": "again"}]},
            },
            HEADERS[A],
            201,
        )
        assert followup["task"]["root_task_id"] == task_id
        lineage = request(
            base, "GET", f"/tasks/{task_id}/lineage", None, HEADERS[A], 200
        )["tasks"]
        assert {item["task_id"] for item in lineage} == {
            task_id, followup["task"]["task_id"]
        }

        summary = request(base, "GET", "/admin/api/summary", None, ADMIN_HEADERS, 200)
        assert summary["protocol_version"] == PROTOCOL_V05
        assert summary["tasks"]["total"] == 2
        assert "by_delivery_status" in summary["messages"]
        agents = request(base, "GET", "/admin/api/agents", None, ADMIN_HEADERS, 200)
        assert len(agents["agents"]) == 3
        admin_tasks = request(base, "GET", "/admin/api/tasks", None, ADMIN_HEADERS, 200)
        assert all("diagnosis" in item and "current_message" in item for item in admin_tasks["tasks"])
        admin_detail = request(
            base, "GET", f"/admin/api/tasks/{task_id}", None, ADMIN_HEADERS, 200
        )
        assert admin_detail["visibility"]["diagnosis"] == "task_completed"
        assert len(admin_detail["audit_events"]) >= 5

        request(
            base,
            "POST",
            "/tasks",
            {
                "protocol_version": "agent-collab-v0.4",
                "idempotency_key": "retired-create",
                "requester_agent_id": A,
                "target_agent_id": B,
                "done_criteria": "retired",
                "message": {"parts": [{"kind": "text", "text": "retired"}]},
            },
            HEADERS[A],
            410,
        )
    finally:
        stop_server(process)


def run_closed_gate(root: Path) -> None:
    v05_db = root / "api-closed-v05.sqlite3"
    seed_registry(v05_db)
    base = "http://127.0.0.1:8799/agentrelay/api"
    process = start_server(root / "api-closed-legacy.sqlite3", v05_db, "closed", 8799)
    try:
        wait_health(base)
        manifest = request(base, "GET", "/protocols/agent-collab/v0.5/manifest", None, {}, 200)
        assert manifest["write_mode"] == "closed"
        readiness = register_and_ready(base, A, "closed-listener-a")
        assert readiness[1] == 1
        request(
            base,
            "POST",
            "/tasks",
            {
                "protocol_version": PROTOCOL_V05,
                "idempotency_key": "closed-create",
                "requester_agent_id": A,
                "target_agent_id": B,
                "done_criteria": "must stay closed",
                "message": {"parts": [{"kind": "text", "text": "closed"}]},
            },
            HEADERS[A],
            503,
        )
    finally:
        stop_server(process)


def start_server(legacy_db: Path, v05_db: Path, mode: str, port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "AGENTRELAY_HOST": "127.0.0.1",
        "AGENTRELAY_PORT": str(port),
        "AGENTRELAY_DB_PATH": str(legacy_db),
        "AGENTRELAY_V05_DB_PATH": str(v05_db),
        "AGENTRELAY_MUTATION_MODE": mode,
        "AGENTRELAY_TOKENS": (
            "zac:zac-agent:a-token,frank:frank-agent:b-token,vivi:vivi-agent:c-token"
        ),
        "AGENTRELAY_ADMIN_TOKEN": "admin-token",
    }
    return subprocess.Popen(
        ["python3", "-m", "server.app"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def register_and_ready(base: str, agent_id: str, instance_id: str) -> tuple[str, int]:
    registered = request(
        base,
        "POST",
        f"/workers/{agent_id}/readiness/register",
        {
            "listener_instance_id": instance_id,
            "client_version": "0.5.0",
            "workspace_version": "2",
            "transport": "websocket",
        },
        HEADERS[agent_id],
        201,
    )["readiness"]
    epoch = registered["readiness_epoch"]
    ready = request(
        base,
        "POST",
        f"/workers/{agent_id}/readiness",
        {"listener_instance_id": instance_id, "readiness_epoch": epoch, "ready": True},
        HEADERS[agent_id],
        200,
    )["readiness"]
    assert ready["ready"] is True
    return instance_id, epoch


def recover(base: str, agent_id: str, listener: tuple[str, int]) -> dict:
    query = urllib.parse.urlencode(
        {"listener_instance_id": listener[0], "readiness_epoch": listener[1]}
    )
    events = request(
        base, "GET", f"/workers/{agent_id}/events?{query}", None, HEADERS[agent_id], 200
    )["events"]
    assert len(events) == 1
    return events[0]


def ack(
    base: str,
    agent_id: str,
    task: dict,
    event: dict,
    listener: tuple[str, int],
    key: str,
) -> dict:
    return request(
        base,
        "POST",
        f"/workers/{agent_id}/messages/{task['current_message_id']}/ack",
        {
            **context(task, key),
            "task_id": task["task_id"],
            "event_id": event["event_id"],
            "listener_instance_id": listener[0],
            "readiness_epoch": listener[1],
        },
        HEADERS[agent_id],
        200,
    )


def ack_info(
    base: str,
    agent_id: str,
    event: dict,
    listener: tuple[str, int],
    key: str,
) -> dict:
    return request(
        base,
        "POST",
        f"/workers/{agent_id}/events/{event['event_id']}/ack",
        {
            "idempotency_key": key,
            "listener_instance_id": listener[0],
            "readiness_epoch": listener[1],
        },
        HEADERS[agent_id],
        200,
    )["event"]


def context(task: dict, key: str) -> dict:
    return {
        "message_id": task["current_message_id"],
        "turn_sequence": task["turn_sequence"],
        "expected_task_version": task["task_version"],
        "idempotency_key": key,
    }


def wait_health(base: str) -> None:
    for _ in range(60):
        try:
            request(base, "GET", "/health", None, {}, 200)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not become healthy")


def stop_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def request(
    base: str,
    method: str,
    path: str,
    payload: dict | None,
    headers: dict[str, str],
    expected_status: int,
) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base + path,
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
