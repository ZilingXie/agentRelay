from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.store import ConflictError, Store
from server.ws_app import format_event_message


A = "zac-agent"
B = "frank-agent"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(f"{tmp}/conformance.sqlite3")
        one_turn_and_idempotency(store)
        multi_turn_and_max_turns(store)
        stale_acks_and_terminal_guards(store)
        expiry(store)
        failure_authority(store)
        lineage_and_concurrency(store)
        deletion_and_compatibility(store)
        websocket_metadata()
    print("protocol v0.4 conformance passed (16/16)")


def create(store: Store, key: str, *, max_turns: int = 3, expires_at: int | None = None) -> dict:
    payload = {
        "protocol_version": "agent-collab-v0.4",
        "idempotency_key": key,
        "requester_agent_id": A,
        "target_agent_id": B,
        "done_criteria": "accepted target response",
        "max_turns": max_turns,
        "message": {"parts": [{"kind": "text", "text": key}]},
    }
    if expires_at is not None:
        payload["task_expires_at"] = expires_at
    return store.create_task_v04(payload)


def context(task: dict, key: str) -> dict:
    return {
        "current_message_id": task["current_message_id"],
        "turn_sequence": task["turn_sequence"],
        "expected_status_version": task["status_version"],
        "idempotency_key": key,
    }


def ack(store: Store, task: dict, agent: str, key: str) -> dict:
    message_id = task["current_message_id"]
    return store.ack_v04_message(
        agent,
        message_id,
        {**context(task, key), "task_id": task["task_id"], "message_id": message_id},
    )


def message(store: Store, task: dict, actor: str, key: str) -> dict:
    return store.submit_v04_message(
        task["task_id"], actor,
        {**context(task, key), "parts": [{"kind": "text", "text": key}]},
    )


def response_delivered(store: Store, task: dict, suffix: str) -> dict:
    task = ack(store, task, B, f"ack-request-{suffix}")
    task = message(store, task, B, f"response-{suffix}")
    return ack(store, task, A, f"ack-response-{suffix}")


def assert_conflict(callable_) -> None:
    try:
        callable_()
    except ConflictError:
        return
    raise AssertionError("expected ConflictError")


def one_turn_and_idempotency(store: Store) -> None:
    task = create(store, "c-one")
    duplicate_create = create(store, "c-one")
    assert duplicate_create["task_id"] == task["task_id"]
    original_expiry = task["task_expires_at"]
    task = ack(store, task, B, "one-ack")
    duplicate_ack = store.ack_v04_message(
        B, task["current_message_id"],
        {
            "task_id": task["task_id"], "message_id": task["current_message_id"],
            "current_message_id": task["current_message_id"], "turn_sequence": 1,
            "expected_status_version": 1, "idempotency_key": "one-ack",
        },
    )
    assert duplicate_ack["status_version"] == 2
    response_request = {**context(task, "one-response"), "parts": [{"kind": "text", "text": "one-response"}]}
    task = store.submit_v04_message(task["task_id"], B, response_request)
    duplicate_message = store.submit_v04_message(task["task_id"], B, response_request)
    assert duplicate_message["current_message_id"] == task["current_message_id"]
    assert task["turn_sequence"] == 1 and task["task_expires_at"] == original_expiry
    task = ack(store, task, A, "one-response-ack")
    completed = store.complete_v04_task(
        task["task_id"], A,
        {**context(task, "one-complete"), "completed_against_message_id": task["current_message_id"]},
    )
    assert completed["status"] == "completed"


def multi_turn_and_max_turns(store: Store) -> None:
    task = response_delivered(store, create(store, "c-multi", max_turns=2), "multi-1")
    assert_conflict(lambda: message(store, task, B, "same-agent"))
    task = message(store, task, A, "followup-turn-2")
    assert task["turn_sequence"] == 2
    task = response_delivered(store, task, "multi-2")
    assert_conflict(lambda: message(store, task, A, "turn-3-rejected"))
    failed = store.fail_v04_task(
        task["task_id"], A, {**context(task, "max-fail"), "reason": "max_turns_exhausted"},
    )
    assert failed["status"] == "failed"


def stale_acks_and_terminal_guards(store: Store) -> None:
    task = create(store, "c-stale")
    base = {**context(task, "stale"), "task_id": task["task_id"], "message_id": task["current_message_id"]}
    assert_conflict(lambda: store.ack_v04_message(B, "msg_old", {**base, "message_id": "msg_old"}))
    assert_conflict(lambda: store.ack_v04_message(B, task["current_message_id"], {**base, "turn_sequence": 2}))
    assert_conflict(lambda: store.ack_v04_message(B, task["current_message_id"], {**base, "expected_status_version": 2}))
    task = response_delivered(store, task, "stale")
    requester_message = next(
        item for item in task["messages"] if item["from_agent_id"] == A
    )
    assert requester_message["message_id"] != task["current_message_id"]
    assert_conflict(
        lambda: store.complete_v04_task(
            task["task_id"], A,
            {**context(task, "wrong-evidence"), "completed_against_message_id": requester_message["message_id"]},
        )
    )
    terminal = store.complete_v04_task(
        task["task_id"], A,
        {**context(task, "right-evidence"), "completed_against_message_id": task["current_message_id"]},
    )
    assert_conflict(lambda: message(store, terminal, A, "terminal-message"))


def expiry(store: Store) -> None:
    submitted = create(store, "c-expire-submitted", expires_at=int(time.time()) + 1)
    time.sleep(1.05)
    assert store.get_task(submitted["task_id"])["status"] == "expired"
    delivered = create(store, "c-expire-delivered", expires_at=int(time.time()) + 1)
    delivered = ack(store, delivered, B, "expire-delivered-ack")
    time.sleep(1.05)
    expired = store.get_task(delivered["task_id"])
    assert expired["status"] == "expired" and expired["task_expires_at"] == delivered["task_expires_at"]


def failure_authority(store: Store) -> None:
    listener = create(store, "c-fail-listener")
    assert_conflict(
        lambda: store.fail_v04_task(listener["task_id"], A, {**context(listener, "bad-listener"), "reason": "listener_persistence_failed"})
    )
    assert store.fail_v04_task(
        listener["task_id"], B, {**context(listener, "listener-fail"), "reason": "listener_persistence_failed"},
    )["status"] == "failed"

    agent = ack(store, create(store, "c-fail-agent"), B, "fail-agent-ack")
    assert_conflict(
        lambda: store.fail_v04_task(agent["task_id"], A, {**context(agent, "bad-agent"), "reason": "agent_reported_failure"})
    )
    assert store.fail_v04_task(
        agent["task_id"], B, {**context(agent, "agent-fail"), "reason": "agent_reported_failure"},
    )["status"] == "failed"

    for reason in ("delivery_retry_exhausted", "relay_persistence_failed", "internal_consistency_error"):
        task = create(store, f"c-{reason}")
        assert_conflict(lambda task=task, reason=reason: store.fail_v04_task(
            task["task_id"], B, {**context(task, f"bad-{reason}"), "reason": reason},
        ))
        assert store.fail_v04_task(
            task["task_id"], "relay", {**context(task, f"relay-{reason}"), "reason": reason},
        )["status"] == "failed"


def lineage_and_concurrency(store: Store) -> None:
    root = response_delivered(store, create(store, "c-root"), "root")
    root = store.complete_v04_task(
        root["task_id"], A,
        {**context(root, "root-complete"), "completed_against_message_id": root["current_message_id"]},
    )

    def follow(index: int) -> dict:
        return store.create_task_v04(
            {
                "protocol_version": "agent-collab-v0.4",
                "idempotency_key": f"follow-{index}",
                "requester_agent_id": A,
                "target_agent_id": B,
                "done_criteria": f"follow-up {index}",
                "message": {"parts": [{"kind": "text", "text": str(index)}]},
            },
            source_task_id=root["task_id"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        children = list(pool.map(follow, (1, 2)))
    assert len({child["task_id"] for child in children}) == 2
    assert all(child["root_task_id"] == root["task_id"] for child in children)
    lineage = store.get_task_lineage(children[0]["task_id"])
    assert {task["task_id"] for task in lineage} == {root["task_id"], *(child["task_id"] for child in children)}
    with store.connect() as conn:
        events = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id = ? AND event_type = 'task.followup_created'",
            (root["task_id"],),
        ).fetchall()
    assert len(events) == 2 and all("source_task_id" in row["payload_json"] for row in events)


def deletion_and_compatibility(store: Store) -> None:
    assert not hasattr(store, "delete_task")
    v04 = create(store, "c-delete")
    with store.connect() as conn:
        task_fks = conn.execute("PRAGMA foreign_key_list(messages)").fetchall()
        assert all(str(row["on_delete"]).upper() != "CASCADE" for row in task_fks)
        try:
            conn.execute("DELETE FROM tasks WHERE task_id = ?", (v04["task_id"],))
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("Task hard delete succeeded")
    v03 = store.create_task(
        {
            "idempotency_key": "legacy-create",
            "requester_agent_id": A,
            "target_agent_id": B,
            "message": {"actor_agent_id": A, "parts": [{"kind": "text", "text": "legacy"}]},
        }
    )
    assert v03["protocol_version"] == "agent-collab-v0.3"
    assert store.get_task(v04["task_id"])["protocol_version"] == "agent-collab-v0.4"


def websocket_metadata() -> None:
    event = format_event_message({
        "event_id": "aevt_v04",
        "event_type": "task.message_pending",
        "agent_id": B,
        "task_id": "task_v04",
        "created_at": 1,
        "delivery_state": "pending",
        "delivery_attempts": 0,
        "payload": {
            "message_id": "msg_v04",
            "turn_sequence": 2,
            "status_version": 7,
            "from_agent_id": A,
            "to_agent_id": B,
            "parts": [{"kind": "text", "text": "must not enter WebSocket push"}],
        },
    })
    assert event["eventType"] == "task.message_pending"
    assert (event["messageId"], event["turnSequence"], event["statusVersion"]) == ("msg_v04", 2, 7)
    assert "parts" not in event


if __name__ == "__main__":
    main()
