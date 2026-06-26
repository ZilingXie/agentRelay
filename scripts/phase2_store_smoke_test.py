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

        pending_events = store.list_agent_events("frank-agent")
        if len(pending_events) != 1:
            raise AssertionError("task creation should emit one pending event for frank-agent")
        event = pending_events[0]
        if event["agent_id"] != "frank-agent":
            raise AssertionError("agent event was not scoped to frank-agent")
        if event["event_type"] != "task.pending":
            raise AssertionError("agent event type was not stored")
        if event["payload"]["taskId"] != task_id:
            raise AssertionError("agent event payload was not decoded")
        if event["payload"]["reason"] != "task.created":
            raise AssertionError("task creation pending event reason was not stored")

        after_artifact = store.submit_artifact(
            task_id,
            {
                "from": "frank-agent",
                "to": "zac-agent",
                "nextStatus": "artifact_submitted",
                "pendingOnAgentId": "zac-agent",
                "nextAction": "Zac should evaluate Frank's candidate time.",
                "artifact": {
                    "kind": "meeting_availability",
                    "parts": [{"kind": "text", "text": "Frank is available at 10:00."}],
                },
            },
        )
        if not after_artifact or after_artifact["pending_on_agent_id"] != "zac-agent":
            raise AssertionError("artifact should transfer pending ownership to zac-agent")
        zac_events = store.list_agent_events("zac-agent")
        if len(zac_events) != 1 or zac_events[0]["payload"]["reason"] != "ownership.transferred":
            raise AssertionError("artifact transfer should emit one pending event for zac-agent")

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

        print(
            json.dumps(
                {
                    "ok": True,
                    "taskId": task_id,
                    "eventId": event["event_id"],
                    "zacEventId": zac_events[0]["event_id"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
