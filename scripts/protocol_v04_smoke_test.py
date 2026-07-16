from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8797/agentrelay/api"
A = {"Authorization": "Bearer a-token", "X-AgentRelay-Agent-Id": "zac-agent"}
B = {"Authorization": "Bearer b-token", "X-AgentRelay-Agent-Id": "frank-agent"}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = f"{tmp}/v04.sqlite3"
        env = {
            **os.environ,
            "AGENTRELAY_HOST": "127.0.0.1",
            "AGENTRELAY_PORT": "8797",
            "AGENTRELAY_DB_PATH": db,
            "AGENTRELAY_TOKENS": "zac:zac-agent:a-token,frank:frank-agent:b-token",
        }
        proc = subprocess.Popen(
            ["python3", "-m", "server.app"], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            wait_health()
            task = post(
                "/tasks",
                {
                    "protocol_version": "agent-collab-v0.4",
                    "idempotency_key": "create-1",
                    "requester_agent_id": "zac-agent",
                    "target_agent_id": "frank-agent",
                    "done_criteria": "target returns pong",
                    "max_turns": 2,
                    "message": {"parts": [{"kind": "text", "text": "ping"}]},
                },
                A,
                201,
            )["task"]
            assert_snapshot(task, "submitted", 1, 1, "zac-agent", "frank-agent")
            task_id = task["task_id"]
            assert task["root_task_id"] == task_id

            events = get("/workers/frank-agent/events", B)["events"]
            pending = next(event for event in events if event["event_type"] == "task.message_pending")
            informational = next(event for event in events if event["event_type"] == "task.status_changed")
            post(
                f"/workers/frank-agent/events/{informational['event_id']}/ack",
                {"taskId": task_id, "deliveryState": "done"},
                B,
            )
            assert get(f"/tasks/{task_id}", B)["task"]["status"] == "submitted"

            task = ack(task, "frank-agent", B, "ack-request")
            assert_snapshot(task, "delivered", 1, 2, "zac-agent", "frank-agent")
            duplicate = ack_with_snapshot(task_id, pending["message_id"], 1, 1, "ack-request", B)
            assert duplicate["status_version"] == 2

            stale = post(
                f"/tasks/{task_id}/messages",
                mutation(task, "target-response", actor="frank-agent", parts="pong", expected_version=1),
                B,
                409,
            )
            assert "stale_task_state" in stale["error"]
            assert stale["code"] == "stale_task_state"
            assert stale["detail"]["current_task"]["status_version"] == task["status_version"]

            task = post(
                f"/tasks/{task_id}/messages",
                mutation(task, "target-response", actor="frank-agent", parts="pong"),
                B,
                201,
            )["task"]
            assert_snapshot(task, "submitted", 1, 3, "frank-agent", "zac-agent")
            task = ack(task, "zac-agent", A, "ack-response")
            assert_snapshot(task, "delivered", 1, 4, "frank-agent", "zac-agent")

            task = post(
                f"/tasks/{task_id}/complete",
                {
                    **context(task, "complete-1"),
                    "actor_agent_id": "zac-agent",
                    "completed_against_message_id": task["current_message_id"],
                },
                A,
            )["task"]
            assert task["status"] == "completed" and task["reason"] == "goal_met"

            follow = post(
                f"/tasks/{task_id}/followups",
                {
                    "idempotency_key": "follow-1",
                    "done_criteria": "target returns another pong",
                    "message": {"parts": [{"kind": "text", "text": "again"}]},
                },
                A,
                201,
            )["task"]
            assert follow["task_id"] != task_id and follow["root_task_id"] == task_id
            assert follow["turn_sequence"] == 1
        finally:
            proc.terminate()
            proc.wait(timeout=5)

        with sqlite3.connect(db) as conn:
            try:
                conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            except sqlite3.IntegrityError as exc:
                assert "forbids hard deletion" in str(exc)
            else:
                raise AssertionError("raw SQL hard delete unexpectedly succeeded")
    print("protocol v0.4 smoke test passed")


def context(task: dict, key: str) -> dict:
    return {
        "current_message_id": task["current_message_id"],
        "turn_sequence": task["turn_sequence"],
        "expected_status_version": task["status_version"],
        "idempotency_key": key,
    }


def mutation(task: dict, key: str, *, actor: str, parts: str, expected_version: int | None = None) -> dict:
    value = {**context(task, key), "actor_agent_id": actor, "parts": [{"kind": "text", "text": parts}]}
    if expected_version is not None:
        value["expected_status_version"] = expected_version
    return value


def ack(task: dict, agent: str, headers: dict[str, str], key: str) -> dict:
    return ack_with_snapshot(
        task["task_id"], task["current_message_id"], task["turn_sequence"],
        task["status_version"], key, headers,
    )


def ack_with_snapshot(task_id: str, message_id: str, turn: int, version: int, key: str, headers: dict[str, str]) -> dict:
    return post(
        f"/workers/{headers['X-AgentRelay-Agent-Id']}/messages/{message_id}/ack",
        {
            "task_id": task_id,
            "message_id": message_id,
            "current_message_id": message_id,
            "turn_sequence": turn,
            "expected_status_version": version,
            "idempotency_key": key,
        },
        headers,
    )["task"]


def assert_snapshot(task: dict, status: str, turn: int, version: int, from_agent: str, to_agent: str) -> None:
    assert (task["status"], task["turn_sequence"], task["status_version"]) == (status, turn, version)
    assert (task["from_agent_id"], task["to_agent_id"]) == (from_agent, to_agent)


def wait_health() -> None:
    for _ in range(60):
        try:
            get("/health", {})
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not become healthy")


def get(path: str, headers: dict[str, str]) -> dict:
    return request("GET", path, None, headers, 200)


def post(path: str, payload: dict, headers: dict[str, str], status: int = 200) -> dict:
    return request("POST", path, payload, headers, status)


def request(method: str, path: str, payload: dict | None, headers: dict[str, str], status: int) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method, headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            actual, data = response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        actual, data = exc.code, json.loads(exc.read())
    if actual != status:
        raise AssertionError(f"{method} {path}: expected {status}, got {actual}: {data}")
    return data


if __name__ == "__main__":
    main()
