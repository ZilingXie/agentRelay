from __future__ import annotations

import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.protocol_v05 import PROTOCOL_V05
from server.store import ConflictError
from server.store_v05 import V05Store


A = "zac-agent"
B = "frank-agent"
BASE = 10_000


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        scenarios = (
            (native_schema_and_admission, BASE),
            (round_trip_completion, BASE + 303),
            (retry_exhaustion, BASE + 400),
            (guarded_nack_and_expiry, BASE + 1500),
            (followup_and_hard_delete, BASE + 1600),
            (concurrent_idempotency_and_ack, BASE + 2000),
        )
        for index, (scenario, readiness_time) in enumerate(scenarios):
            store = V05Store(f"{tmp}/v05-{index}.sqlite3")
            listener_state = prepare_agents(store, readiness_time)
            scenario(store, listener_state)
    print("protocol v0.5 native Store conformance passed (20/20)")


def prepare_agents(store: V05Store, readiness_time: int) -> dict[str, tuple[str, int]]:
    state: dict[str, tuple[str, int]] = {}
    for agent_id in (A, B):
        store.upsert_agent(
            agent_id,
            name=agent_id,
            owner=agent_id,
            enabled=True,
            protocol_capabilities=[PROTOCOL_V05],
            now=readiness_time,
        )
        instance_id = f"listener-{agent_id}-1"
        registered = store.register_listener(
            agent_id,
            listener_instance_id=instance_id,
            client_version="0.5.0",
            workspace_version="2",
            transport="websocket",
            now=readiness_time,
        )
        store.publish_readiness(
            agent_id,
            listener_instance_id=instance_id,
            readiness_epoch=registered["readiness_epoch"],
            ready=True,
            now=readiness_time,
        )
        state[agent_id] = (instance_id, registered["readiness_epoch"])
    return state


def create(
    store: V05Store,
    key: str,
    *,
    now: int,
    max_turns: int = 3,
    expires_at: int | None = None,
    source_task_id: str | None = None,
) -> dict:
    payload = {
        "protocol_version": PROTOCOL_V05,
        "idempotency_key": key,
        "requester_agent_id": A,
        "target_agent_id": B,
        "done_criteria": "accepted target response",
        "max_turns": max_turns,
        "task_expires_at": expires_at or now + 3600,
        "message": {"parts": [{"kind": "text", "text": key}]},
    }
    return store.create_task(payload, source_task_id=source_task_id, now=now)


def context(detail: dict, key: str) -> dict:
    task = detail["task"]
    return {
        "message_id": task["current_message_id"],
        "turn_sequence": task["turn_sequence"],
        "expected_task_version": task["task_version"],
        "idempotency_key": key,
    }


def claim(store: V05Store, agent_id: str, now: int) -> dict:
    event = store.claim_due_event(agent_id, now=now)
    assert event is not None
    return event


def ack(
    store: V05Store,
    detail: dict,
    event: dict,
    agent_id: str,
    listener: tuple[str, int],
    key: str,
    now: int,
) -> dict:
    instance_id, epoch = listener
    return store.ack_message(
        agent_id,
        {
            **context(detail, key),
            "task_id": detail["task"]["task_id"],
            "event_id": event["event_id"],
            "listener_instance_id": instance_id,
            "readiness_epoch": epoch,
        },
        now=now,
    )


def respond_and_ack(
    store: V05Store,
    detail: dict,
    listeners: dict[str, tuple[str, int]],
    suffix: str,
    now: int,
) -> dict:
    request_event = claim(store, B, now)
    detail = ack(store, detail, request_event, B, listeners[B], f"ack-request-{suffix}", now + 1)
    detail = store.submit_message(
        detail["task"]["task_id"],
        {
            **context(detail, f"response-{suffix}"),
            "actor_agent_id": B,
            "parts": [{"kind": "text", "text": suffix}],
        },
        now=now + 2,
    )
    response_event = claim(store, A, now + 2)
    return ack(store, detail, response_event, A, listeners[A], f"ack-response-{suffix}", now + 3)


def assert_conflict(callable_, code: str | None = None) -> None:
    try:
        callable_()
    except ConflictError as exc:
        if code is not None:
            assert exc.code == code, (exc.code, code)
        return
    raise AssertionError("expected ConflictError")


def native_schema_and_admission(
    store: V05Store, listeners: dict[str, tuple[str, int]]
) -> None:
    with store.connect() as conn:
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert "task_version" in task_columns
        assert "delivery_status" not in task_columns
        assert "status_version" not in task_columns

    first = create(store, "native-create", now=BASE + 1)
    duplicate = create(store, "native-create", now=BASE + 1)
    assert duplicate["task"]["task_id"] == first["task"]["task_id"]
    assert_conflict(
        lambda: store.create_task(
            {
                "protocol_version": PROTOCOL_V05,
                "idempotency_key": "native-create",
                "requester_agent_id": A,
                "target_agent_id": B,
                "done_criteria": "different",
                "message": {"parts": [{"kind": "text", "text": "different"}]},
            },
            now=BASE + 1,
        )
    )
    assert_conflict(lambda: create(store, "stale-readiness", now=BASE + 301), "listener_not_ready")

    old_instance, old_epoch = listeners[A]
    replacement = store.register_listener(
        A,
        listener_instance_id="listener-zac-agent-2",
        client_version="0.5.0",
        workspace_version="2",
        transport="websocket",
        now=BASE + 302,
    )
    assert replacement["readiness_epoch"] == old_epoch + 1
    assert_conflict(
        lambda: store.publish_readiness(
            A,
            listener_instance_id=old_instance,
            readiness_epoch=old_epoch,
            ready=True,
            now=BASE + 302,
        ),
        "stale_readiness_epoch",
    )
    store.publish_readiness(
        A,
        listener_instance_id="listener-zac-agent-2",
        readiness_epoch=replacement["readiness_epoch"],
        ready=True,
        now=BASE + 302,
    )
    b_instance, b_epoch = listeners[B]
    store.publish_readiness(
        B,
        listener_instance_id=b_instance,
        readiness_epoch=b_epoch,
        ready=True,
        now=BASE + 302,
    )
    listeners[A] = ("listener-zac-agent-2", replacement["readiness_epoch"])


def round_trip_completion(
    store: V05Store, listeners: dict[str, tuple[str, int]]
) -> None:
    now = BASE + 303
    detail = create(store, "round-trip", now=now)
    assert detail["task"]["status"] == "open"
    assert detail["messages"][0]["delivery_status"] == "pending"
    assert store.visibility(detail["task"]["task_id"], now=now)["diagnosis"] == "message_queued"

    request_event = claim(store, B, now)
    ack_payload = {
        **context(detail, "round-trip-request-ack"),
        "task_id": detail["task"]["task_id"],
        "event_id": request_event["event_id"],
        "listener_instance_id": listeners[B][0],
        "readiness_epoch": listeners[B][1],
    }
    detail = store.ack_message(B, ack_payload, now=now + 1)
    assert detail["task"]["task_version"] == 2
    assert store.visibility(detail["task"]["task_id"], now=now + 1)["diagnosis"] == "waiting_target_response"
    duplicate_ack = store.ack_message(B, ack_payload, now=now + 2)
    assert duplicate_ack["task"]["task_version"] == 2

    assert_conflict(
        lambda: store.submit_message(
            detail["task"]["task_id"],
            {
                **context(detail, "same-agent"),
                "actor_agent_id": A,
                "parts": [{"kind": "text", "text": "invalid"}],
            },
            now=now + 2,
        )
    )
    detail = store.submit_message(
        detail["task"]["task_id"],
        {
            **context(detail, "target-response"),
            "actor_agent_id": B,
            "parts": [{"kind": "text", "text": "done"}],
        },
        now=now + 2,
    )
    assert detail["task"]["turn_sequence"] == 1
    response_event = claim(store, A, now + 2)
    detail = ack(store, detail, response_event, A, listeners[A], "response-ack", now + 3)
    assert store.visibility(detail["task"]["task_id"], now=now + 3)["diagnosis"] == "waiting_requester_decision"
    completed = store.complete_task(
        detail["task"]["task_id"],
        {
            **context(detail, "complete"),
            "actor_agent_id": A,
            "completed_against_message_id": detail["task"]["current_message_id"],
        },
        now=now + 4,
    )
    assert completed["task"]["status"] == "completed"
    assert store.visibility(completed["task"]["task_id"], now=now + 4)["diagnosis"] == "task_completed"


def retry_exhaustion(store: V05Store, listeners: dict[str, tuple[str, int]]) -> None:
    del listeners
    now = BASE + 400
    detail = create(store, "retry-exhaustion", now=now)
    task_id = detail["task"]["task_id"]
    expected_claim_times = [now, now + 60, now + 360, now + 960]
    for attempt, claim_time in enumerate(expected_claim_times, start=1):
        event = claim(store, B, claim_time)
        assert event["outbox_attempts"] == attempt
        failed = store.record_attempt_failure(event["event_id"], "listener_unavailable", now=claim_time)
        if attempt < 4:
            assert failed["outbox_status"] == "retry_wait"
            assert store.claim_due_event(B, now=failed["next_retry_at"] - 1) is None
        else:
            assert failed["outbox_status"] == "exhausted"
    terminal = store.get_task_detail(task_id)
    assert terminal["task"]["status"] == "failed"
    assert terminal["messages"][0]["delivery_status"] == "failed"
    assert store.visibility(task_id, now=now + 960)["diagnosis"] == "task_failed_delivery"


def guarded_nack_and_expiry(
    store: V05Store, listeners: dict[str, tuple[str, int]]
) -> None:
    now = BASE + 1500
    nack = create(store, "guarded-nack", now=now)
    event = claim(store, B, now)
    failed = store.fail_delivery(
        B,
        {
            **context(nack, "guarded-nack-fail"),
            "task_id": nack["task"]["task_id"],
            "event_id": event["event_id"],
            "listener_instance_id": listeners[B][0],
            "readiness_epoch": listeners[B][1],
            "reason": "listener_persistence_failed",
        },
        now=now + 1,
    )
    assert failed["task"]["status"] == "failed"
    assert failed["task"]["terminal_by_agent_id"] == B

    expiring = create(store, "expiry", now=now, expires_at=now + 2)
    assert store.expire_tasks(now=now + 1) == 0
    assert store.expire_tasks(now=now + 2) == 1
    expired = store.get_task_detail(expiring["task"]["task_id"])
    assert expired["task"]["status"] == "expired"
    assert store.visibility(expired["task"]["task_id"], now=now + 2)["diagnosis"] == "task_expired"


def followup_and_hard_delete(
    store: V05Store, listeners: dict[str, tuple[str, int]]
) -> None:
    now = BASE + 1600
    root = respond_and_ack(store, create(store, "root", now=now), listeners, "root", now)
    root = store.complete_task(
        root["task"]["task_id"],
        {
            **context(root, "root-complete"),
            "actor_agent_id": A,
            "completed_against_message_id": root["task"]["current_message_id"],
        },
        now=now + 4,
    )
    child = create(
        store,
        "child",
        now=now + 5,
        source_task_id=root["task"]["task_id"],
    )
    assert child["task"]["root_task_id"] == root["task"]["task_id"]
    assert store.get_task_detail(root["task"]["task_id"])["task"]["status"] == "completed"
    lineage = store.get_lineage(child["task"]["task_id"])
    assert {item["task_id"] for item in lineage} == {
        root["task"]["task_id"], child["task"]["task_id"]
    }
    try:
        with store.connect() as conn:
            conn.execute("DELETE FROM tasks WHERE task_id = ?", (child["task"]["task_id"],))
    except sqlite3.IntegrityError as exc:
        assert "forbids hard deletion" in str(exc)
    else:
        raise AssertionError("hard delete unexpectedly succeeded")


def concurrent_idempotency_and_ack(
    store: V05Store, listeners: dict[str, tuple[str, int]]
) -> None:
    now = BASE + 2000
    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(lambda _: create(store, "concurrent-create", now=now), range(2)))
    assert len({item["task"]["task_id"] for item in created}) == 1
    detail = created[0]
    event = claim(store, B, now)

    def ack_once(index: int) -> tuple[str, dict | ConflictError]:
        payload = {
            **context(detail, f"concurrent-ack-{index}"),
            "task_id": detail["task"]["task_id"],
            "event_id": event["event_id"],
            "listener_instance_id": listeners[B][0],
            "readiness_epoch": listeners[B][1],
        }
        try:
            return "ok", store.ack_message(B, payload, now=now + 1)
        except ConflictError as exc:
            return "conflict", exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ack_once, range(2)))
    assert [kind for kind, _ in results].count("ok") == 1
    assert [kind for kind, _ in results].count("conflict") == 1
    latest = store.get_task_detail(detail["task"]["task_id"])
    assert latest["task"]["task_version"] == 2
    assert latest["messages"][0]["delivery_status"] == "delivered"


if __name__ == "__main__":
    main()
