from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable

from server.store_v05 import V05Store
from server.store_v06 import V06Store


SendEvent = Callable[[dict[str, Any]], None]
CloseSocket = Callable[[], None]


@dataclass(frozen=True)
class SocketRegistration:
    agent_id: str
    listener_instance_id: str
    readiness_epoch: int
    send: SendEvent
    close: CloseSocket | None = None


class DeliveryCoordinator:
    def __init__(
        self,
        store: V05Store | V06Store,
        *,
        poll_interval_seconds: float = 1.0,
        max_events_per_tick: int = 100,
        files_maintenance: Callable[[], None] | None = None,
        files_maintenance_interval_seconds: int = 3600,
    ):
        self.store = store
        self.protocol_version = (
            "agent-collab-v0.6" if isinstance(store, V06Store) else "agent-collab-v0.5"
        )
        self.poll_interval_seconds = poll_interval_seconds
        self.max_events_per_tick = max_events_per_tick
        self.files_maintenance = files_maintenance
        self.files_maintenance_interval_seconds = files_maintenance_interval_seconds
        self._last_files_maintenance_at: int | None = None
        self._sockets: dict[str, SocketRegistration] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def register_socket(
        self,
        agent_id: str,
        listener_instance_id: str,
        readiness_epoch: int,
        send: SendEvent,
        close: CloseSocket | None = None,
    ) -> SocketRegistration:
        self.store.assert_listener_epoch(agent_id, listener_instance_id, readiness_epoch)
        registration = SocketRegistration(
            agent_id=agent_id,
            listener_instance_id=listener_instance_id,
            readiness_epoch=readiness_epoch,
            send=send,
            close=close,
        )
        previous: SocketRegistration | None
        with self._lock:
            previous = self._sockets.get(agent_id)
            self._sockets[agent_id] = registration
        if previous and previous != registration and previous.close:
            previous.close()
        self.wake()
        return registration

    def unregister_socket(self, registration: SocketRegistration) -> None:
        with self._lock:
            if self._sockets.get(registration.agent_id) == registration:
                self._sockets.pop(registration.agent_id, None)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"agentrelay-{self.protocol_version.rsplit('v', 1)[-1]}-delivery-coordinator",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def wake(self) -> None:
        self._wake.set()

    def run_once(self, *, now: int | None = None) -> dict[str, int]:
        timestamp = int(time.time()) if now is None else int(now)
        stale_sockets_closed = self._close_stale_sockets()
        expired_leases = self.store.expire_ack_leases(now=timestamp)
        expired_tasks = self.store.expire_tasks(now=timestamp)
        should_maintain_files = (
            self.files_maintenance is not None
            and (
                self._last_files_maintenance_at is None
                or timestamp - self._last_files_maintenance_at
                >= self.files_maintenance_interval_seconds
            )
        )
        if should_maintain_files:
            self._last_files_maintenance_at = timestamp
            try:
                self.files_maintenance()
            except Exception:
                # File GC is best-effort; it must never stall event delivery.
                pass
        delivered = 0
        failed = 0
        claimed = 0
        blocked_agents: set[str] = set()

        while claimed < self.max_events_per_tick:
            due_agents = self.store.list_due_agent_ids(now=timestamp)
            if not due_agents:
                break
            made_progress = False
            for agent_id in due_agents:
                if claimed >= self.max_events_per_tick:
                    break
                if agent_id in blocked_agents:
                    continue
                event = self.store.claim_due_event(agent_id, now=timestamp)
                if not event:
                    continue
                claimed += 1
                made_progress = True
                registration = self._current_registration(agent_id)
                if not registration or not self._registration_is_current(registration):
                    self.store.record_attempt_failure(
                        event["event_id"], "listener_unavailable", now=timestamp
                    )
                    failed += 1
                    blocked_agents.add(agent_id)
                    continue
                try:
                    registration.send(format_event_message(event, self.protocol_version))
                except Exception:
                    self.store.record_attempt_failure(
                        event["event_id"], "socket_write_failed", now=timestamp
                    )
                    failed += 1
                    blocked_agents.add(agent_id)
                else:
                    delivered += 1
            if not made_progress:
                break
        return {
            "claimed": claimed,
            "sent": delivered,
            "attempt_failures": failed,
            "expired_leases": expired_leases,
            "expired_tasks": expired_tasks,
            "stale_sockets_closed": stale_sockets_closed,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            self.run_once()
            self._wake.wait(self.poll_interval_seconds)

    def _current_registration(self, agent_id: str) -> SocketRegistration | None:
        with self._lock:
            return self._sockets.get(agent_id)

    def _registration_is_current(self, registration: SocketRegistration) -> bool:
        readiness = self.store.get_readiness(registration.agent_id)
        return bool(
            readiness
            and readiness["listener_instance_id"] == registration.listener_instance_id
            and int(readiness["readiness_epoch"]) == registration.readiness_epoch
        )

    def _close_stale_sockets(self) -> int:
        with self._lock:
            registrations = list(self._sockets.values())
        closed = 0
        for registration in registrations:
            if self._registration_is_current(registration):
                continue
            self.unregister_socket(registration)
            if registration.close:
                registration.close()
            closed += 1
        return closed


def format_v05_event_message(event: dict[str, Any]) -> dict[str, Any]:
    return format_event_message(event, "agent-collab-v0.5")


def format_event_message(event: dict[str, Any], protocol_version: str) -> dict[str, Any]:
    message = {
        "type": event["event_type"],
        "protocolVersion": protocol_version,
        "eventId": event["event_id"],
        "agentId": event["agent_id"],
        "taskId": event["task_id"],
        "messageId": event["message_id"],
        "outboxStatus": event["outbox_status"],
        "deliveryAttempt": event["outbox_attempts"],
        "inflightUntil": event["inflight_until"],
        "canTransitionMessage": event["can_transition_message"],
        "payloadRef": {
            "method": "GET",
            "href": f"/agentrelay/api/tasks/{event['task_id']}",
        },
    }
    if protocol_version == "agent-collab-v0.6":
        message["recoveryAttempt"] = event.get("recovery_attempts", 0)
        message["inflightVia"] = event.get("inflight_via")
    return message
