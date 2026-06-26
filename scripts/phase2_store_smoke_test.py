from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.store import Store


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(str(Path(tmp) / "agentrelay-phase2.sqlite3"))
        task = store.create_task(
            {
                "from": "zac-agent",
                "to": "frank-agent",
                "requesterThreadId": "zac-thread-phase2",
                "subject": "Phase 2 store smoke",
                "doneCriteria": "Zac and Frank agree on one meeting time.",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "Ask Frank for availability."}],
                },
            }
        )
        task_id = task["task_id"]

        event = store.create_agent_event(
            "frank-agent",
            "task.pending",
            task_id,
            {
                "taskId": task_id,
                "subject": task["subject"],
                "status": task["status"],
                "reason": "task.created",
            },
        )
        if event["agent_id"] != "frank-agent":
            raise AssertionError("agent event was not scoped to frank-agent")
        if event["event_type"] != "task.pending":
            raise AssertionError("agent event type was not stored")
        if event["payload"]["taskId"] != task_id:
            raise AssertionError("agent event payload was not decoded")

        pending_events = store.list_agent_events("frank-agent")
        if [row["event_id"] for row in pending_events] != [event["event_id"]]:
            raise AssertionError("unacked event list did not include the new event")

        acked = store.ack_agent_event("frank-agent", event["event_id"])
        if not acked or not acked["acked_at"]:
            raise AssertionError("agent event was not acked")
        if store.list_agent_events("frank-agent"):
            raise AssertionError("acked event should be hidden from default event list")
        all_events = store.list_agent_events("frank-agent", include_acked=True)
        if [row["event_id"] for row in all_events] != [event["event_id"]]:
            raise AssertionError("include_acked should return acked events")

        binding = store.upsert_thread_binding(
            task_id,
            "frank-agent",
            "frank-codex-thread-1",
            project_path="/Users/frank/work/agentrelay",
        )
        if binding["thread_id"] != "frank-codex-thread-1":
            raise AssertionError("thread binding was not inserted")
        if binding["thread_role"] != "agent_inbox":
            raise AssertionError("default thread role changed")

        updated_binding = store.upsert_thread_binding(
            task_id,
            "frank-agent",
            "frank-codex-thread-2",
        )
        if updated_binding["thread_id"] != "frank-codex-thread-2":
            raise AssertionError("thread binding was not updated")
        if updated_binding["created_at"] != binding["created_at"]:
            raise AssertionError("thread binding should preserve created_at on update")

        task_with_binding = store.get_task(task_id)
        bindings = task_with_binding["threadBindings"] if task_with_binding else []
        if len(bindings) != 1 or bindings[0]["thread_id"] != "frank-codex-thread-2":
            raise AssertionError("get_task did not include threadBindings")

        print(json.dumps({"ok": True, "taskId": task_id, "eventId": event["event_id"]}, indent=2))


if __name__ == "__main__":
    main()
