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

from server.store import Store

HOST = "127.0.0.1"
PORT = 8793
BASE_URL = f"http://{HOST}:{PORT}/agentrelay/api"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/agentrelay-phase2-ws.sqlite3"
        store = Store(db_path)
        task = store.create_task(
            {
                "from": "zac-agent",
                "to": "frank-agent",
                "requesterThreadId": "zac-thread-phase2-ws",
                "subject": "Phase 2 WebSocket smoke",
                "doneCriteria": "Frank receives the pending event over WebSocket.",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "Ask Frank for availability."}],
                },
            }
        )
        task_id = task["task_id"]

        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_WS_HOST": HOST,
                "AGENTRELAY_WS_PORT": str(PORT),
                "AGENTRELAY_DB_PATH": db_path,
                "AGENTRELAY_TOKENS": "zac:zac-agent:zac-token,frank:frank-agent:frank-token",
                "AGENTRELAY_WS_POLL_SECONDS": "0.1",
                "AGENTRELAY_WS_HEARTBEAT_SECONDS": "60",
            }
        )
        proc = subprocess.Popen(
            ["python3", "-m", "server.ws_app"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sock: socket.socket | None = None
        try:
            wait_for_health()
            sock = websocket_connect(
                f"/agentrelay/api/workers/frank-agent/events/ws",
                {
                    "Authorization": "Bearer frank-token",
                    "X-AgentRelay-Agent-Id": "frank-agent",
                    "X-AgentRelay-Username": "frank",
                },
            )
            hello = read_json_frame(sock)
            if hello.get("type") != "hello" or hello.get("agentId") != "frank-agent":
                raise AssertionError(f"unexpected hello frame: {hello}")
            event = read_json_frame(sock)
            if event.get("type") != "task.pending":
                raise AssertionError(f"expected task.pending frame, got: {event}")
            if event.get("taskId") != task_id:
                raise AssertionError("WebSocket event did not reference created task")
            if event.get("agentId") != "frank-agent":
                raise AssertionError("WebSocket event escaped agent scope")
            if event.get("reason") != "task.created":
                raise AssertionError("WebSocket event did not preserve pending reason")
            if not event.get("eventId"):
                raise AssertionError("WebSocket event did not include eventId")
            if event.get("deliveryState") != "inflight":
                raise AssertionError("WebSocket event should be claimed as inflight")
            if not event.get("payloadRef"):
                raise AssertionError("WebSocket event should include a payloadRef")
            if "subject" in event or "nextAction" in event:
                raise AssertionError("WebSocket push should not include task content fields")
            print(json.dumps({"ok": True, "taskId": task_id, "eventId": event["eventId"]}, indent=2))
        finally:
            if sock:
                sock.close()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def wait_for_health() -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("ok"):
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("WebSocket sidecar did not start")


def websocket_connect(path: str, headers: dict[str, str]) -> socket.socket:
    sock = socket.create_connection((HOST, PORT), timeout=5)
    sock.settimeout(5)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request_lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {HOST}:{PORT}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    request_lines.extend(f"{name}: {value}" for name, value in headers.items())
    request = "\r\n".join(request_lines) + "\r\n\r\n"
    sock.sendall(request.encode("utf-8"))
    response = read_until(sock, b"\r\n\r\n")
    if b" 101 " not in response.split(b"\r\n", 1)[0]:
        raise AssertionError(f"websocket upgrade failed: {response.decode('utf-8', 'replace')}")
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
    first = sock.recv(2)
    if len(first) != 2:
        raise RuntimeError("socket closed before frame header")
    opcode = first[0] & 0x0F
    masked = bool(first[1] & 0x80)
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else b""
    payload = bytearray(recv_exact(sock, length))
    if masked:
        for index in range(len(payload)):
            payload[index] ^= mask[index % 4]
    if opcode == 8:
        raise RuntimeError("received close frame")
    if opcode != 1:
        raise RuntimeError(f"expected text frame, got opcode {opcode}")
    return json.loads(payload.decode("utf-8"))


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
