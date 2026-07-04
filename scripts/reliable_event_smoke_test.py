from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.store import Store

BASE_URL = "http://127.0.0.1:8795/agentrelay/api"
FRANK_HEADERS = {
    "Authorization": "Bearer frank-token",
    "X-AgentRelay-Agent-Id": "frank-agent",
    "X-AgentRelay-Username": "frank",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/agentrelay-reliable-events.sqlite3"
        store = Store(db_path)
        task = store.create_task(
            {
                "from": "zac-agent",
                "to": "frank-agent",
                "requesterThreadId": "zac-thread-reliable-events",
                "subject": "Private subject should not be pushed",
                "doneCriteria": "Frank receives a reliable event.",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "Ask Frank for availability."}],
                },
            }
        )
        task_id = task["task_id"]
        first_events = store.list_agent_events("frank-agent")
        if len(first_events) != 1:
            raise AssertionError("task creation should emit one event")
        with store.connect() as conn:
            duplicate = store.create_pending_agent_event_conn(
                conn,
                task_id,
                "task.created",
                task["updated_at"],
            )
        if duplicate and duplicate["event_id"] != first_events[0]["event_id"]:
            raise AssertionError("pending event idempotency key did not deduplicate")

        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_HOST": "127.0.0.1",
                "AGENTRELAY_PORT": "8795",
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
            listed = get_json(f"{BASE_URL}/workers/frank-agent/events?limit=1", FRANK_HEADERS)
            event = listed["events"][0]
            if not listed.get("nextCursor") or listed.get("next_cursor") != listed.get("nextCursor"):
                raise AssertionError("event list should return nextCursor aliases")
            if event["delivery_state"] != "pending" or not event.get("cursor"):
                raise AssertionError("fresh event should be pending and cursor-addressable")
            if "subject" in event["payload"] or "nextAction" in event["payload"]:
                raise AssertionError("agent event payload should be secret-safe metadata")
            payload_ref = event["payload"].get("payloadRef")
            if payload_ref != {"method": "GET", "href": f"/agentrelay/tasks/{task_id}"}:
                raise AssertionError("agent event should include payloadRef to fetch task content")

            after_cursor = get_json(
                f"{BASE_URL}/workers/frank-agent/events?cursor={listed['nextCursor']}",
                FRANK_HEADERS,
            )
            if after_cursor["events"]:
                raise AssertionError("cursor read should not return the same event")

            claimed = get_json(
                f"{BASE_URL}/workers/frank-agent/events?claim=true&lease_seconds=30",
                FRANK_HEADERS,
            )["events"][0]
            if claimed["delivery_state"] != "inflight" or claimed["delivery_attempts"] != 1:
                raise AssertionError("claim should mark event inflight and increment attempts")
            if not claimed["inflight_until"]:
                raise AssertionError("claimed event should have a lease")

            failed = post_json(
                f"{BASE_URL}/workers/frank-agent/events/{event['event_id']}/ack",
                {"taskId": task_id, "deliveryState": "failed", "error": "local adapter unavailable"},
                FRANK_HEADERS,
            )["event"]
            if failed["delivery_state"] != "failed" or failed["acked_at"] is not None:
                raise AssertionError("failed delivery should not permanently ack event")

            reclaimed = get_json(
                f"{BASE_URL}/workers/frank-agent/events?claim=true&lease_seconds=30",
                FRANK_HEADERS,
            )["events"][0]
            if reclaimed["delivery_attempts"] != 2:
                raise AssertionError("failed event should be claimable again")

            done = post_json(
                f"{BASE_URL}/workers/frank-agent/events/{event['event_id']}/ack",
                {
                    "taskId": task_id,
                    "deliveryState": "done",
                    "threadId": "frank-thread-reliable-events",
                },
                FRANK_HEADERS,
            )["event"]
            if done["delivery_state"] != "done" or not done["acked_at"] or not done["done_at"]:
                raise AssertionError("done delivery should ack event")

            remaining = get_json(f"{BASE_URL}/workers/frank-agent/events", FRANK_HEADERS)["events"]
            if remaining:
                raise AssertionError("done event should be hidden from default event list")
            done_list = get_json(
                f"{BASE_URL}/workers/frank-agent/events?include_acked=true&delivery_state=done",
                FRANK_HEADERS,
            )["events"]
            if [row["event_id"] for row in done_list] != [event["event_id"]]:
                raise AssertionError("done event should be visible when explicitly requested")

            cleanup = assert_terminal_task_cleans_stale_events(store)

            print(
                json.dumps(
                    {
                        "ok": True,
                        "taskId": task_id,
                        "eventId": event["event_id"],
                        "terminalCleanupEventId": cleanup["event_id"],
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


def assert_terminal_task_cleans_stale_events(store: Store) -> dict:
    task = store.create_task(
        {
            "from": "zac-agent",
            "to": "frank-agent",
            "requesterThreadId": "zac-thread-terminal-cleanup",
            "subject": "Terminal cleanup",
            "doneCriteria": "Requester closes after receiving artifact.",
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "Return a result, then requester will close."}],
            },
        }
    )
    task_id = task["task_id"]
    frank_event = store.list_agent_events("frank-agent")[-1]
    store.ack_agent_event("frank-agent", frank_event["event_id"])
    store.submit_artifact(
        task_id,
        {
            "from": "frank-agent",
            "to": "zac-agent",
            "nextStatus": "delivery_pending",
            "pendingOnAgentId": "zac-agent",
            "nextAction": "Requester should evaluate and close.",
            "artifact": {
                "kind": "result",
                "parts": [{"kind": "text", "text": "The requested work is done."}],
            },
        },
    )
    zac_events = store.claim_agent_events("zac-agent", lease_seconds=30)
    cleanup_event = next(row for row in zac_events if row["task_id"] == task_id)
    if cleanup_event["delivery_state"] != "inflight":
        raise AssertionError("test setup should leave requester event inflight before close")
    before_close = store.get_task(task_id)
    if not before_close or before_close["delivery_status"] != "pending":
        raise AssertionError("test setup should leave delivery_status pending before close")

    closed = store.close_task(
        task_id,
        {
            "closedByAgentId": "zac-agent",
            "terminalReason": "Requester validated artifact and closed task.",
        },
    )
    if not closed or closed["status"] != "completed":
        raise AssertionError("terminal cleanup task did not close")
    if closed["delivery_status"] != "delivered":
        raise AssertionError("close should clear pending delivery_status")
    if store.list_agent_events("zac-agent"):
        raise AssertionError("terminal close should hide stale requester events from default list")
    done_events = [
        row
        for row in store.list_agent_events("zac-agent", include_acked=True, delivery_state="done")
        if row["task_id"] == task_id
    ]
    if [row["event_id"] for row in done_events] != [cleanup_event["event_id"]]:
        raise AssertionError("terminal close should ack stale requester event as done")
    if not done_events[0]["acked_at"] or not done_events[0]["done_at"]:
        raise AssertionError("terminal cleanup should set acked_at and done_at")
    return cleanup_event


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
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    main()
