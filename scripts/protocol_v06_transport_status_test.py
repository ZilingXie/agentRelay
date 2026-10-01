from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.delivery_coordinator import DeliveryCoordinator
from server.protocol_v06 import PROTOCOL_V06
from server.protocol_v05 import PROTOCOL_V05
from server.store import ConflictError
from server.store_v05 import V05Store
from server.store_v06 import V06Store


HOST = "127.0.0.1"
PORT = 8816
REQUESTER = "zac-agent"
TARGET = "vivi-agent"
PONGLESS = "pongless-agent"
NOT_READY = "notready-agent"
TOKENS = (
    "zac:zac-agent:zac-token,"
    "vivi:vivi-agent:vivi-token,"
    "pong:pongless-agent:pongless-token,"
    "notready:notready-agent:notready-token"
)
HEARTBEAT_SECONDS = 0.4
PONG_TIMEOUT_SECONDS = 1.2


def main() -> None:
    os.environ["AGENTRELAY_WS_PONG_TIMEOUT_SECONDS"] = str(PONG_TIMEOUT_SECONDS)
    store_level_checks(Path(tempfile.mkdtemp(prefix="agentrelay-transport-")))
    registration_order_checks(Path(tempfile.mkdtemp(prefix="agentrelay-order-")))
    stale_epoch_registration_barrier_checks(
        Path(tempfile.mkdtemp(prefix="agentrelay-stale-epoch-"))
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        ws_level_checks(Path(temp_dir))


def stale_epoch_registration_barrier_checks(root: Path) -> None:
    # Barrier regression for the epoch-fencing gap: a connection that already
    # passed the pre-lock epoch check must not overwrite a newer Listener that
    # advanced the epoch and completed its own registration meanwhile.
    previous_timeout = os.environ.get("AGENTRELAY_WS_PONG_TIMEOUT_SECONDS")
    os.environ["AGENTRELAY_WS_PONG_TIMEOUT_SECONDS"] = "90"
    try:
        store = V06Store(str(root / "stale-epoch.sqlite3"))
        for agent_id in (REQUESTER, TARGET):
            store.upsert_agent(
                agent_id,
                name=agent_id,
                owner=agent_id,
                enabled=True,
                protocol_capabilities=[PROTOCOL_V06],
                now=100,
            )
        first = store.register_listener(
            TARGET,
            listener_instance_id="listener-barrier-1",
            client_version="0.6.0",
            workspace_version="2",
            transport="websocket",
            now=100,
        )
        old_epoch = int(first["readiness_epoch"])
        store.publish_readiness(
            TARGET,
            listener_instance_id="listener-barrier-1",
            readiness_epoch=old_epoch,
            ready=True,
            now=100,
        )

        def persist(session_id: str, timestamp: int):
            def callback(selected):
                store.record_transport_connected(
                    selected.agent_id,
                    listener_instance_id=selected.listener_instance_id,
                    readiness_epoch=selected.readiness_epoch,
                    transport_session_id=session_id,
                    now=timestamp,
                )

            return callback

        coordinator = DeliveryCoordinator(store)
        sent: list[dict] = []
        new_closed: list[bool] = []
        outcome: dict[str, BaseException] = {}
        original_assert = store.assert_listener_epoch
        checked = threading.Event()
        release = threading.Event()

        def gated_assert(agent_id, listener_instance_id, readiness_epoch):
            original_assert(agent_id, listener_instance_id, readiness_epoch)
            checked.set()
            assert release.wait(5), "barrier was never released"

        def stale_connection() -> None:
            try:
                coordinator.register_socket(
                    TARGET,
                    "listener-barrier-1",
                    old_epoch,
                    sent.append,
                    transport_session_id="ts-stale-epoch",
                    on_registered=persist("ts-stale-epoch", 130),
                )
            except BaseException as exc:  # noqa: BLE001 - captured for assertion
                outcome["error"] = exc

        store.assert_listener_epoch = gated_assert
        thread = threading.Thread(target=stale_connection)
        thread.start()
        assert checked.wait(5), "stale connection never passed its epoch pre-check"
        store.assert_listener_epoch = original_assert

        # The newer Listener advances the epoch and completes registration
        # while the old connection is parked between its pre-check and the
        # registration critical section.
        second = store.register_listener(
            TARGET,
            listener_instance_id="listener-barrier-2",
            client_version="0.6.0",
            workspace_version="2",
            transport="websocket",
            now=110,
        )
        new_epoch = int(second["readiness_epoch"])
        assert new_epoch > old_epoch
        store.publish_readiness(
            TARGET,
            listener_instance_id="listener-barrier-2",
            readiness_epoch=new_epoch,
            ready=True,
            now=115,
        )
        coordinator.register_socket(
            TARGET,
            "listener-barrier-2",
            new_epoch,
            sent.append,
            close=lambda: new_closed.append(True),
            transport_session_id="ts-current-epoch",
            on_registered=persist("ts-current-epoch", 120),
        )

        release.set()
        thread.join(5)
        assert not thread.is_alive(), "stale registration never finished"

        stale_error = outcome.get("error")
        assert isinstance(stale_error, ConflictError), stale_error
        assert stale_error.code == "stale_readiness_epoch"
        assert new_closed == [], "the current connection must not be closed by a stale one"
        row = store.get_transport(TARGET)
        assert row["readiness_epoch"] == new_epoch, row
        assert row["transport_session_id"] == "ts-current-epoch", row
        assert row["state"] == "connected", row
        assert (
            store.record_transport_pong(
                TARGET,
                listener_instance_id="listener-barrier-2",
                readiness_epoch=new_epoch,
                transport_session_id="ts-current-epoch",
                now=140,
            )
            is True
        )
        agent = _admin_agent(store, TARGET, now=140)
        assert agent["status"] == "online_ready", agent["status"]
    finally:
        if previous_timeout is None:
            os.environ.pop("AGENTRELAY_WS_PONG_TIMEOUT_SECONDS", None)
        else:
            os.environ["AGENTRELAY_WS_PONG_TIMEOUT_SECONDS"] = previous_timeout
    print("stale-epoch registration barrier checks passed (v0.6)")


def registration_order_checks(root: Path) -> None:
    # Regression for the interleaved-registration defect: a slower older
    # connection must never overwrite a newer session's transport row after
    # the newer connection already registered and persisted.
    previous_timeout = os.environ.get("AGENTRELAY_WS_PONG_TIMEOUT_SECONDS")
    os.environ["AGENTRELAY_WS_PONG_TIMEOUT_SECONDS"] = "90"
    try:
        store = V06Store(str(root / "registration-order.sqlite3"))
        store.upsert_agent(
            TARGET,
            name=TARGET,
            owner=TARGET,
            enabled=True,
            protocol_capabilities=[PROTOCOL_V06],
            now=100,
        )
        registered = store.register_listener(
            TARGET,
            listener_instance_id="listener-order",
            client_version="0.6.0",
            workspace_version="2",
            transport="websocket",
            now=100,
        )
        instance = registered["listener_instance_id"]
        epoch = int(registered["readiness_epoch"])
        store.publish_readiness(
            TARGET,
            listener_instance_id=instance,
            readiness_epoch=epoch,
            ready=True,
            now=100,
        )

        def persist(session_id: str, timestamp: int):
            def callback(selected):
                store.record_transport_connected(
                    selected.agent_id,
                    listener_instance_id=selected.listener_instance_id,
                    readiness_epoch=selected.readiness_epoch,
                    transport_session_id=session_id,
                    now=timestamp,
                )

            return callback

        coordinator = DeliveryCoordinator(store)
        sent: list[dict] = []
        old_registration = coordinator.register_socket(
            TARGET,
            instance,
            epoch,
            sent.append,
            transport_session_id="ts-old",
            on_registered=persist("ts-old", 110),
        )
        assert store.get_transport(TARGET)["transport_session_id"] == "ts-old"
        coordinator.register_socket(
            TARGET,
            instance,
            epoch,
            sent.append,
            transport_session_id="ts-new",
            on_registered=persist("ts-new", 120),
        )
        # Socket selection and transport persistence are one critical
        # section: when register returns, the row already names this session.
        assert store.get_transport(TARGET)["transport_session_id"] == "ts-new"

        coordinator.unregister_socket(old_registration)
        assert (
            store.record_transport_disconnected(
                TARGET,
                listener_instance_id=instance,
                readiness_epoch=epoch,
                transport_session_id="ts-old",
                reason="superseded",
                now=130,
            )
            is False
        )
        row = store.get_transport(TARGET)
        assert row["transport_session_id"] == "ts-new" and row["state"] == "connected"
        assert (
            store.record_transport_pong(
                TARGET,
                listener_instance_id=instance,
                readiness_epoch=epoch,
                transport_session_id="ts-new",
                now=140,
            )
            is True
        )
        agent = _admin_agent(store, TARGET, now=140)
        assert agent["status"] == "online_ready", agent["status"]
    finally:
        if previous_timeout is None:
            os.environ.pop("AGENTRELAY_WS_PONG_TIMEOUT_SECONDS", None)
        else:
            os.environ["AGENTRELAY_WS_PONG_TIMEOUT_SECONDS"] = previous_timeout
    print("transport registration-order checks passed (v0.6)")


def store_level_checks(root: Path) -> None:
    from server.store_v05 import V05Store as V05
    from server.store_v06 import V06Store as V06

    # Store-level checks use fixed fake timestamps, so evaluate against the
    # default 90s pong timeout instead of the short WS-level timeout.
    previous_timeout = os.environ.get("AGENTRELAY_WS_PONG_TIMEOUT_SECONDS")
    os.environ["AGENTRELAY_WS_PONG_TIMEOUT_SECONDS"] = "90"
    try:
        for store_cls, protocol in ((V06, PROTOCOL_V06), (V05, PROTOCOL_V05)):
            _store_level_scenario(store_cls, protocol, root)
    finally:
        if previous_timeout is None:
            os.environ.pop("AGENTRELAY_WS_PONG_TIMEOUT_SECONDS", None)
        else:
            os.environ["AGENTRELAY_WS_PONG_TIMEOUT_SECONDS"] = previous_timeout
    print("transport store-level checks passed (v0.5 + v0.6)")


def _store_level_scenario(store_cls, protocol: str, root: Path) -> None:
        store = store_cls(str(root / f"{store_cls.__name__}.sqlite3"))
        store.upsert_agent(
            TARGET,
            name=TARGET,
            owner=TARGET,
            enabled=True,
            protocol_capabilities=[protocol],
            now=100,
        )
        registered = store.register_listener(
            TARGET,
            listener_instance_id="listener-1",
            client_version="0.6.0",
            workspace_version="2",
            transport="websocket",
            now=100,
        )
        epoch = int(registered["readiness_epoch"])
        connected = store.record_transport_connected(
            TARGET,
            listener_instance_id="listener-1",
            readiness_epoch=epoch,
            transport_session_id="ts-old",
            now=110,
        )
        assert connected["state"] == "connected"
        assert connected["last_pong_at"] == 110

        store.record_transport_connected(
            TARGET,
            listener_instance_id="listener-1",
            readiness_epoch=epoch,
            transport_session_id="ts-new",
            now=120,
        )
        # The old session's teardown must not clear the new session's state.
        assert (
            store.record_transport_disconnected(
                TARGET,
                listener_instance_id="listener-1",
                readiness_epoch=epoch,
                transport_session_id="ts-old",
                reason="heartbeat_timeout",
                now=130,
            )
            is False
        )
        assert (
            store.record_transport_pong(
                TARGET,
                listener_instance_id="listener-1",
                readiness_epoch=epoch,
                transport_session_id="ts-old",
                now=131,
            )
            is False
        )
        current = store.get_transport(TARGET)
        assert current["transport_session_id"] == "ts-new"
        assert current["state"] == "connected"
        assert current["last_pong_at"] == 120

        assert (
            store.record_transport_pong(
                TARGET,
                listener_instance_id="listener-1",
                readiness_epoch=epoch,
                transport_session_id="ts-new",
                now=131,
            )
            is True
        )
        store.publish_readiness(
            TARGET,
            listener_instance_id="listener-1",
            readiness_epoch=epoch,
            ready=True,
            now=140,
        )
        # Readiness publishing must not rewrite transport liveness.
        after_readiness = store.get_transport(TARGET)
        assert after_readiness["last_pong_at"] == 131
        assert after_readiness["state"] == "connected"

        agent = _admin_agent(store, TARGET, now=140)
        assert agent["status"] == "online_ready"
        assert agent["transport_online"] is True
        assert agent["listener_ready"] is True
        assert agent["readiness_fresh"] is True
        assert agent["can_receive_push"] is True
        assert agent["pending_event_count"] == 0

        # A connected row whose last Pong is older than the pong timeout is
        # offline for reporting (covers a server restart leaving a stale row).
        edge = _admin_agent(store, TARGET, now=131 + 90)
        assert edge["transport_online"] is True
        expired = _admin_agent(store, TARGET, now=131 + 91)
        assert expired["transport_online"] is False
        assert expired["status"] in {"offline_idle", "offline_with_pending"}

        status = store.admin_agent_status(TARGET, now=140)
        assert status is not None and status["status"] == "online_ready"
        assert status["transport_session_id"] == "ts-new"
        assert store.admin_agent_status("missing-agent", now=140) is None

        assert (
            store.record_transport_disconnected(
                TARGET,
                listener_instance_id="listener-1",
                readiness_epoch=epoch,
                transport_session_id="ts-new",
                reason="client_close",
                now=150,
            )
            is True
        )
        # The first disconnect result sticks.
        assert (
            store.record_transport_disconnected(
                TARGET,
                listener_instance_id="listener-1",
                readiness_epoch=epoch,
                transport_session_id="ts-new",
                reason="heartbeat_timeout",
                now=160,
            )
            is False
        )
        disconnected = store.get_transport(TARGET)
        assert disconnected["state"] == "disconnected"
        assert disconnected["disconnect_reason"] == "client_close"
        assert disconnected["disconnected_at"] == 150


def _admin_agent(store: V05Store | V06Store, agent_id: str, *, now: int) -> dict:
    agents = [agent for agent in store.admin_agents(now=now) if agent["agent_id"] == agent_id]
    assert agents, f"agent {agent_id} missing from admin listing"
    return agents[0]


def ws_level_checks(root: Path) -> None:
    v06_db = root / "v06.sqlite3"
    v05_db = root / "v05.sqlite3"
    store = V06Store(str(v06_db))
    v05_store = V05Store(str(v05_db))
    now = int(time.time())
    for agent_id in (REQUESTER, TARGET, PONGLESS, NOT_READY):
        store.upsert_agent(
            agent_id,
            name=agent_id,
            owner=agent_id,
            enabled=True,
            protocol_capabilities=[PROTOCOL_V06],
            now=now,
        )
        v05_store.upsert_agent(
            agent_id,
            name=agent_id,
            owner=agent_id,
            enabled=True,
            protocol_capabilities=[PROTOCOL_V06, PROTOCOL_V05],
            now=now,
        )
    listener = ready_listener(store, TARGET, now)
    pongless_listener = registered_listener(store, PONGLESS, now, ready=True)
    notready_listener = registered_listener(store, NOT_READY, now, ready=False)
    v05_listener = ready_listener(v05_store, TARGET, now)
    process = start_server(root / "legacy.sqlite3", v05_db, v06_db)
    connections: list[socket.socket] = []
    try:
        wait_health()

        # 1. Normal connection: hello, server Ping answered by Pong, transport row online.
        conn = websocket_connect(TARGET, listener, PROTOCOL_V06, "vivi-token")
        connections.append(conn)
        frame = read_json_auto_pong(conn)
        assert frame["type"] == "hello" and frame["protocolVersion"] == PROTOCOL_V06
        wait_until(
            lambda: (store.get_transport(TARGET) or {}).get("last_pong_at") is not None,
            "server never recorded a first transport row for the target",
        )
        first_pong_at = store.get_transport(TARGET)["last_pong_at"]
        pong_wait_started = time.time()
        while time.time() - pong_wait_started < 3:
            read_json_auto_pong(conn, idle_ok=True, timeout=1.0)
            row = store.get_transport(TARGET)
            if row and row["last_pong_at"] > first_pong_at:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("server Pong bookkeeping never advanced last_pong_at")

        # 2. Client Ping is answered with a Pong control frame.
        send_client_frame(conn, 0x9, b"probe")
        deadline = time.time() + 3
        replied = False
        while time.time() < deadline:
            opcode, payload = read_frame(conn)
            if opcode == 0xA:
                assert payload == b"probe"
                replied = True
                break
            if opcode == 0x9:
                send_client_frame(conn, 0xA, b"")
        assert replied, "server did not answer the client Ping with a Pong"

        # 3. Online but not ready shows online_not_ready.
        notready_conn = websocket_connect(NOT_READY, notready_listener, PROTOCOL_V06, "notready-token")
        connections.append(notready_conn)
        notready_hello = read_json_auto_pong(notready_conn)
        assert notready_hello["type"] == "hello"
        wait_until(
            lambda: store.get_transport(NOT_READY) is not None,
            "notready agent never registered a transport row",
        )
        notready_status = _admin_agent(store, NOT_READY, now=int(time.time()))
        assert notready_status["transport_online"] is True
        assert notready_status["listener_ready"] is False
        assert notready_status["status"] == "online_not_ready", notready_status["status"]

        # 4. A silent listener is closed after the Pong timeout and marked offline.
        pongless_conn = websocket_connect(PONGLESS, pongless_listener, PROTOCOL_V06, "pongless-token")
        pongless_hello = read_frame(pongless_conn)
        assert pongless_hello[0] == 0x1
        closed = wait_socket_closed(pongless_conn, timeout=5)
        assert closed, "server did not close the Pong-silent connection"
        wait_until(
            lambda: (store.get_transport(PONGLESS) or {}).get("state") == "disconnected",
            "pong-silent connection never became disconnected",
        )
        pongless_row = store.get_transport(PONGLESS)
        assert pongless_row["disconnect_reason"] == "heartbeat_timeout", pongless_row

        # 5. Newest transport session wins: the old socket is closed and only
        #    the new session receives events; its teardown cannot clear the row.
        replacement = websocket_connect(TARGET, listener, PROTOCOL_V06, "vivi-token")
        connections.append(replacement)
        replacement_hello = read_json_auto_pong(replacement)
        assert replacement_hello["type"] == "hello"
        assert wait_socket_closed(conn, timeout=3), "superseded socket was not closed"
        wait_until(
            lambda: (store.get_transport(TARGET) or {}).get("state") == "connected",
            "replacement transport session never recorded connected",
        )
        row = store.get_transport(TARGET)
        assert row["state"] == "connected", row
        assert row["transport_session_id"]

        created = store.create_task(
            {
                "protocol_version": PROTOCOL_V06,
                "idempotency_key": "transport-ws-push",
                "requester_agent_id": REQUESTER,
                "target_agent_id": TARGET,
                "done_criteria": "transport status test push",
                "task_expires_at": int(time.time()) + 3600,
                "message": {
                    "subject": "transport ws push",
                    "parts": [{"kind": "text", "text": "push"}],
                },
            }
        )
        event = next_event_frame(replacement)
        assert event["type"] == "message.pending", event
        assert event["taskId"] == created["task"]["task_id"]
        visibility = store.visibility(created["task"]["task_id"])
        assert visibility["outbox"]["outbox_status"] == "inflight"
        assert visibility["current_message"]["delivery_status"] == "pending"

        # The superseded connection's late teardown must leave the new
        # session online: Pong bookkeeping keeps landing and the combined
        # status reports online_ready, not a stale offline_idle row.
        pong_before = store.get_transport(TARGET)["last_pong_at"]
        pong_wait_started = time.time()
        while time.time() - pong_wait_started < 3:
            read_json_auto_pong(replacement, idle_ok=True, timeout=1.0)
            if store.get_transport(TARGET)["last_pong_at"] > pong_before:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("replacement session Pong bookkeeping stopped advancing")
        live_status = _admin_agent(store, TARGET, now=int(time.time()))
        assert live_status["transport_online"] is True, live_status
        assert live_status["status"] == "online_ready", live_status["status"]

        store.ack_message(
            TARGET,
            {
                "task_id": created["task"]["task_id"],
                "event_id": event["eventId"],
                "message_id": created["task"]["current_message_id"],
                "turn_sequence": created["task"]["turn_sequence"],
                "expected_task_version": created["task"]["task_version"],
                "idempotency_key": "transport-ws-push-ack",
                "listener_instance_id": listener[0],
                "readiness_epoch": listener[1],
            },
        )
        acked = store.visibility(created["task"]["task_id"])
        assert acked["outbox"]["outbox_status"] == "acked"
        assert acked["current_message"]["delivery_status"] == "delivered"

        # 6. Clean client Close records client_close immediately.
        send_client_frame(replacement, 0x8, struct.pack("!H", 1000))
        wait_until(
            lambda: (store.get_transport(TARGET) or {}).get("disconnect_reason") == "client_close",
            "clean client close never recorded client_close",
        )
        assert wait_socket_closed(replacement, timeout=3)

        # 7. Events created while the listener is not ready stay parked, then
        #    recover and ACK after the listener becomes ready again.
        store.publish_readiness(
            TARGET,
            listener_instance_id=listener[0],
            readiness_epoch=listener[1],
            ready=False,
        )
        offline_task = store.create_task(
            {
                "protocol_version": PROTOCOL_V06,
                "idempotency_key": "transport-offline-create",
                "requester_agent_id": REQUESTER,
                "target_agent_id": TARGET,
                "done_criteria": "offline created task survives",
                "task_expires_at": int(time.time()) + 3600,
                "message": {
                    "subject": "offline delivery",
                    "parts": [{"kind": "text", "text": "offline"}],
                },
            }
        )
        parked = store.visibility(offline_task["task"]["task_id"])
        assert parked["outbox"]["outbox_status"] == "parked"
        store.publish_readiness(
            TARGET,
            listener_instance_id=listener[0],
            readiness_epoch=listener[1],
            ready=True,
        )
        recovered = store.recover_event(
            TARGET,
            listener_instance_id=listener[0],
            readiness_epoch=listener[1],
        )
        assert recovered is not None and recovered["outbox_status"] == "inflight"
        assert recovered["inflight_via"] == "recovery"
        store.ack_message(
            TARGET,
            {
                "task_id": offline_task["task"]["task_id"],
                "event_id": recovered["event_id"],
                "message_id": offline_task["task"]["current_message_id"],
                "turn_sequence": offline_task["task"]["turn_sequence"],
                "expected_task_version": offline_task["task"]["task_version"],
                "idempotency_key": "transport-offline-ack",
                "listener_instance_id": listener[0],
                "readiness_epoch": listener[1],
            },
        )
        recovered_visibility = store.visibility(offline_task["task"]["task_id"])
        assert recovered_visibility["outbox"]["outbox_status"] == "acked"

        # 8. The v0.5 compatibility lane gets the same transport behavior.
        v05_conn = websocket_connect(TARGET, v05_listener, PROTOCOL_V05, "vivi-token")
        connections.append(v05_conn)
        v05_hello = read_json_auto_pong(v05_conn)
        assert v05_hello["type"] == "hello" and v05_hello["protocolVersion"] == PROTOCOL_V05
        wait_until(
            lambda: (v05_store.get_transport(TARGET) or {}).get("state") == "connected",
            "v0.5 lane never recorded a connected transport row",
        )
        v05_before = v05_store.get_transport(TARGET)["last_pong_at"]
        v05_wait_started = time.time()
        while time.time() - v05_wait_started < 3:
            read_json_auto_pong(v05_conn, idle_ok=True, timeout=1.0)
            if v05_store.get_transport(TARGET)["last_pong_at"] > v05_before:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("v0.5 lane Pong bookkeeping never advanced")
        assert v05_store.get_transport(TARGET)["protocol_version"] == PROTOCOL_V05
    finally:
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
        stop_server(process)
    print("transport WebSocket-level checks passed (v0.6 + v0.5 drain)")


def ready_listener(
    store: V05Store | V06Store, agent_id: str, now: int
) -> tuple[str, int]:
    registered = registered_listener(store, agent_id, now, ready=True)
    return registered


def registered_listener(
    store: V05Store | V06Store, agent_id: str, now: int, *, ready: bool
) -> tuple[str, int]:
    instance_id = f"listener-{agent_id}-{store.__class__.__name__}-transport"
    registered = store.register_listener(
        agent_id,
        listener_instance_id=instance_id,
        client_version="0.6.0",
        workspace_version="2",
        transport="websocket",
        now=now,
    )
    epoch = int(registered["readiness_epoch"])
    store.publish_readiness(
        agent_id,
        listener_instance_id=instance_id,
        readiness_epoch=epoch,
        ready=ready,
        now=now,
    )
    return instance_id, epoch


def start_server(legacy_db: Path, v05_db: Path, v06_db: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "AGENTRELAY_WS_HOST": HOST,
        "AGENTRELAY_WS_PORT": str(PORT),
        "AGENTRELAY_DB_PATH": str(legacy_db),
        "AGENTRELAY_V05_DB_PATH": str(v05_db),
        "AGENTRELAY_V06_DB_PATH": str(v06_db),
        "AGENTRELAY_MUTATION_MODE": "v06",
        "AGENTRELAY_V05_DRAIN_ENABLED": "1",
        "AGENTRELAY_TOKENS": TOKENS,
        "AGENTRELAY_WS_POLL_SECONDS": "0.05",
        "AGENTRELAY_WS_HEARTBEAT_SECONDS": str(HEARTBEAT_SECONDS),
        "AGENTRELAY_WS_PONG_TIMEOUT_SECONDS": str(PONG_TIMEOUT_SECONDS),
        "AGENTRELAY_V06_COORDINATOR_POLL_SECONDS": "0.05",
        "AGENTRELAY_V05_COORDINATOR_POLL_SECONDS": "0.05",
    }
    return subprocess.Popen(
        ["python3", "-m", "server.ws_app"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_health() -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://{HOST}:{PORT}/agentrelay/health", timeout=1
            ) as response:
                payload = json.loads(response.read())
            if payload.get("ok") and payload.get("mutation_mode") == "v06":
                return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("transport status WebSocket sidecar did not start")


def websocket_connect(
    agent_id: str, listener: tuple[str, int], protocol_version: str, token: str
) -> socket.socket:
    sock = socket.create_connection((HOST, PORT), timeout=5)
    sock.settimeout(5)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    path = (
        f"/agentrelay/workers/{agent_id}/events/ws"
        f"?listener_instance_id={listener[0]}&readiness_epoch={listener[1]}"
        f"&protocol_version={protocol_version}"
    )
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {HOST}:{PORT}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        f"Authorization: Bearer {token}",
        f"X-AgentRelay-Agent-Id: {agent_id}",
        "",
        "",
    ]
    sock.sendall("\r\n".join(lines).encode("utf-8"))
    response = read_until(sock, b"\r\n\r\n")
    status = int(response.split(b"\r\n", 1)[0].split()[1])
    if status != 101:
        sock.close()
        raise AssertionError(f"WebSocket upgrade failed with {status}")
    return sock


def read_until(sock: socket.socket, marker: bytes) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(1)
        if not chunk:
            raise RuntimeError("socket closed while reading")
        data.extend(chunk)
    return bytes(data)


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    first = recv_exact(sock, 2)
    opcode = first[0] & 0x0F
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    payload = recv_exact(sock, length) if length else b""
    return opcode, payload


def read_json_auto_pong(
    sock: socket.socket, *, idle_ok: bool = False, timeout: float = 5.0
) -> dict:
    sock.settimeout(timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            opcode, payload = read_frame(sock)
        except socket.timeout:
            if idle_ok:
                return {"type": "idle"}
            raise
        if opcode == 0x9:
            send_client_frame(sock, 0xA, payload)
            continue
        if opcode == 0xA:
            continue
        if opcode == 0x8:
            raise RuntimeError("server closed the connection")
        assert opcode == 0x1, f"expected text frame, got opcode {opcode}"
        return json.loads(payload)
    raise RuntimeError("timed out waiting for a text frame")


def next_event_frame(sock: socket.socket, *, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        frame = read_json_auto_pong(sock, timeout=max(0.2, deadline - time.time()))
        if frame.get("type") not in {"hello", "heartbeat", "idle"}:
            return frame
    raise RuntimeError("timed out waiting for an event frame")


def send_client_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes([0x80 | opcode, 0x80 | length])
    elif length <= 0xFFFF:
        header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", length)
    else:
        header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", length)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(header + mask + masked)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("socket closed while reading frame")
        data.extend(chunk)
    return bytes(data)


def wait_socket_closed(sock: socket.socket, *, timeout: float) -> bool:
    sock.settimeout(timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = sock.recv(1)
        except socket.timeout:
            return False
        except OSError:
            return True
        if not chunk:
            return True
    return False


def wait_until(predicate, message: str, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def stop_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


if __name__ == "__main__":
    main()
