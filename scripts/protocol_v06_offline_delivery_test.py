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


def create_offline(
    store: V06Store,
    *,
    expires_at: int = BASE + 3600,
    key: str = "offline-create",
    metadata: dict | None = None,
) -> dict:
    return store.create_task(
        {
            "protocol_version": PROTOCOL_V06,
            "idempotency_key": key,
            "requester_agent_id": REQUESTER,
            "target_agent_id": TARGET,
            "done_criteria": "target receives the request",
            "task_expires_at": expires_at,
            "message": {
                "subject": "Offline delivery",
                "metadata": metadata,
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


def exact_deadline_rejects_late_mutations(path: Path) -> None:
    store = seed(path)
    target_listener = register(store, TARGET, "listener-vivi-deadline", BASE)

    late_ack = create_offline(store, expires_at=BASE + 5, key="late-ack")
    event = store.claim_due_event(TARGET, now=BASE + 1)
    try:
        store.ack_message(
            TARGET,
            ack_payload(late_ack, event, *target_listener),
            now=BASE + 5,
        )
    except ConflictError as exc:
        assert exc.code == "task_expired"
    else:
        raise AssertionError("ACK at the exact deadline unexpectedly succeeded")
    assert store.get_task_detail(late_ack["task"]["task_id"])["task"]["status"] == "expired"

    late_reply = create_offline(store, expires_at=BASE + 10, key="late-reply")
    event = store.claim_due_event(TARGET, now=BASE + 6)
    delivered = store.ack_message(
        TARGET,
        ack_payload(late_reply, event, *target_listener),
        now=BASE + 7,
    )
    current = delivered["task"]
    try:
        store.submit_message(
            current["task_id"],
            {
                "actor_agent_id": TARGET,
                "message_id": current["current_message_id"],
                "turn_sequence": current["turn_sequence"],
                "expected_task_version": current["task_version"],
                "idempotency_key": "late-result",
                "parts": [{"kind": "result", "status": "answered", "summary": "done"}],
            },
            now=BASE + 10,
        )
    except ConflictError as exc:
        assert exc.code == "task_expired"
    else:
        raise AssertionError("reply at the exact deadline unexpectedly succeeded")

    completable = create_offline(
        store,
        expires_at=BASE + 30,
        key="late-complete",
        metadata={
            "investigation_id": "inv-1",
            "round_id": "round-1",
            "work_item_id": "work-1",
        },
    )
    event = store.claim_due_event(TARGET, now=BASE + 11)
    delivered = store.ack_message(
        TARGET,
        ack_payload(completable, event, *target_listener),
        now=BASE + 12,
    )
    current = delivered["task"]
    replied = store.submit_message(
        current["task_id"],
        {
            "actor_agent_id": TARGET,
            "message_id": current["current_message_id"],
            "turn_sequence": current["turn_sequence"],
            "expected_task_version": current["task_version"],
            "idempotency_key": "valid-result",
            "parts": [{"kind": "result", "status": "blocked", "summary": "no access", "blocker": {"code": "access_denied"}}],
        },
        now=BASE + 13,
    )
    requester_listener = register(store, REQUESTER, "listener-zac-deadline", BASE + 13)
    event = store.recover_event(
        REQUESTER,
        listener_instance_id=requester_listener[0],
        readiness_epoch=requester_listener[1],
        now=BASE + 14,
    )
    accepted = store.ack_message(
        REQUESTER,
        ack_payload(replied, event, *requester_listener),
        now=BASE + 15,
    )
    current = accepted["task"]
    try:
        store.complete_task(
            current["task_id"],
            {
                "actor_agent_id": REQUESTER,
                "message_id": current["current_message_id"],
                "turn_sequence": current["turn_sequence"],
                "expected_task_version": current["task_version"],
                "idempotency_key": "late-completion",
                "completed_against_message_id": current["current_message_id"],
            },
            now=BASE + 30,
        )
    except ConflictError as exc:
        assert exc.code == "task_expired"
    else:
        raise AssertionError("completion at the exact deadline unexpectedly succeeded")
    with store.connect() as conn:
        audit = conn.execute(
            "SELECT payload_json FROM task_audit_events WHERE task_id = ? AND event_type = 'task.created'",
            (current["task_id"],),
        ).fetchone()
    assert '"investigation_id": "inv-1"' in audit["payload_json"]


def one_round_multi_agent_flow(path: Path) -> None:
    store = seed(path)
    targets = ("data-agent", "websdk-agent", "offline-agent")
    for target in targets:
        store.upsert_agent(
            target,
            name=target,
            owner=target,
            enabled=True,
            protocol_capabilities=[PROTOCOL_V06],
            now=BASE,
        )
    deadline = BASE + 100

    def create(target: str, key: str) -> dict:
        return store.create_task(
            {
                "protocol_version": PROTOCOL_V06,
                "idempotency_key": key,
                "requester_agent_id": REQUESTER,
                "target_agent_id": target,
                "done_criteria": "return one valid Result Packet",
                "max_turns": 1,
                "task_expires_at": deadline,
                "message": {
                    "subject": f"Investigate with {target}",
                    "metadata": {
                        "investigation_id": "inv-multi",
                        "round_id": "round-1",
                        "work_item_id": key,
                    },
                    "parts": [{"kind": "text", "text": f"bounded question for {target}"}],
                },
            },
            now=BASE,
        )

    created = {
        "data": create("data-agent", "work-data"),
        "web": create("websdk-agent", "work-web"),
        "offline": create("offline-agent", "work-offline"),
    }
    replayed = create("data-agent", "work-data")
    assert replayed["task"]["task_id"] == created["data"]["task"]["task_id"]
    try:
        store.create_task(
            {
                "protocol_version": PROTOCOL_V06,
                "idempotency_key": "work-data",
                "requester_agent_id": REQUESTER,
                "target_agent_id": "data-agent",
                "done_criteria": "different request",
                "max_turns": 1,
                "task_expires_at": deadline,
                "message": {"subject": "Different", "parts": [{"kind": "text", "text": "different"}]},
            },
            now=BASE,
        )
    except ConflictError:
        pass
    else:
        raise AssertionError("idempotency key reuse with a different request succeeded")

    requester_listener = register(store, REQUESTER, "listener-requester-multi", BASE + 1)

    def answer(label: str, target: str, packet: dict, at: int) -> None:
        target_listener = register(store, target, f"listener-{target}", at)
        event = store.recover_event(
            target,
            listener_instance_id=target_listener[0],
            readiness_epoch=target_listener[1],
            now=at,
        )
        delivered = store.ack_message(
            target,
            ack_payload(created[label], event, *target_listener),
            now=at + 1,
        )
        task = delivered["task"]
        replied = store.submit_message(
            task["task_id"],
            {
                "actor_agent_id": target,
                "message_id": task["current_message_id"],
                "turn_sequence": task["turn_sequence"],
                "expected_task_version": task["task_version"],
                "idempotency_key": f"result-{label}",
                "parts": [packet],
            },
            now=at + 2,
        )
        event = store.recover_event(
            REQUESTER,
            listener_instance_id=requester_listener[0],
            readiness_epoch=requester_listener[1],
            now=at + 3,
        )
        accepted = store.ack_message(
            REQUESTER,
            ack_payload(replied, event, *requester_listener),
            now=at + 4,
        )
        task = accepted["task"]
        completed = store.complete_task(
            task["task_id"],
            {
                "actor_agent_id": REQUESTER,
                "message_id": task["current_message_id"],
                "turn_sequence": task["turn_sequence"],
                "expected_task_version": task["task_version"],
                "idempotency_key": f"complete-{label}",
                "completed_against_message_id": task["current_message_id"],
            },
            now=at + 5,
        )
        assert completed["task"]["status"] == "completed"

    answer(
        "data",
        "data-agent",
        {"kind": "result", "status": "answered", "summary": "three rows", "data": {"rows": 3}},
        BASE + 2,
    )
    answer(
        "web",
        "websdk-agent",
        {"kind": "result", "status": "blocked", "summary": "no trace access", "blocker": {"code": "access_denied"}},
        BASE + 20,
    )
    assert store.expire_tasks(now=deadline) == 1
    batch = store.visibility_batch(
        [value["task"]["task_id"] for value in created.values()], now=deadline
    )
    assert [item["task"]["status"] for item in batch["items"]] == [
        "completed", "completed", "expired"
    ]
    assert batch["errors"] == []


def install_healthcheck_has_governed_profile(path: Path) -> None:
    store = seed(path)
    store.create_install_healthcheck(
        REQUESTER,
        idempotency_key="install-healthcheck-profile",
        now=BASE,
    )
    profile = store.get_agent_profile("agentrelay-healthcheck")
    assert profile is not None
    assert profile["agent_role"] == "service_agent"
    assert any(
        agent["agent_id"] == "agentrelay-healthcheck"
        for agent in store.list_agents(now=BASE)
    )

def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        offline_create_and_recovery(root / "offline.sqlite3")
        old_epoch_is_rejected(root / "epoch.sqlite3")
        expired_notice_recovers(root / "expired.sqlite3")
        push_failure_parks_and_recovery_lease_stays_parked(root / "push.sqlite3")
        business_failure_exhausts_parked_delivery(root / "failed.sqlite3")
        recovery_reclaims_its_expired_lease(root / "recovery-lease.sqlite3")
        exact_deadline_rejects_late_mutations(root / "deadline.sqlite3")
        one_round_multi_agent_flow(root / "multi-agent.sqlite3")
        install_healthcheck_has_governed_profile(root / "healthcheck-profile.sqlite3")
    print("protocol v0.6 offline delivery passed (9/9)")


if __name__ == "__main__":
    main()
