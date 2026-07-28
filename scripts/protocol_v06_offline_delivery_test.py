from __future__ import annotations

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.protocol_v06 import PROTOCOL_V06
from server.store import ConflictError
from server.store_v06 import V06Store


REQUESTER = "zac-agent"
TARGET = "vivi-agent"
BASE = 1_785_210_000


def seed(path: Path) -> V06Store:
    store = V06Store(str(path))
    for agent_id in (REQUESTER, TARGET):
        store.upsert_agent(
            agent_id,
            name=agent_id,
            owner=agent_id,
            enabled=True,
            protocol_capabilities=[PROTOCOL_V06],
            now=BASE,
        )
    return store


def create_offline(store: V06Store, *, expires_at: int = BASE + 3600) -> dict:
    return store.create_task(
        {
            "protocol_version": PROTOCOL_V06,
            "idempotency_key": "offline-create",
            "requester_agent_id": REQUESTER,
            "target_agent_id": TARGET,
            "done_criteria": "target receives the request",
            "task_expires_at": expires_at,
            "message": {
                "subject": "Offline delivery",
                "parts": [{"kind": "text", "text": "ping"}],
            },
        },
        now=BASE,
    )


def register(store: V06Store, agent_id: str, instance: str, now: int) -> tuple[str, int]:
    readiness = store.register_listener(
        agent_id,
        listener_instance_id=instance,
        client_version="0.6.0",
        workspace_version="3",
        transport="websocket",
        now=now,
    )
    epoch = int(readiness["readiness_epoch"])
    store.publish_readiness(
        agent_id,
        listener_instance_id=instance,
        readiness_epoch=epoch,
        ready=True,
        now=now,
    )
    return instance, epoch


def ack_payload(task: dict, event: dict, instance: str, epoch: int) -> dict:
    current = task["task"]
    return {
        "task_id": current["task_id"],
        "event_id": event["event_id"],
        "message_id": current["current_message_id"],
        "turn_sequence": current["turn_sequence"],
        "expected_task_version": current["task_version"],
        "idempotency_key": f"ack-{event['event_id']}",
        "listener_instance_id": instance,
        "readiness_epoch": epoch,
    }


def offline_create_and_recovery(path: Path) -> None:
    store = seed(path)
    created = create_offline(store)
    visibility = store.visibility(created["task"]["task_id"], now=BASE)
    assert created["task"]["status"] == "open"
    assert visibility["current_message"]["delivery_status"] == "pending"
    assert visibility["outbox"]["outbox_status"] == "parked"
    assert visibility["diagnosis"] == "waiting_listener"

    instance, epoch = register(store, TARGET, "listener-vivi-1", BASE + 10)
    event = store.recover_event(
        TARGET,
        listener_instance_id=instance,
        readiness_epoch=epoch,
        now=BASE + 10,
    )
    assert event and event["inflight_via"] == "recovery"
    assert event["outbox_attempts"] == 0
    assert event["recovery_attempts"] == 1
    delivered = store.ack_message(
        TARGET,
        ack_payload(created, event, instance, epoch),
        now=BASE + 11,
    )
    assert delivered["task"]["status"] == "open"
    assert delivered["messages"][0]["delivery_status"] == "delivered"


def old_epoch_is_rejected(path: Path) -> None:
    store = seed(path)
    created = create_offline(store)
    old_instance, old_epoch = register(store, TARGET, "listener-vivi-old", BASE + 10)
    event = store.recover_event(
        TARGET,
        listener_instance_id=old_instance,
        readiness_epoch=old_epoch,
        now=BASE + 10,
    )
    register(store, TARGET, "listener-vivi-new", BASE + 11)
    try:
        store.ack_message(
            TARGET,
            ack_payload(created, event, old_instance, old_epoch),
            now=BASE + 12,
        )
    except ConflictError as exc:
        assert exc.code == "stale_readiness_epoch"
    else:
        raise AssertionError("old Listener epoch ACK unexpectedly succeeded")


def expired_notice_recovers(path: Path) -> None:
    store = seed(path)
    created = create_offline(store, expires_at=BASE + 5)
    assert store.expire_tasks(now=BASE + 5) == 1
    instance, epoch = register(store, TARGET, "listener-vivi-expired", BASE + 6)
    event = store.recover_event(
        TARGET,
        listener_instance_id=instance,
        readiness_epoch=epoch,
        now=BASE + 6,
    )
    assert event and event["event_type"] == "task.status_changed"
    detail = store.get_task_detail(created["task"]["task_id"])
    assert detail["task"]["status"] == "expired"


def push_failure_parks_and_recovery_lease_stays_parked(path: Path) -> None:
    store = seed(path)
    register(store, TARGET, "listener-vivi-push", BASE)
    created = create_offline(store)
    event = store.claim_due_event(TARGET, now=BASE)
    assert event and event["outbox_attempts"] == 1
    event_id = event["event_id"]
    store.record_attempt_failure(event_id, "listener_unavailable", now=BASE)
    visibility = store.visibility(created["task"]["task_id"], now=BASE)
    assert visibility["task"]["status"] == "open"
    assert visibility["current_message"]["delivery_status"] == "pending"
    assert visibility["outbox"]["outbox_status"] == "parked"
    assert visibility["diagnosis"] == "waiting_listener"
    assert TARGET not in store.list_due_agent_ids(now=BASE + 10_000)

    instance, epoch = register(store, TARGET, "listener-vivi-recovery", BASE + 1000)
    recovered = store.recover_event(
        TARGET,
        listener_instance_id=instance,
        readiness_epoch=epoch,
        now=BASE + 1000,
    )
    assert recovered and recovered["event_id"] == event_id
    assert recovered["outbox_attempts"] == 1
    store.expire_ack_leases(now=BASE + 1060)
    after_lease = store.visibility(created["task"]["task_id"], now=BASE + 1060)
    assert after_lease["outbox"]["outbox_status"] == "parked"
    assert after_lease["outbox"]["outbox_attempts"] == 1
    assert TARGET not in store.list_due_agent_ids(now=BASE + 10_000)


def business_failure_exhausts_parked_delivery(path: Path) -> None:
    store = seed(path)
    created = create_offline(store)
    task = created["task"]
    failed = store.fail_task(
        task["task_id"],
        {
            "actor_agent_id": "relay",
            "message_id": task["current_message_id"],
            "turn_sequence": task["turn_sequence"],
            "expected_task_version": task["task_version"],
            "idempotency_key": "relay-business-failure",
            "reason": "internal_consistency_error",
        },
        now=BASE + 1,
    )
    assert failed["task"]["status"] == "failed"
    visibility = store.visibility(task["task_id"], now=BASE + 1)
    assert visibility["outbox"]["outbox_status"] == "exhausted"
    assert visibility["outbox"]["exhaustion_reason"] == "task_failed"


def recovery_reclaims_its_expired_lease(path: Path) -> None:
    store = seed(path)
    created = create_offline(store)
    instance, epoch = register(store, TARGET, "listener-vivi-lease", BASE + 10)
    first = store.recover_event(
        TARGET,
        listener_instance_id=instance,
        readiness_epoch=epoch,
        now=BASE + 10,
    )
    assert first and first["inflight_via"] == "recovery"
    recovered = store.recover_event(
        TARGET,
        listener_instance_id=instance,
        readiness_epoch=epoch,
        now=BASE + 70,
    )
    assert recovered and recovered["event_id"] == first["event_id"]
    assert recovered["recovery_attempts"] == 2
    assert recovered["outbox_attempts"] == 0
    delivered = store.ack_message(
        TARGET,
        ack_payload(created, recovered, instance, epoch),
        now=BASE + 71,
    )
    assert delivered["messages"][0]["delivery_status"] == "delivered"

def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        offline_create_and_recovery(root / "offline.sqlite3")
        old_epoch_is_rejected(root / "epoch.sqlite3")
        expired_notice_recovers(root / "expired.sqlite3")
        push_failure_parks_and_recovery_lease_stays_parked(root / "push.sqlite3")
        business_failure_exhausts_parked_delivery(root / "failed.sqlite3")
        recovery_reclaims_its_expired_lease(root / "recovery-lease.sqlite3")
    print("protocol v0.6 offline delivery passed (6/6)")


if __name__ == "__main__":
    main()
