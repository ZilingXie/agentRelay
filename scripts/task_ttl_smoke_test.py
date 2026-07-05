from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.store import DEFAULT_TASK_TTL_SECONDS, Store


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/agentrelay-task-ttl.sqlite3"
        store = Store(db_path)

        default_task = store.create_task(base_task_payload("default-ttl"))
        now = int(time.time())
        if not isinstance(default_task.get("ttl"), int):
            raise AssertionError(f"default ttl should be stored as epoch seconds: {default_task.get('ttl')}")
        if default_task["ttl"] < now + DEFAULT_TASK_TTL_SECONDS - 5:
            raise AssertionError(f"default ttl should be about {DEFAULT_TASK_TTL_SECONDS} seconds in the future: {default_task['ttl']}")

        expiring = store.create_task({**base_task_payload("expires-before-reply"), "ttl_seconds": 3600})
        expire_task(db_path, expiring["task_id"])
        requester_events = store.list_agent_events("zac-agent")
        expired_event = next(
            (event for event in requester_events if event["task_id"] == expiring["task_id"]),
            None,
        )
        if not expired_event:
            raise AssertionError("expired task should notify requester agent")
        if expired_event["event_type"] != "task.pending":
            raise AssertionError(f"expired notification should stay listener-compatible: {expired_event}")
        if expired_event["payload"].get("reason") != "task.ttl_expired":
            raise AssertionError(f"expired notification should explain ttl expiry: {expired_event}")
        if expired_event["payload"].get("pendingOnAgentId") != "zac-agent":
            raise AssertionError(f"expired notification should route to requester agent: {expired_event}")
        expired = store.get_task(expiring["task_id"])
        if expired["status"] != "expired" or expired["pending_on_agent_id"] is not None:
            raise AssertionError(f"expired task should be terminal with no pending owner: {expired}")
        target_events = [
            event for event in store.list_agent_events("frank-agent", include_acked=True)
            if event["task_id"] == expiring["task_id"]
        ]
        if not target_events or target_events[0]["delivery_state"] != "done":
            raise AssertionError(f"target pending event should be cleaned up on expiry: {target_events}")
        try:
            store.submit_artifact(
                expiring["task_id"],
                artifact_payload("frank-agent", "zac-agent"),
            )
        except ValueError as exc:
            if "terminal" not in str(exc):
                raise AssertionError(f"late artifact should fail because task is terminal: {exc}") from exc
        else:
            raise AssertionError("late artifact after ttl expiry should be rejected")

        replied = store.create_task({**base_task_payload("reply-before-ttl"), "ttl_seconds": 3600})
        store.submit_artifact(
            replied["task_id"],
            artifact_payload("frank-agent", "zac-agent"),
        )
        expire_task(db_path, replied["task_id"])
        still_open = store.get_task(replied["task_id"])
        if still_open["status"] == "expired":
            raise AssertionError("task should not expire after target agent has replied")

        print(json.dumps({
            "ok": True,
            "expiredTaskId": expiring["task_id"],
            "requesterEventId": expired_event["event_id"],
            "repliedTaskStatus": still_open["status"],
        }, indent=2))


def base_task_payload(suffix: str) -> dict:
    return {
        "from": "zac-agent",
        "to": "frank-agent",
        "subject": f"TTL smoke {suffix}",
        "doneCriteria": "Frank replies before the task TTL.",
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "Please reply before the TTL."}],
        },
    }


def artifact_payload(actor_agent_id: str, pending_on_agent_id: str) -> dict:
    return {
        "from": actor_agent_id,
        "to": pending_on_agent_id,
        "nextStatus": "delivery_pending",
        "pendingOnAgentId": pending_on_agent_id,
        "nextAction": "Requester should evaluate the reply.",
        "artifact": {
            "kind": "result",
            "parts": [{"kind": "text", "text": "I can meet at 10:00."}],
        },
    }


def expire_task(db_path: str, task_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE tasks SET ttl = ? WHERE task_id = ?", (int(time.time()) - 1, task_id))


if __name__ == "__main__":
    main()
