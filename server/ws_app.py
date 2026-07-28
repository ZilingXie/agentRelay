from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from server.app import (
    clean_path,
    first_query_value,
    load_auth_identities,
    parse_required_positive_int_query,
    query_params,
    validate_protocol_drain_stores,
)
from server.delivery_coordinator import DeliveryCoordinator
from server.store import ConflictError, Store
from server.store_v05 import V05Store
from server.store_v06 import V06Store
from server.protocol_v05 import PROTOCOL_V05
from server.protocol_v06 import PROTOCOL_V06


DEFAULT_DB_PATH = "./data/agentrelay.sqlite3"
DEFAULT_V05_DB_PATH = "./data/agentrelay-v05.sqlite3"
DEFAULT_V06_DB_PATH = "./data/agentrelay-v06.sqlite3"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class AgentRelayWebSocketHandler(BaseHTTPRequestHandler):
    store: Store
    v05_store: V05Store | None = None
    v06_store: V06Store | None = None
    coordinator: DeliveryCoordinator | None = None
    coordinators: dict[str, DeliveryCoordinator] = {}
    mutation_mode: str = "legacy"
    v05_drain_enabled: bool = False
    auth_identities: dict[str, dict[str, str]] = {}
    auth_required: bool = False
    poll_interval_seconds: float = 2.0
    heartbeat_seconds: float = 30.0
    lease_seconds: int = 60

    def do_GET(self) -> None:
        path = clean_path(self.path)
        if path in {"/health", "/agentrelay/health"}:
            self.respond_json(
                {"ok": True, "service": "agentrelay-ws", "mutation_mode": self.mutation_mode}
            )
            return
        match = re.fullmatch(r"/agentrelay/workers/([^/]+)/events/ws", path)
        if not match:
            self.respond_error(404, "not found")
            return
        agent_id = match.group(1)
        auth = self.require_auth()
        if auth is None:
            return
        if not self.require_agent(auth, agent_id):
            return
        if not self.is_websocket_upgrade():
            self.respond_error(400, "expected websocket upgrade")
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self.respond_error(400, "missing Sec-WebSocket-Key")
            return
        self._send_lock = threading.Lock()
        self._current_closed = threading.Event()
        if self.mutation_mode in {"closed", "v05", "v06"}:
            query = query_params(self.path)
            protocol_version = first_query_value(query, "protocol_version") or (
                PROTOCOL_V06 if self.mutation_mode == "v06" else PROTOCOL_V05
            )
            current_store = self.v06_store if protocol_version == PROTOCOL_V06 else self.v05_store
            coordinator = self.coordinators.get(protocol_version)
            lane_allowed = protocol_version == PROTOCOL_V06 or (
                protocol_version == PROTOCOL_V05
                and (self.mutation_mode in {"closed", "v05"} or self.v05_drain_enabled)
            )
            if not lane_allowed or current_store is None or coordinator is None:
                self.respond_error(503, f"Protocol {protocol_version} delivery is not configured")
                return
            instance_id = first_query_value(query, "listener_instance_id")
            if not instance_id:
                self.respond_error(400, "missing listener_instance_id")
                return
            try:
                epoch = parse_required_positive_int_query(query, "readiness_epoch")
                current_store.assert_listener_epoch(agent_id, instance_id, epoch)
            except (ValueError, ConflictError) as exc:
                self.respond_error(
                    409 if isinstance(exc, ConflictError) else 400,
                    str(exc),
                    code=exc.code if isinstance(exc, ConflictError) else "VALIDATION_ERROR",
                )
                return
            self.accept_websocket(key)
            self.stream_current_events(
                agent_id, instance_id, epoch, protocol_version, coordinator
            )
            return
        self.accept_websocket(key)
        self.stream_events(agent_id)

    def stream_current_events(
        self,
        agent_id: str,
        listener_instance_id: str,
        readiness_epoch: int,
        protocol_version: str,
        coordinator: DeliveryCoordinator,
    ) -> None:
        registration = coordinator.register_socket(
            agent_id,
            listener_instance_id,
            readiness_epoch,
            self.send_current_json_frame,
            close=self._current_closed.set,
        )
        next_heartbeat_at = time.time() + self.heartbeat_seconds
        try:
            self.send_current_json_frame(
                {
                    "type": "hello",
                    "protocolVersion": protocol_version,
                    "agentId": agent_id,
                    "listenerInstanceId": listener_instance_id,
                    "readinessEpoch": readiness_epoch,
                    "serverTime": int(time.time()),
                }
            )
            while not self._current_closed.wait(self.poll_interval_seconds):
                now = time.time()
                if now >= next_heartbeat_at:
                    self.send_current_json_frame({"type": "heartbeat", "serverTime": int(now)})
                    next_heartbeat_at = now + self.heartbeat_seconds
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            return
        except OSError:
            return
        finally:
            coordinator.unregister_socket(registration)
            self.close_connection = True

    def send_current_json_frame(self, payload: dict[str, Any]) -> None:
        try:
            self.send_json_frame(payload)
        except Exception:
            self._current_closed.set()
            raise

    def stream_events(self, agent_id: str) -> None:
        next_heartbeat_at = time.time() + self.heartbeat_seconds
        try:
            self.send_json_frame(
                {
                    "type": "hello",
                    "agentId": agent_id,
                    "serverTime": int(time.time()),
                }
            )
            while True:
                events = self.store.claim_agent_events(
                    agent_id,
                    limit=100,
                    lease_seconds=self.lease_seconds,
                )
                for event in events:
                    self.send_json_frame(format_event_message(event))
                now = time.time()
                if now >= next_heartbeat_at:
                    self.send_json_frame({"type": "heartbeat", "serverTime": int(now)})
                    next_heartbeat_at = now + self.heartbeat_seconds
                time.sleep(self.poll_interval_seconds)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            return
        except OSError:
            return
        finally:
            self.close_connection = True

    def is_websocket_upgrade(self) -> bool:
        upgrade = self.headers.get("Upgrade", "")
        connection = self.headers.get("Connection", "")
        return upgrade.lower() == "websocket" and "upgrade" in connection.lower()

    def accept_websocket(self, key: str) -> None:
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

    def send_json_frame(self, payload: dict[str, Any]) -> None:
        self.send_text_frame(json.dumps(payload, separators=(",", ":")))

    def send_text_frame(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length <= 0xFFFF:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        lock = getattr(self, "_send_lock", None)
        if lock is None:
            self.wfile.write(bytes(header) + payload)
            self.wfile.flush()
            return
        with lock:
            self.wfile.write(bytes(header) + payload)
            self.wfile.flush()

    def require_auth(self) -> dict[str, str] | None:
        if not self.auth_required:
            return {"username": "", "agent_id": ""}
        authorization = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            self.respond_error(401, "missing bearer token")
            return None
        token = authorization[len(prefix):]
        identity = self.auth_identities.get(token)
        if not identity:
            self.respond_error(401, "invalid bearer token")
            return None
        username = self.headers.get("X-AgentRelay-Username", "")
        agent_id = self.headers.get("X-AgentRelay-Agent-Id", "")
        if username and not hmac.compare_digest(username, identity["username"]):
            self.respond_error(403, "username does not match token")
            return None
        if agent_id and not hmac.compare_digest(agent_id, identity["agent_id"]):
            self.respond_error(403, "agent id does not match token")
            return None
        return identity

    def require_agent(self, auth: dict[str, str], requested_agent_id: Any) -> bool:
        if not self.auth_required:
            return True
        if not isinstance(requested_agent_id, str) or not requested_agent_id:
            self.respond_error(400, "missing agent id for authenticated action")
            return False
        if not hmac.compare_digest(auth["agent_id"], requested_agent_id):
            self.respond_error(403, "token cannot subscribe as requested agent")
            return False
        return True

    def respond_json(self, payload: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def respond_error(self, status: int, message: str, *, code: str | None = None) -> None:
        payload = {"error": message}
        if code is not None:
            payload["code"] = code
        self.respond_json(payload, status=status)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def format_event_message(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event.get("payload") or {})
    payload_ref = payload.get("payloadRef") or payload.get("payload_ref") or {
        "method": "GET",
        "href": f"/agentrelay/tasks/{event['task_id']}",
    }
    message = {
        "type": payload.pop("type", event["event_type"]),
        "eventId": event["event_id"],
        "eventType": event["event_type"],
        "agentId": event["agent_id"],
        "taskId": event["task_id"],
        "createdAt": event["created_at"],
        "cursor": event.get("cursor"),
        "deliveryState": event.get("delivery_state"),
        "deliveryAttempts": event.get("delivery_attempts"),
        "inflightUntil": event.get("inflight_until"),
        "payloadRef": payload_ref,
    }
    for key in ("contextId", "status", "pendingOnAgentId", "updatedAt", "reason"):
        if key in payload:
            message[key] = payload[key]
    for source_key, target_key in {
        "message_id": "messageId",
        "turn_sequence": "turnSequence",
        "status_version": "statusVersion",
        "from_agent_id": "fromAgentId",
        "to_agent_id": "toAgentId",
    }.items():
        if source_key in payload:
            message[target_key] = payload[source_key]
    return message


def create_server() -> ThreadingHTTPServer:
    host = os.environ.get("AGENTRELAY_WS_HOST", os.environ.get("AGENTRELAY_HOST", "127.0.0.1"))
    port = int(os.environ.get("AGENTRELAY_WS_PORT", "8788"))
    db_path = os.environ.get("AGENTRELAY_DB_PATH", DEFAULT_DB_PATH)
    store = Store(db_path)
    mutation_mode = os.environ.get("AGENTRELAY_MUTATION_MODE", "legacy").strip().lower()
    if mutation_mode not in {"legacy", "closed", "v05", "v06"}:
        raise ValueError("AGENTRELAY_MUTATION_MODE must be legacy, closed, v05, or v06")
    v05_drain_enabled = os.environ.get("AGENTRELAY_V05_DRAIN_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if v05_drain_enabled and mutation_mode != "v06":
        raise ValueError("AGENTRELAY_V05_DRAIN_ENABLED requires AGENTRELAY_MUTATION_MODE=v06")
    v05_db_path = os.environ.get("AGENTRELAY_V05_DB_PATH", "").strip()
    if (mutation_mode in {"closed", "v05"} or v05_drain_enabled) and not v05_db_path:
        v05_db_path = DEFAULT_V05_DB_PATH
    v05_store = V05Store(v05_db_path) if v05_db_path else None
    v06_db_path = os.environ.get("AGENTRELAY_V06_DB_PATH", "").strip()
    if mutation_mode == "v06" and not v06_db_path:
        v06_db_path = DEFAULT_V06_DB_PATH
    v06_store = V06Store(v06_db_path) if v06_db_path else None
    if v05_drain_enabled:
        validate_protocol_drain_stores(v05_store, v06_store)
    coordinator_stores = {}
    if mutation_mode == "v06" and v06_store is not None:
        coordinator_stores[PROTOCOL_V06] = v06_store
    elif mutation_mode in {"closed", "v05"} and v05_store is not None:
        coordinator_stores[PROTOCOL_V05] = v05_store
    if v05_drain_enabled and v05_store is not None:
        coordinator_stores[PROTOCOL_V05] = v05_store
    coordinators = {}
    for protocol_version, coordinator_store in coordinator_stores.items():
        poll_env = (
            "AGENTRELAY_V06_COORDINATOR_POLL_SECONDS"
            if protocol_version == PROTOCOL_V06
            else "AGENTRELAY_V05_COORDINATOR_POLL_SECONDS"
        )
        coordinator = DeliveryCoordinator(
            coordinator_store,
            poll_interval_seconds=float(os.environ.get(poll_env, "1")),
        )
        coordinator.start()
        coordinators[protocol_version] = coordinator
    current_protocol = PROTOCOL_V06 if mutation_mode == "v06" else PROTOCOL_V05
    coordinator = coordinators.get(current_protocol)
    AgentRelayWebSocketHandler.store = store
    AgentRelayWebSocketHandler.v05_store = v05_store
    AgentRelayWebSocketHandler.v06_store = v06_store
    AgentRelayWebSocketHandler.coordinator = coordinator
    AgentRelayWebSocketHandler.coordinators = coordinators
    AgentRelayWebSocketHandler.mutation_mode = mutation_mode
    AgentRelayWebSocketHandler.v05_drain_enabled = v05_drain_enabled
    auth_required, identities = load_auth_identities()
    AgentRelayWebSocketHandler.auth_required = auth_required
    AgentRelayWebSocketHandler.auth_identities = identities
    AgentRelayWebSocketHandler.poll_interval_seconds = float(os.environ.get("AGENTRELAY_WS_POLL_SECONDS", "2"))
    AgentRelayWebSocketHandler.heartbeat_seconds = float(os.environ.get("AGENTRELAY_WS_HEARTBEAT_SECONDS", "30"))
    AgentRelayWebSocketHandler.lease_seconds = int(os.environ.get("AGENTRELAY_WS_LEASE_SECONDS", "60"))
    server = ThreadingHTTPServer((host, port), AgentRelayWebSocketHandler)
    server.delivery_coordinator = coordinator  # type: ignore[attr-defined]
    server.v05_coordinator = coordinators.get(PROTOCOL_V05)  # type: ignore[attr-defined]
    server.v06_coordinator = coordinators.get(PROTOCOL_V06)  # type: ignore[attr-defined]
    return server


def main() -> None:
    server = create_server()
    host, port = server.server_address
    print(f"AgentRelay WebSocket listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
