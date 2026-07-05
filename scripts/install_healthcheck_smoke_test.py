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
BASE_URL = "http://127.0.0.1:8804/agentrelay/api"
ZAC_HEADERS = {
    "Authorization": "Bearer zac-token",
    "X-AgentRelay-Agent-Id": "zac-agent",
    "X-AgentRelay-Username": "zac",
}
FRANK_HEADERS = {
    "Authorization": "Bearer frank-token",
    "X-AgentRelay-Agent-Id": "frank-agent",
    "X-AgentRelay-Username": "frank",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/agentrelay-install-healthcheck.sqlite3"
        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_HOST": "127.0.0.1",
                "AGENTRELAY_PORT": "8804",
                "AGENTRELAY_DB_PATH": db_path,
                "AGENTRELAY_TOKENS": "zac:zac-agent:zac-token,frank:frank-agent:frank-token",
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
            expect_http_error(
                "install healthcheck without auth",
                401,
                "POST",
                f"{BASE_URL}/healthchecks/install",
                {},
            )
            expect_http_error(
                "install healthcheck requester spoof",
                403,
                "POST",
                f"{BASE_URL}/healthchecks/install",
                {"requester_agent_id": "frank-agent"},
                ZAC_HEADERS,
            )

            created = post_json(
                f"{BASE_URL}/healthchecks/install",
                {
                    "requester_agent_id": "zac-agent",
                    "requesterThreadId": "zac-install-health-thread",
                    "idempotency_key": "install-healthcheck-smoke",
                },
                ZAC_HEADERS,
                expected_status=201,
            )
            task = created["task"]
            task_id = task["task_id"]
            if task["requester_agent_id"] != "zac-agent":
                raise AssertionError("healthcheck task requester must come from auth")
            if task["target_agent_id"] != "agentrelay-healthcheck":
                raise AssertionError("healthcheck task target should be synthetic")
            if task["completion_owner_agent_id"] != "zac-agent":
                raise AssertionError("healthcheck completion owner should be requester")
            if task["pending_on_agent_id"] != "zac-agent":
                raise AssertionError("healthcheck should be pending on requester")
            if not isinstance(task.get("ttl"), int) or task["ttl"] <= int(time.time()):
                raise AssertionError(f"healthcheck should have a short future ttl: {task.get('ttl')}")
            artifacts = task["artifacts"]
            if len(artifacts) != 1:
                raise AssertionError("healthcheck should include one ACK artifact")
            artifact = artifacts[0]
            if artifact["from_agent_id"] != "agentrelay-healthcheck":
                raise AssertionError("ACK artifact should come from synthetic healthcheck actor")
            ack_text = "\n".join(part.get("text", "") for part in artifact["parts"])
            for expected in ("ACK from agentrelay-healthcheck", "requester=zac-agent", f"task={task_id}"):
                if expected not in ack_text:
                    raise AssertionError(f"ACK artifact missing {expected!r}: {ack_text}")

            events = get_json(f"{BASE_URL}/workers/zac-agent/events", ZAC_HEADERS)["events"]
            if len(events) != 1:
                raise AssertionError(f"expected one requester pending event, got {len(events)}")
            event = events[0]
            if event["event_type"] != "task.pending" or event["task_id"] != task_id:
                raise AssertionError(f"unexpected requester event: {event}")
            if event["payload"].get("reason") != "install.healthcheck":
                raise AssertionError(f"unexpected healthcheck event reason: {event}")

            repeated = post_json(
                f"{BASE_URL}/healthchecks/install",
                {
                    "requester_agent_id": "zac-agent",
                    "requesterThreadId": "zac-install-health-thread",
                    "idempotency_key": "install-healthcheck-smoke",
                },
                ZAC_HEADERS,
                expected_status=201,
            )
            if repeated["task"]["task_id"] != task_id or not repeated.get("idempotent"):
                raise AssertionError("same healthcheck idempotency key should return the existing task")
            repeated_events = get_json(f"{BASE_URL}/workers/zac-agent/events", ZAC_HEADERS)["events"]
            if [item["event_id"] for item in repeated_events] != [event["event_id"]]:
                raise AssertionError("idempotent healthcheck retry should not create another agent event")

            task_events = get_json(f"{BASE_URL}/tasks/{task_id}/events", ZAC_HEADERS)["events"]
            if [item["event_type"] for item in task_events[:3]] != ["task.created", "artifact.submitted", "ownership.transferred"]:
                raise AssertionError(f"healthcheck task events are out of semantic order: {task_events}")
            if [item.get("event_sequence") for item in task_events[:3]] != [1, 2, 3]:
                raise AssertionError(f"healthcheck task events did not get stable sequence numbers: {task_events}")

            closed = post_json(
                f"{BASE_URL}/tasks/{task_id}/close",
                {
                    "protocol_version": "agent-collab-v0.3",
                    "idempotency_key": "install-healthcheck-smoke-close",
                    "closed_by_agent_id": "zac-agent",
                    "completion_authority": {
                        "type": "agent",
                        "agent_id": "zac-agent",
                        "summary": "Install healthcheck smoke verified synthetic ACK delivery.",
                    },
                    "terminal_reason": "Install healthcheck smoke completed.",
                },
                ZAC_HEADERS,
            )
            closed_task = protocol_data(closed)["task"]
            if closed_task["status"] != "completed":
                raise AssertionError("healthcheck close did not complete task")
            closed_events = get_json(f"{BASE_URL}/tasks/{task_id}/events", ZAC_HEADERS)["events"]
            completed_event = next(item for item in closed_events if item["event_type"] == "task.completed")
            refs = completed_event["payload"].get("completion_authority", {}).get("source_refs") or []
            if not refs or refs[0].get("summary", "").find(artifacts[0]["artifact_id"]) == -1:
                raise AssertionError(f"close event should source-ref the latest artifact: {completed_event}")

            expired = post_json(
                f"{BASE_URL}/healthchecks/install",
                {"idempotency_key": "install-healthcheck-expire-me"},
                ZAC_HEADERS,
                expected_status=201,
            )["task"]
            expire_task(db_path, expired["task_id"])
            post_json(
                f"{BASE_URL}/healthchecks/install",
                {"idempotency_key": "install-healthcheck-trigger-cleanup"},
                ZAC_HEADERS,
                expected_status=201,
            )
            expired_after = get_json(f"{BASE_URL}/tasks/{expired['task_id']}", ZAC_HEADERS)["task"]
            if expired_after["status"] != "expired" or expired_after["pending_on_agent_id"] is not None:
                raise AssertionError(f"expired healthcheck was not cleaned up: {expired_after}")

            agents = get_json(f"{BASE_URL}/agents", ZAC_HEADERS)["agents"]
            agent_ids = {agent["agent_id"] for agent in agents}
            if "agentrelay-healthcheck" not in agent_ids:
                raise AssertionError("synthetic healthcheck agent row was not created")

            print(json.dumps({"ok": True, "taskId": task_id, "eventId": event["event_id"]}, indent=2))
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
    return request_json("GET", url, headers=headers)


def post_json(
    url: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
) -> dict:
    return request_json("POST", url, payload=payload, headers=headers, expected_status=expected_status)


def request_json(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    with urllib.request.urlopen(req, timeout=5) as response:
        if response.status != expected_status:
            raise AssertionError(f"{method} {url} returned {response.status}, expected {expected_status}")
        return json.loads(response.read().decode("utf-8"))


def protocol_data(payload: dict) -> dict:
    if payload.get("ok") is True and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def expect_http_error(
    label: str,
    expected_status: int,
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    try:
        request_json(method, url, payload, headers)
    except urllib.error.HTTPError as exc:
        if exc.code != expected_status:
            body = exc.read().decode("utf-8")
            raise AssertionError(f"{label} returned {exc.code}, expected {expected_status}: {body}") from exc
        return
    raise AssertionError(f"{label} unexpectedly succeeded")


def expire_task(db_path: str, task_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE tasks SET ttl = ? WHERE task_id = ?", (int(time.time()) - 1, task_id))


if __name__ == "__main__":
    main()
