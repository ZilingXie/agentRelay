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
BASE_URL = "http://127.0.0.1:8795/agentrelay/api"
AGENT_A_HEADERS = {
    "Authorization": "Bearer zac-token",
    "X-AgentRelay-Agent-Id": "zac-agent",
    "X-AgentRelay-Username": "zac",
}
AGENT_B_HEADERS = {
    "Authorization": "Bearer frank-token",
    "X-AgentRelay-Agent-Id": "frank-agent",
    "X-AgentRelay-Username": "frank",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/agentrelay-protocol-v02.sqlite3"
        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_HOST": "127.0.0.1",
                "AGENTRELAY_PORT": "8795",
                "AGENTRELAY_DB_PATH": db_path,
                "AGENTRELAY_TOKENS": (
                    "zac:zac-agent:zac-token,"
                    "frank:frank-agent:frank-token"
                ),
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
            created = post_json(
                f"{BASE_URL}/tasks",
                {
                    "protocol_version": "agent-collab-v0.2",
                    "requester_agent_id": "zac-agent",
                    "target_agent_id": "frank-agent",
                    "requesterThreadId": "zac-agent-thread-v02",
                    "subject": "Protocol v0.2 availability smoke",
                    "done_criteria": "Zac agent and Frank agent agree on one online meeting time.",
                    "completion_owner_agent_id": "zac-agent",
                    "pending_on_agent_id": "frank-agent",
                    "message": {
                        "actor_agent_id": "zac-agent",
                        "intent": "request_availability",
                        "parts": [{"kind": "text", "text": "Can you meet tomorrow at 14:00?"}],
                    },
                },
                AGENT_A_HEADERS,
            )["task"]
            task_id = created["task_id"]
            if created["requester_agent_id"] != "zac-agent":
                raise AssertionError("requester_agent_id was not stored")
            if created["target_agent_id"] != "frank-agent":
                raise AssertionError("target_agent_id was not stored")
            if created["pending_on_agent_id"] != "frank-agent":
                raise AssertionError("pending_on_agent_id should start on target")

            expect_http_error(
                "frank-agent token cannot create as zac-agent",
                403,
                "POST",
                f"{BASE_URL}/tasks",
                {
                    "requester_agent_id": "zac-agent",
                    "target_agent_id": "frank-agent",
                    "message": {
                        "actor_agent_id": "zac-agent",
                        "intent": "request_availability",
                        "parts": [{"kind": "text", "text": "bad create"}],
                    },
                },
                AGENT_B_HEADERS,
            )

            claimed = get_json(f"{BASE_URL}/workers/frank-agent/claim", AGENT_B_HEADERS)["task"]
            if claimed["task_id"] != task_id:
                raise AssertionError("frank-agent did not claim v0.2 task")

            expect_http_error(
                "zac-agent token cannot submit as frank-agent",
                403,
                "POST",
                f"{BASE_URL}/tasks/{task_id}/artifacts",
                {
                    "actor_agent_id": "frank-agent",
                    "artifact": {
                        "intent": "availability_response",
                        "kind": "availability",
                        "parts": [{"kind": "text", "text": "14:00 works."}],
                    },
                },
                AGENT_A_HEADERS,
            )

            after_artifact = post_json(
                f"{BASE_URL}/tasks/{task_id}/artifacts",
                {
                    "actor_agent_id": "frank-agent",
                    "artifact": {
                        "intent": "availability_response",
                        "kind": "availability",
                        "parts": [{"kind": "text", "text": "14:00 works."}],
                    },
                },
                AGENT_B_HEADERS,
            )["task"]
            if after_artifact["pending_on_agent_id"] != "zac-agent":
                raise AssertionError("artifact should transfer pending ownership to completion owner")
            if after_artifact["status"] != "delivery_pending":
                raise AssertionError("artifact should default to delivery_pending")

            expect_http_error(
                "non-completion-owner close rejected",
                400,
                "POST",
                f"{BASE_URL}/tasks/{task_id}/close",
                {"closedByAgentId": "frank-agent", "terminalReason": "wrong owner"},
                AGENT_B_HEADERS,
            )

            closed = post_json(
                f"{BASE_URL}/tasks/{task_id}/close",
                {
                    "closedByAgentId": "zac-agent",
                    "terminalReason": "Zac agent confirmed the same meeting time.",
                },
                AGENT_A_HEADERS,
            )["task"]
            if closed["status"] != "completed":
                raise AssertionError("completion owner did not close task")

            corrected_owner = post_json(
                f"{BASE_URL}/tasks",
                {
                    "protocol_version": "agent-collab-v0.2",
                    "requester_agent_id": "zac-agent",
                    "target_agent_id": "frank-agent",
                    "requesterThreadId": "zac-agent-thread-v02-bad-owner",
                    "subject": "Protocol v0.2 owner normalization smoke",
                    "done_criteria": "Requester evaluates completion.",
                    "completion_owner_agent_id": "frank-agent",
                    "pending_on_agent_id": "frank-agent",
                    "message": {
                        "actor_agent_id": "zac-agent",
                        "intent": "request",
                        "parts": [{"kind": "text", "text": "Owner should normalize to requester."}],
                    },
                },
                AGENT_A_HEADERS,
            )["task"]
            if corrected_owner["completion_owner_agent_id"] != "zac-agent":
                raise AssertionError("v0.2 two-agent task should keep requester as completion owner")

            events = get_json(f"{BASE_URL}/tasks/{task_id}/events", AGENT_A_HEADERS)["events"]
            created_event = find_event(events, "task.created")
            artifact_event = find_event(events, "artifact.submitted")
            if created_event["payload"].get("protocol_version") != "agent-collab-v0.2":
                raise AssertionError("task.created missing protocol_version")
            if created_event["payload"].get("actor_agent_id") != "zac-agent":
                raise AssertionError("task.created missing actor_agent_id")
            if created_event["payload"].get("intent") != "request_availability":
                raise AssertionError("task.created missing intent")
            if artifact_event["payload"].get("actor_agent_id") != "frank-agent":
                raise AssertionError("artifact.submitted missing actor_agent_id")
            if artifact_event["payload"].get("intent") != "availability_response":
                raise AssertionError("artifact.submitted missing intent")

            legacy = create_legacy_task()
            print(
                json.dumps(
                    {
                        "ok": True,
                        "taskId": task_id,
                        "legacyTaskId": legacy["task_id"],
                        "status": closed["status"],
                    },
                    indent=2,
                )
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def create_legacy_task() -> dict:
    task = post_json(
        f"{BASE_URL}/tasks",
        {
            "from": "zac-agent",
            "to": "frank-agent",
            "requesterThreadId": "zac-agent-thread-legacy",
            "subject": "Legacy compatibility smoke",
            "doneCriteria": "Legacy task remains compatible.",
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "Legacy hello."}],
            },
            "pendingOnHumanId": "legacy-human",
        },
        AGENT_A_HEADERS,
    )["task"]
    if task["requester_agent_id"] != "zac-agent" or task["target_agent_id"] != "frank-agent":
        raise AssertionError("legacy from/to did not normalize")
    return task


def find_event(events: list[dict], event_type: str) -> dict:
    for event in events:
        if event["event_type"] == event_type:
            return event
    raise AssertionError(f"missing event: {event_type}")


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


def post_json(url: str, payload: dict, headers: dict[str, str] | None = None) -> dict:
    return request_json("POST", url, payload=payload, headers=headers)


def request_json(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"{method} {url} failed: {exc.code} {body}") from exc


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
    except RuntimeError as exc:
        if f"failed: {expected_status}" not in str(exc):
            raise AssertionError(f"{label} returned unexpected error: {exc}") from exc
        return
    raise AssertionError(f"{label} unexpectedly succeeded")


if __name__ == "__main__":
    main()
