from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.delivery_coordinator import DeliveryCoordinator
from server.protocol_v05 import PROTOCOL_V05
from server.store_v05 import V05Store


A = "zac-agent"
B = "frank-agent"
BASE = 30_000


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        offline_exhaustion(Path(tmp) / "offline.sqlite3")
        socket_delivery_and_lease(Path(tmp) / "socket.sqlite3")
        lease_exhaustion(Path(tmp) / "lease-exhaustion.sqlite3")
        epoch_replacement(Path(tmp) / "epoch.sqlite3")
        restart_retry_persistence(Path(tmp) / "restart.sqlite3")
        informational_event_exhaustion(Path(tmp) / "informational.sqlite3")
    print("protocol v0.5 delivery coordinator passed (20/20)")


def prepare(path: Path, now: int) -> tuple[V05Store, dict[str, tuple[str, int]]]:
    store = V05Store(str(path))
    listeners: dict[str, tuple[str, int]] = {}
    for agent_id in (A, B):
        store.upsert_agent(
            agent_id,
            name=agent_id,
            owner=agent_id,
            enabled=True,
            protocol_capabilities=[PROTOCOL_V05],
            now=now,
        )
        instance_id = f"listener-{agent_id}-1"
        readiness = store.register_listener(
            agent_id,
            listener_instance_id=instance_id,
            client_version="0.5.0",
            workspace_version="2",
            transport="websocket",
            now=now,
        )
        store.publish_readiness(
            agent_id,
            listener_instance_id=instance_id,
            readiness_epoch=readiness["readiness_epoch"],
            ready=True,
            now=now,
        )
        listeners[agent_id] = (instance_id, readiness["readiness_epoch"])
    return store, listeners


def create(store: V05Store, key: str, now: int) -> dict:
    return store.create_task(
        {
            "protocol_version": PROTOCOL_V05,
            "idempotency_key": key,
            "requester_agent_id": A,
            "target_agent_id": B,
            "done_criteria": "response",
            "task_expires_at": now + 5000,
            "message": {"parts": [{"kind": "text", "text": key}]},
        },
        now=now,
    )


def offline_exhaustion(path: Path) -> None:
    store, _ = prepare(path, BASE)
    task = create(store, "offline", BASE)
    coordinator = DeliveryCoordinator(store)
    for expected_attempt, timestamp in enumerate((BASE, BASE + 60, BASE + 360, BASE + 960), start=1):
        result = coordinator.run_once(now=timestamp)
        assert result["claimed"] >= 1 and result["attempt_failures"] == result["claimed"]
        visibility = store.visibility(task["task"]["task_id"], now=timestamp)
        assert visibility["outbox"]["outbox_attempts"] == expected_attempt
    visibility = store.visibility(task["task"]["task_id"], now=BASE + 960)
    assert visibility["diagnosis"] == "task_failed_delivery"
    summary = store.admin_summary(now=BASE + 960)
    assert {item["code"] for item in summary["alerts"]} >= {"exhausted_outbox"}


def socket_delivery_and_lease(path: Path) -> None:
    now = BASE + 2000
    store, listeners = prepare(path, now)
    task = create(store, "socket", now)
    sent: list[dict] = []
    coordinator = DeliveryCoordinator(store)
    coordinator.register_socket(B, *listeners[B], sent.append)
    result = coordinator.run_once(now=now)
    assert result["sent"] == 1 and sent[0]["messageId"] == task["task"]["current_message_id"]
    inflight = store.visibility(task["task"]["task_id"], now=now)
    assert inflight["diagnosis"] == "message_inflight"
    lease_result = coordinator.run_once(now=now + 60)
    assert lease_result["expired_leases"] == 1
    waiting = store.visibility(task["task"]["task_id"], now=now + 60)
    assert waiting["diagnosis"] == "message_pending_retry"


def epoch_replacement(path: Path) -> None:
    now = BASE + 3000
    store, listeners = prepare(path, now)
    coordinator = DeliveryCoordinator(store)
    old_sent: list[dict] = []
    new_sent: list[dict] = []
    old_closed: list[bool] = []
    coordinator.register_socket(
        B, *listeners[B], old_sent.append, close=lambda: old_closed.append(True)
    )
    replacement = store.register_listener(
        B,
        listener_instance_id="listener-frank-agent-2",
        client_version="0.5.0",
        workspace_version="2",
        transport="websocket",
        now=now + 1,
    )
    store.publish_readiness(
        B,
        listener_instance_id="listener-frank-agent-2",
        readiness_epoch=replacement["readiness_epoch"],
        ready=True,
        now=now + 1,
    )
    sweep = coordinator.run_once(now=now + 1)
    assert sweep["stale_sockets_closed"] == 1
    assert old_closed == [True]
    coordinator.register_socket(
        B,
        "listener-frank-agent-2",
        replacement["readiness_epoch"],
        new_sent.append,
    )
    task = create(store, "epoch", now + 1)
    result = coordinator.run_once(now=now + 1)
    assert result["sent"] == 1
    assert old_sent == [] and new_sent[0]["taskId"] == task["task"]["task_id"]


def lease_exhaustion(path: Path) -> None:
    now = BASE + 4000
    store, listeners = prepare(path, now)
    task = create(store, "lease-exhaustion", now)
    sent: list[dict] = []
    coordinator = DeliveryCoordinator(store)
    coordinator.register_socket(B, *listeners[B], sent.append)
    claim_times = (now, now + 120, now + 480, now + 1140)
    failure_times = (now + 60, now + 180, now + 540, now + 1200)
    for attempt, (claim_time, failure_time) in enumerate(
        zip(claim_times, failure_times), start=1
    ):
        sent_result = coordinator.run_once(now=claim_time)
        assert sent_result["sent"] == 1
        visibility = store.visibility(task["task"]["task_id"], now=claim_time)
        assert visibility["outbox"]["outbox_attempts"] == attempt
        lease_result = coordinator.run_once(now=failure_time)
        assert lease_result["expired_leases"] == 1
    terminal = store.visibility(task["task"]["task_id"], now=failure_times[-1])
    assert terminal["diagnosis"] == "task_failed_delivery"
    assert len([event for event in sent if event["canTransitionMessage"]]) == 4
    assert any(
        event["type"] == "task.status_changed" and not event["canTransitionMessage"]
        for event in sent
    )


def restart_retry_persistence(path: Path) -> None:
    now = BASE + 6000
    store, _ = prepare(path, now)
    task = create(store, "restart", now)
    coordinator = DeliveryCoordinator(store)
    first = coordinator.run_once(now=now)
    assert first["attempt_failures"] >= 1
    before_restart = store.visibility(task["task"]["task_id"], now=now)
    next_retry_at = before_restart["outbox"]["next_retry_at"]

    restarted = V05Store(str(path))
    after_restart = restarted.visibility(task["task"]["task_id"], now=now + 1)
    assert after_restart["outbox"]["next_retry_at"] == next_retry_at
    assert restarted.claim_due_event(B, now=next_retry_at - 1) is None
    second = restarted.claim_due_event(B, now=next_retry_at)
    assert second is not None and second["outbox_attempts"] == 2


def informational_event_exhaustion(path: Path) -> None:
    now = BASE + 7000
    store, _ = prepare(path, now)
    detail = create(store, "informational", now)
    task_id = detail["task"]["task_id"]
    message_event = store.claim_due_event(B, now=now)
    assert message_event is not None and message_event["can_transition_message"]
    store.record_attempt_failure(
        message_event["event_id"], "listener_unavailable", now=now
    )

    attempt_time = now
    informational_event = None
    for expected_attempt in range(1, 5):
        informational_event = store.claim_due_event(A, now=attempt_time)
        assert informational_event is not None
        assert informational_event["can_transition_message"] is False
        assert informational_event["outbox_attempts"] == expected_attempt
        informational_event = store.record_attempt_failure(
            informational_event["event_id"], "listener_unavailable", now=attempt_time
        )
        if informational_event["next_retry_at"] is not None:
            attempt_time = informational_event["next_retry_at"]

    assert informational_event["outbox_status"] == "exhausted"
    after = store.get_task_detail(task_id)
    assert after["task"]["status"] == "open"
    assert after["task"]["task_version"] == detail["task"]["task_version"]
    assert after["messages"][0]["delivery_status"] == "pending"
    summary = store.admin_summary(now=attempt_time)
    assert summary["outbox"]["exhausted"] == 1
    assert "exhausted_outbox" not in {item["code"] for item in summary["alerts"]}


if __name__ == "__main__":
    main()
