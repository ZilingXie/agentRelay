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

from server.store import Store

BASE_URL = "http://127.0.0.1:8792/agentrelay/api"
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
        db_path = f"{tmpdir}/agentrelay-phase2-api.sqlite3"
        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_HOST": "127.0.0.1",
                "AGENTRELAY_PORT": "8792",
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
            task = post_json(
                f"{BASE_URL}/tasks",
                {
                    "from": "zac-agent",
                    "to": "frank-agent",
                    "requesterThreadId": "zac-thread-phase2-api",
                    "subject": "Phase 2 API smoke",
                    "doneCriteria": "Zac and Frank agree on one meeting time.",
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": "Ask Frank for availability."}],
                    },
                },
                ZAC_HEADERS,
            )["task"]
            task_id = task["task_id"]

            pending = get_json(f"{BASE_URL}/workers/frank-agent/pending", FRANK_HEADERS)["tasks"]
            if [item["taskId"] for item in pending] != [task_id]:
                raise AssertionError("pending endpoint did not return Frank's task")
            if pending[0]["subject"] != "Phase 2 API smoke":
                raise AssertionError("pending summary is missing subject")

            expect_http_error(
                "Zac cannot claim Frank's precise task",
                409,
                "POST",
                f"{BASE_URL}/workers/zac-agent/tasks/{task_id}/claim",
                {},
                ZAC_HEADERS,
            )

            claimed = post_json(
                f"{BASE_URL}/workers/frank-agent/tasks/{task_id}/claim",
                {},
                FRANK_HEADERS,
            )["task"]
            if claimed["task_id"] != task_id or claimed["status"] != "claimed":
                raise AssertionError("precise claim did not claim the requested task")

            claimed_again = post_json(
                f"{BASE_URL}/workers/frank-agent/tasks/{task_id}/claim",
                {},
                FRANK_HEADERS,
            )["task"]
            if claimed_again["task_id"] != task_id or claimed_again["status"] != "claimed":
                raise AssertionError("precise claim should be idempotent for same agent")

            pending_after_claim = get_json(f"{BASE_URL}/workers/frank-agent/pending", FRANK_HEADERS)["tasks"]
            if [item["taskId"] for item in pending_after_claim] != [task_id]:
                raise AssertionError("pending sync should include same-agent claimed work for recovery")

            event = Store(db_path).create_agent_event(
                "frank-agent",
                "task.pending",
                task_id,
                {
                    "taskId": task_id,
                    "subject": "Phase 2 API smoke",
                    "status": "claimed",
                    "reason": "api_smoke_test",
                },
            )
            expect_http_error(
                "wrong event taskId rejected",
                400,
                "POST",
                f"{BASE_URL}/workers/frank-agent/events/{event['event_id']}/ack",
                {"taskId": "task_wrong"},
                FRANK_HEADERS,
            )

            acked = post_json(
                f"{BASE_URL}/workers/frank-agent/events/{event['event_id']}/ack",
                {
                    "taskId": task_id,
                    "status": "dispatched_to_local_listener",
                    "threadId": "frank-thread-phase2-api",
                    "projectPath": "/Users/frank/agentrelay",
                },
                FRANK_HEADERS,
            )
            if acked["event"]["acked_at"] is None:
                raise AssertionError("ack endpoint did not ack event")
            binding = acked["threadBinding"]
            if binding["thread_id"] != "frank-thread-phase2-api":
                raise AssertionError("ack endpoint did not record thread binding")

            fetched = get_json(f"{BASE_URL}/tasks/{task_id}", FRANK_HEADERS)["task"]
            bindings = fetched["threadBindings"]
            if len(bindings) != 1 or bindings[0]["thread_id"] != "frank-thread-phase2-api":
                raise AssertionError("task payload did not include ack-created thread binding")

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
