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

from server.protocol_v06 import PROTOCOL_V06
from server.protocol_v05 import PROTOCOL_V05
from server.store_v05 import V05Store
from server.store_v06 import V06Store


HOST = "127.0.0.1"
PORT = 8806
REQUESTER = "zac-agent"
TARGET = "vivi-agent"


def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        v05_db = root / "v05.sqlite3"
        v06_db = root / "v06.sqlite3"
        store = V06Store(str(v06_db))
        v05_store = V05Store(str(v05_db))
        now = int(time.time())
        for agent_id in (REQUESTER, TARGET):
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
        listener = ready_listener(store, now)
        v05_requester_listener = ready_listener(v05_store, now, REQUESTER)
        v05_listener = ready_listener(v05_store, now, TARGET)
        process = start_server(root / "legacy.sqlite3", v05_db, v06_db)
        connection: socket.socket | None = None
        v05_connection: socket.socket | None = None
        try:
            wait_health()
            connection = websocket_connect(listener, PROTOCOL_V06)
            hello = read_json_frame(connection)
            assert hello["protocolVersion"] == PROTOCOL_V06
            assert hello["readinessEpoch"] == listener[1]

            created = store.create_task(
                {
                    "protocol_version": PROTOCOL_V06,
                    "idempotency_key": "v06-ws-push",
                    "requester_agent_id": REQUESTER,
                    "target_agent_id": TARGET,
                    "done_criteria": "listener receives a v0.6 push frame",
                    "task_expires_at": int(time.time()) + 3600,
                    "message": {
                        "subject": "v0.6 WebSocket push",
                        "parts": [{"kind": "text", "text": "push"}],
                    },
                }
            )
            event = read_json_frame(connection)
            assert event["protocolVersion"] == PROTOCOL_V06
            assert event["taskId"] == created["task"]["task_id"]
            assert event["outboxStatus"] == "inflight"
            assert event["deliveryAttempt"] == 1
            assert event["recoveryAttempt"] == 0
            assert event["inflightVia"] == "push"
            assert "parts" not in event
            assert event["payloadRef"]["method"] == "GET"

            v05_connection = websocket_connect(v05_listener, PROTOCOL_V05)
            v05_hello = read_json_frame(v05_connection)
            assert v05_hello["protocolVersion"] == PROTOCOL_V05
            v05_created = v05_store.create_task(
                {
                    "protocol_version": PROTOCOL_V05,
                    "idempotency_key": "v05-drain-ws-push",
                    "requester_agent_id": REQUESTER,
                    "target_agent_id": TARGET,
                    "done_criteria": "drain listener receives a v0.5 push frame",
                    "task_expires_at": int(time.time()) + 3600,
                    "message": {
                        "subject": "v0.5 drain WebSocket push",
                        "parts": [{"kind": "text", "text": "push"}],
                    },
                }
            )
            v05_event = read_json_frame(v05_connection)
            assert v05_event["protocolVersion"] == PROTOCOL_V05
            assert v05_event["taskId"] == v05_created["task"]["task_id"]
            assert "recoveryAttempt" not in v05_event
        finally:
            if connection is not None:
                connection.close()
            if v05_connection is not None:
                v05_connection.close()
            stop_server(process)
    print("protocol v0.6 WebSocket smoke passed")


def ready_listener(store: V05Store | V06Store, now: int, agent_id: str = TARGET) -> tuple[str, int]:
    instance_id = f"listener-{agent_id}-{store.__class__.__name__}-ws"
    registered = store.register_listener(
        agent_id,
        listener_instance_id=instance_id,
        client_version="0.6.0",
        workspace_version="2",
        transport="websocket",
        now=now,
    )
    epoch = registered["readiness_epoch"]
    store.publish_readiness(
        agent_id,
        listener_instance_id=instance_id,
        readiness_epoch=epoch,
        ready=True,
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
        "AGENTRELAY_TOKENS": "vivi:vivi-agent:target-token",
        "AGENTRELAY_WS_POLL_SECONDS": "0.05",
        "AGENTRELAY_WS_HEARTBEAT_SECONDS": "60",
        "AGENTRELAY_V06_COORDINATOR_POLL_SECONDS": "0.05",
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
    raise RuntimeError("v0.6 WebSocket sidecar did not start")


def websocket_connect(listener: tuple[str, int], protocol_version: str) -> socket.socket:
    sock = socket.create_connection((HOST, PORT), timeout=5)
    sock.settimeout(5)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    path = (
        f"/agentrelay/workers/{TARGET}/events/ws"
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
        "Authorization: Bearer target-token",
        f"X-AgentRelay-Agent-Id: {TARGET}",
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


def stop_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


if __name__ == "__main__":
    main()
