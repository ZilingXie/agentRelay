from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import threading
from http.server import ThreadingHTTPServer
import sys
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.delivery_control import DeliveryControl, DeliveryLane
from server.delivery_coordinator import DeliveryCoordinator
from server.delivery_wakeup import DeliveryWakeClient
from server.protocol_v05 import PROTOCOL_V05
from server.protocol_v06 import PROTOCOL_V06
from server.store_v05 import V05Store
from server.store_v06 import V06Store
from server.ws_app import AgentRelayWebSocketHandler


REQUESTER = "flow-requester"
TARGET = "flow-target"
SECOND_TARGET = "flow-target-two"
BASE = 1_786_000_000


def make_control(root: Path, lanes: list[tuple[str, Path]]) -> DeliveryControl:
    return DeliveryControl(
        root / "delivery-control.sqlite3",
        [DeliveryLane(protocol, path) for protocol, path in lanes],
    )


def seed_store(
    store: V05Store | V06Store,
    protocol: str,
    *,
    ready_agents: tuple[str, ...] = (),
    now: int = BASE,
) -> dict[str, tuple[str, int]]:
    listeners: dict[str, tuple[str, int]] = {}
    for agent_id in (REQUESTER, TARGET, SECOND_TARGET):
        store.upsert_agent(
            agent_id,
            name=agent_id,
            owner=agent_id,
            enabled=True,
            protocol_capabilities=[protocol],
            now=now,
        )
    for agent_id in ready_agents:
        instance_id = f"listener-{agent_id}"
        readiness = store.register_listener(
            agent_id,
            listener_instance_id=instance_id,
            client_version="0.6.0" if protocol == PROTOCOL_V06 else "0.5.0",
            workspace_version="3" if protocol == PROTOCOL_V06 else "2",
            transport="websocket",
            now=now,
        )
        epoch = int(readiness["readiness_epoch"])
        store.publish_readiness(
            agent_id,
            listener_instance_id=instance_id,
            readiness_epoch=epoch,
            ready=True,
            now=now,
        )
        listeners[agent_id] = (instance_id, epoch)
    return listeners


def create_task(
    store: V05Store | V06Store,
    protocol: str,
    target: str,
    key: str,
    now: int,
) -> dict:
    return store.create_task(
        {
            "protocol_version": protocol,
            "idempotency_key": key,
            "requester_agent_id": REQUESTER,
            "target_agent_id": target,
            "done_criteria": "deliver exactly once",
            "task_expires_at": now + 3600,
            "message": {
                "subject": key,
                "parts": [{"kind": "text", "text": key}],
            },
        },
        now=now,
    )


def ack_payload(
    created: dict,
    event_id: str,
    listener: tuple[str, int],
    key: str,
) -> dict:
    task = created["task"]
    return {
        "task_id": task["task_id"],
        "event_id": event_id,
        "message_id": task["current_message_id"],
        "turn_sequence": task["turn_sequence"],
        "expected_task_version": task["task_version"],
        "idempotency_key": key,
        "listener_instance_id": listener[0],
        "readiness_epoch": listener[1],
    }


def agent_metrics(control: DeliveryControl, agent_id: str) -> dict:
    return next(
        item for item in control.metrics_summary()["agents"]
        if item["agent_id"] == agent_id
    )


def default_limit_serializes_five(root: Path) -> None:
    db_path = root / "serial.sqlite3"
    control = make_control(root, [(PROTOCOL_V06, db_path)])
    store = V06Store(str(db_path), delivery_control=control)
    listeners = seed_store(store, PROTOCOL_V06, ready_agents=(TARGET,))
    created = [
        create_task(store, PROTOCOL_V06, TARGET, f"serial-{index}", BASE + index)
        for index in range(5)
    ]
    by_task = {item["task"]["task_id"]: item for item in created}
    sent: list[dict] = []
    coordinator = DeliveryCoordinator(store)
    coordinator.register_socket(TARGET, *listeners[TARGET], sent.append)
    latencies = [1, 2, 3, 4, 5]

    assert control.get_max_inflight(TARGET, now=BASE) == 1
    for index, latency in enumerate(latencies):
        claim_at = BASE + 20 + index * 10
        before = len(sent)
        coordinator.run_once(now=claim_at)
        assert len(sent) == before + 1
        assert control.inflight_count(TARGET) == 1
        message = sent[-1]
        payload = ack_payload(
            by_task[message["taskId"]],
            message["eventId"],
            listeners[TARGET],
            f"serial-ack-{index}",
        )
        first = store.ack_message(TARGET, payload, now=claim_at + latency)
        if index == 0:
            duplicate = store.ack_message(TARGET, payload, now=claim_at + latency)
            assert duplicate == first
        assert control.inflight_count(TARGET) == 0

    coordinator.run_once(now=BASE + 100)
    event_ids = [item["eventId"] for item in sent]
    assert len(event_ids) == 5
    assert len(set(event_ids)) == 5
    metrics = agent_metrics(control, TARGET)
    assert metrics["max_inflight"] == 1
    assert (metrics["queued"], metrics["inflight"], metrics["parked"]) == (0, 0, 0)
    assert metrics["ack_latency_seconds"] == {
        "count": 5,
        "p50": 3,
        "p95": 5,
        "max": 5,
    }


def configured_limit_and_agent_isolation(root: Path) -> None:
    db_path = root / "configured.sqlite3"
    control = make_control(root, [(PROTOCOL_V06, db_path)])
    store = V06Store(str(db_path), delivery_control=control)
    listeners = seed_store(
        store,
        PROTOCOL_V06,
        ready_agents=(TARGET, SECOND_TARGET),
    )
    control.set_max_inflight(TARGET, 2, now=BASE)
    assert control.get_max_inflight(TARGET, now=BASE + 1) == 2
    for invalid in (0, 101):
        try:
            control.set_max_inflight(TARGET, invalid, now=BASE)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid max_inflight: {invalid}")
    for index in range(3):
        create_task(store, PROTOCOL_V06, TARGET, f"configured-{index}", BASE + index)
    create_task(store, PROTOCOL_V06, SECOND_TARGET, "isolated", BASE)

    sent: list[dict] = []
    coordinator = DeliveryCoordinator(store)
    coordinator.register_socket(TARGET, *listeners[TARGET], sent.append)
    coordinator.register_socket(
        SECOND_TARGET, *listeners[SECOND_TARGET], sent.append
    )
    result = coordinator.run_once(now=BASE + 10)
    assert result["sent"] == 3
    assert control.inflight_count(TARGET) == 2
    assert control.inflight_count(SECOND_TARGET) == 1
    metrics = agent_metrics(control, TARGET)
    assert metrics["max_inflight"] == 2
    assert (metrics["queued"], metrics["inflight"], metrics["parked"]) == (1, 2, 0)
    assert metrics["ack_latency_seconds"] == {
        "count": 0,
        "p50": None,
        "p95": None,
        "max": None,
    }


def cross_lane_claim_is_global(root: Path) -> None:
    v05_path = root / "lane-v05.sqlite3"
    v06_path = root / "lane-v06.sqlite3"
    control = make_control(
        root,
        [(PROTOCOL_V05, v05_path), (PROTOCOL_V06, v06_path)],
    )
    v05 = V05Store(str(v05_path), delivery_control=control)
    v06 = V06Store(str(v06_path), delivery_control=control)
    seed_store(v05, PROTOCOL_V05, ready_agents=(REQUESTER, TARGET))
    seed_store(v06, PROTOCOL_V06, ready_agents=(TARGET,))
    create_task(v05, PROTOCOL_V05, TARGET, "lane-v05", BASE)
    create_task(v06, PROTOCOL_V06, TARGET, "lane-v06", BASE)
    barrier = threading.Barrier(2)

    def claim(store: V05Store | V06Store) -> dict | None:
        barrier.wait()
        return store.claim_due_event(TARGET, now=BASE + 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (v05, v06)))
    assert sum(item is not None for item in results) == 1
    assert control.inflight_count(TARGET) == 1


def lease_expiry_parks_and_releases_capacity(root: Path) -> None:
    db_path = root / "lease.sqlite3"
    control = make_control(root, [(PROTOCOL_V06, db_path)])
    store = V06Store(str(db_path), delivery_control=control)
    listeners = seed_store(store, PROTOCOL_V06, ready_agents=(TARGET,))
    first = create_task(store, PROTOCOL_V06, TARGET, "lease-first", BASE)
    second = create_task(store, PROTOCOL_V06, TARGET, "lease-second", BASE + 1)
    by_task = {
        first["task"]["task_id"]: first,
        second["task"]["task_id"]: second,
    }
    sent: list[dict] = []
    coordinator = DeliveryCoordinator(store)
    coordinator.register_socket(TARGET, *listeners[TARGET], sent.append)
    coordinator.run_once(now=BASE + 10)
    assert len(sent) == 1

    result = coordinator.run_once(now=BASE + 70)
    assert result["expired_leases"] == 1
    assert len(sent) == 2
    assert control.inflight_count(TARGET) == 1
    first_visibility = store.visibility(first["task"]["task_id"], now=BASE + 70)
    assert first_visibility["task"]["status"] == "open"
    assert first_visibility["current_message"]["delivery_status"] == "pending"
    assert first_visibility["outbox"]["outbox_status"] == "parked"
    assert first_visibility["outbox"]["last_error"] == "ack_lease_expired"
    assert first_visibility["diagnosis"] == "waiting_listener"

    second_message = sent[-1]
    store.ack_message(
        TARGET,
        ack_payload(
            by_task[second_message["taskId"]],
            second_message["eventId"],
            listeners[TARGET],
            "lease-second-ack",
        ),
        now=BASE + 71,
    )
    recovered = store.recover_event(
        TARGET,
        listener_instance_id=listeners[TARGET][0],
        readiness_epoch=listeners[TARGET][1],
        now=BASE + 80,
    )
    assert recovered and recovered["event_id"] == first_visibility["outbox"]["event_id"]
    store.ack_message(
        TARGET,
        ack_payload(first, recovered["event_id"], listeners[TARGET], "lease-first-ack"),
        now=BASE + 90,
    )
    metrics = agent_metrics(control, TARGET)
    assert metrics["recovery_latency_seconds"] == {
        "count": 1,
        "p50": 20,
        "p95": 20,
        "max": 20,
    }


def push_recovery_race_has_one_lease(root: Path) -> None:
    db_path = root / "race.sqlite3"
    control = make_control(root, [(PROTOCOL_V06, db_path)])
    store = V06Store(str(db_path), delivery_control=control)
    seed_store(store, PROTOCOL_V06)
    create_task(store, PROTOCOL_V06, TARGET, "race-parked", BASE)
    listeners = seed_store(store, PROTOCOL_V06, ready_agents=(TARGET,), now=BASE + 1)
    create_task(store, PROTOCOL_V06, TARGET, "race-queued", BASE + 2)
    barrier = threading.Barrier(2)

    def push() -> dict | None:
        barrier.wait()
        return store.claim_due_event(TARGET, now=BASE + 3)

    def recover() -> dict | None:
        barrier.wait()
        return store.recover_event(
            TARGET,
            listener_instance_id=listeners[TARGET][0],
            readiness_epoch=listeners[TARGET][1],
            now=BASE + 3,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(push), pool.submit(recover)]
        values = [future.result() for future in results]
    event_ids = {item["event_id"] for item in values if item is not None}
    assert len(event_ids) == 1
    assert control.inflight_count(TARGET) == 1


class WakeCounter:
    def __init__(self) -> None:
        self.count = 0

    def wake(self) -> None:
        self.count += 1


class WakeHandler(AgentRelayWebSocketHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return


def wake_endpoint_and_fallback() -> None:
    counter = WakeCounter()
    WakeHandler.admin_token = "wake-token"
    WakeHandler.coordinators = {PROTOCOL_V06: counter}  # type: ignore[assignment]
    server = ThreadingHTTPServer(("127.0.0.1", 0), WakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    url = f"http://{host}:{port}/agentrelay/internal/delivery/wake"
    body = json.dumps({"agent_id": TARGET, "reason": "message_ack"}).encode()
    try:
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Authorization": "Bearer wrong-token"},
        )
        try:
            urllib.request.urlopen(request, timeout=1)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("wake endpoint accepted an invalid token")

        connection = http.client.HTTPConnection(host, port, timeout=1)
        connection.request(
            "POST",
            "/agentrelay/internal/delivery/wake",
            body=b"{}",
            headers={
                "Authorization": "Bearer wake-token",
                "Content-Length": "invalid",
            },
        )
        assert connection.getresponse().status == 400
        connection.close()

        assert DeliveryWakeClient(url, "wake-token", timeout_seconds=1).notify(
            TARGET, "message_ack"
        )
        assert counter.count == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert not DeliveryWakeClient(
        "http://127.0.0.1:1/unavailable",
        "wake-token",
        timeout_seconds=0.05,
    ).notify(TARGET, "message_ack")


def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        default_limit_serializes_five(root / "serial")
        configured_limit_and_agent_isolation(root / "configured")
        cross_lane_claim_is_global(root / "lanes")
        lease_expiry_parks_and_releases_capacity(root / "lease")
        push_recovery_race_has_one_lease(root / "race")
    wake_endpoint_and_fallback()
    print("per-Agent delivery flow control passed (6/6)")


if __name__ == "__main__":
    main()
