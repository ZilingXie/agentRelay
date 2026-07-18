from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.protocol_v05 import PROTOCOL_V05
from server.store_v05 import V05Store


HOST = "127.0.0.1"
PORT = 8800
AGENT = "frank-agent"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        legacy_db = root / "legacy.sqlite3"
        v05_db = root / "v05.sqlite3"
        store = V05Store(str(v05_db))
        now = int(time.time())
        for agent_id in ("zac-agent", AGENT):
            store.upsert_agent(
                agent_id,
                name=agent_id,
                owner=agent_id,
                enabled=True,
                protocol_capabilities=[PROTOCOL_V05],
                now=now,
            )
        old_listener = ready_listener(store, "frank-listener-1", now)
        ready_listener(store, "zac-listener-1", now, agent_id="zac-agent")

        process = start_server(legacy_db, v05_db)
        old_socket: socket.socket | None = None
        new_socket: socket.socket | None = None
        try:
            wait_health()
            old_socket = websocket_connect(old_listener)
            hello = read_json_frame(old_socket)
            assert hello["protocolVersion"] == PROTOCOL_V05
            assert hello["readinessEpoch"] == old_listener[1]

            task = create_task(store, "ws-first")
            event = read_json_frame(old_socket)
            assert event["type"] == "message.pending"
            assert event["taskId"] == task["task"]["task_id"]
            assert event["messageId"] == task["task"]["current_message_id"]
            assert event["outboxStatus"] == "inflight"
            assert event["deliveryAttempt"] == 1
            assert "parts" not in event and event["payloadRef"]["method"] == "GET"
            store.ack_message(
                AGENT,
                {
                    "task_id": task["task"]["task_id"],
                    "event_id": event["eventId"],
                    "message_id": task["task"]["current_message_id"],
                    "turn_sequence": 1,
                    "expected_task_version": 1,
                    "idempotency_key": "ws-first-ack",
                    "listener_instance_id": old_listener[0],
                    "readiness_epoch": old_listener[1],
                },
            )

            new_listener = ready_listener(store, "frank-listener-2", int(time.time()))
            wait_for_socket_close(old_socket)
            old_socket = None
            stale_status = websocket_handshake_status(old_listener)
            assert stale_status == 409

            new_socket = websocket_connect(new_listener)
            new_hello = read_json_frame(new_socket)
            assert new_hello["readinessEpoch"] == new_listener[1]
            second = create_task(store, "ws-second")
            second_event = read_json_frame(new_socket)
            assert second_event["taskId"] == second["task"]["task_id"]
            assert second_event["deliveryAttempt"] == 1
        finally:
            if old_socket:
                old_socket.close()
            if new_socket:
                new_socket.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    print("protocol v0.5 WebSocket smoke passed")


def ready_listener(
    store: V05Store,
    instance_id: str,
    now: int,
    *,
    agent_id: str = AGENT,
) -> tuple[str, int]:
    registered = store.register_listener(
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
        readiness_epoch=registered["readiness_epoch"],
        ready=True,
        now=now,
    )
    return instance_id, registered["readiness_epoch"]


def create_task(store: V05Store, key: str) -> dict:
    now = int(time.time())
    return store.create_task(
        {
            "protocol_version": PROTOCOL_V05,
            "idempotency_key": key,
            "requester_agent_id": "zac-agent",
            "target_agent_id": AGENT,
            "done_criteria": "delivery",
            "task_expires_at": now + 3600,
            "message": {"parts": [{"kind": "text", "text": key}]},
        },
        now=now,
    )


def start_server(legacy_db: Path, v05_db: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "AGENTRELAY_WS_HOST": HOST,
        "AGENTRELAY_WS_PORT": str(PORT),
        "AGENTRELAY_DB_PATH": str(legacy_db),
        "AGENTRELAY_V05_DB_PATH": str(v05_db),
        "AGENTRELAY_MUTATION_MODE": "v05",
        "AGENTRELAY_TOKENS": "frank:frank-agent:frank-token",
        "AGENTRELAY_WS_POLL_SECONDS": "0.05",
        "AGENTRELAY_WS_HEARTBEAT_SECONDS": "60",
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
                f"http://{HOST}:{PORT}/agentrelay/api/health", timeout=1
            ) as response:
                payload = json.loads(response.read())
            if payload.get("ok") and payload.get("mutation_mode") == "v05":
                return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("v0.5 WebSocket sidecar did not start")


def websocket_path(listener: tuple[str, int]) -> str:
    return (
        f"/agentrelay/api/workers/{AGENT}/events/ws"
        f"?listener_instance_id={listener[0]}&readiness_epoch={listener[1]}"
    )


def websocket_connect(listener: tuple[str, int]) -> socket.socket:
    sock, status = websocket_handshake(listener)
    if status != 101:
        sock.close()
        raise AssertionError(f"websocket upgrade failed with {status}")
    return sock


def websocket_handshake_status(listener: tuple[str, int]) -> int:
    sock, status = websocket_handshake(listener)
    sock.close()
    return status


def websocket_handshake(listener: tuple[str, int]) -> tuple[socket.socket, int]:
    sock = socket.create_connection((HOST, PORT), timeout=5)
    sock.settimeout(5)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [
        f"GET {websocket_path(listener)} HTTP/1.1",
        f"Host: {HOST}:{PORT}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        "Authorization: Bearer frank-token",
        f"X-AgentRelay-Agent-Id: {AGENT}",
        "",
        "",
    ]
    sock.sendall("\r\n".join(lines).encode("utf-8"))
    response = read_until(sock, b"\r\n\r\n")
    status = int(response.split(b"\r\n", 1)[0].split()[1])
    return sock, status


def wait_for_socket_close(sock: socket.socket) -> None:
    deadline = time.time() + 3
    sock.settimeout(0.2)
    while time.time() < deadline:
        try:
            data = sock.recv(1)
            if not data:
                return
        except socket.timeout:
            continue
        except OSError:
            return
    raise AssertionError("stale WebSocket was not closed after epoch replacement")


def read_until(sock: socket.socket, marker: bytes) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(1)
        if not chunk:
            raise RuntimeError("socket closed while reading")
        data.extend(chunk)
    return bytes(data)


def read_json_frame(sock: socket.socket) -> dict:
    first = recv_exact(sock, 2)
    opcode = first[0] & 0x0F
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    if opcode != 1:
        raise RuntimeError(f"expected text frame, got opcode {opcode}")
    return json.loads(recv_exact(sock, length))


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("socket closed while reading frame")
        data.extend(chunk)
    return bytes(data)


if __name__ == "__main__":
    main()
