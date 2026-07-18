from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any
import uuid

from server.protocol_v05 import (
    DELIVERY_ACK_LEASE_SECONDS,
    DELIVERY_FAILURE_REASONS,
    LISTENER_READINESS_MAX_AGE_SECONDS,
    MAX_DELIVERY_ATTEMPTS,
    OUTBOX_LAST_ERRORS,
    PROTOCOL_V05,
    RETRY_BACKOFF_SECONDS,
    TASK_FAILURE_REASONS,
)
from server.store import ConflictError


DEFAULT_TASK_TTL_SECONDS = 24 * 60 * 60


class V05Store:
    """Native Protocol v0.5 storage for the clean writable database."""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    protocol_capabilities_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_listener_readiness (
                    agent_id TEXT PRIMARY KEY,
                    protocol_version TEXT NOT NULL,
                    client_version TEXT NOT NULL,
                    workspace_version TEXT NOT NULL,
                    listener_instance_id TEXT NOT NULL,
                    readiness_epoch INTEGER NOT NULL CHECK (readiness_epoch >= 1),
                    transport TEXT NOT NULL,
                    ready INTEGER NOT NULL CHECK (ready IN (0, 1)),
                    observed_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    root_task_id TEXT NOT NULL,
                    protocol_version TEXT NOT NULL CHECK (protocol_version = 'agent-collab-v0.5'),
                    requester_agent_id TEXT NOT NULL,
                    target_agent_id TEXT NOT NULL,
                    done_criteria TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('open', 'completed', 'expired', 'failed')),
                    turn_sequence INTEGER NOT NULL CHECK (turn_sequence >= 1),
                    current_message_id TEXT NOT NULL,
                    from_agent_id TEXT NOT NULL,
                    to_agent_id TEXT NOT NULL,
                    task_version INTEGER NOT NULL CHECK (task_version >= 1),
                    max_turns INTEGER NOT NULL CHECK (max_turns >= 1),
                    task_expires_at INTEGER NOT NULL,
                    reason TEXT,
                    terminal_by_agent_id TEXT,
                    completed_against_message_id TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (root_task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
                    FOREIGN KEY (requester_agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT,
                    FOREIGN KEY (target_agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    turn_sequence INTEGER NOT NULL CHECK (turn_sequence >= 1),
                    from_agent_id TEXT NOT NULL,
                    to_agent_id TEXT NOT NULL,
                    parts_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    delivery_status TEXT NOT NULL CHECK (delivery_status IN ('pending', 'delivered', 'failed')),
                    max_delivery_attempts INTEGER NOT NULL CHECK (max_delivery_attempts = 4),
                    delivered_at INTEGER,
                    failed_at INTEGER,
                    delivery_reason TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
                    FOREIGN KEY (from_agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT,
                    FOREIGN KEY (to_agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT,
                    UNIQUE (task_id, from_agent_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS agent_events (
                    event_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    message_id TEXT,
                    payload_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    outbox_status TEXT NOT NULL CHECK (outbox_status IN ('queued', 'inflight', 'acked', 'retry_wait', 'exhausted')),
                    outbox_attempts INTEGER NOT NULL CHECK (outbox_attempts BETWEEN 0 AND 4),
                    inflight_until INTEGER,
                    next_retry_at INTEGER,
                    acked_at INTEGER,
                    exhausted_at INTEGER,
                    exhaustion_reason TEXT,
                    last_error TEXT,
                    can_transition_message INTEGER NOT NULL CHECK (can_transition_message IN (0, 1)),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
                    FOREIGN KEY (message_id) REFERENCES messages(message_id) ON DELETE RESTRICT,
                    UNIQUE (agent_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS task_audit_events (
                    audit_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_agent_id TEXT,
                    message_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
                    FOREIGN KEY (message_id) REFERENCES messages(message_id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS idempotency_records (
                    operation TEXT NOT NULL,
                    actor_agent_id TEXT NOT NULL,
                    task_scope TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    result_task_id TEXT NOT NULL,
                    result_message_id TEXT,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (operation, actor_agent_id, task_scope, idempotency_key),
                    FOREIGN KEY (result_task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
                    FOREIGN KEY (result_message_id) REFERENCES messages(message_id) ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_messages_task_turn
                    ON messages (task_id, turn_sequence, created_at, message_id);
                CREATE INDEX IF NOT EXISTS idx_agent_events_due
                    ON agent_events (outbox_status, next_retry_at, created_at, event_id);
                CREATE INDEX IF NOT EXISTS idx_agent_events_recovery
                    ON agent_events (agent_id, outbox_status, inflight_until, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_expiry
                    ON tasks (status, task_expires_at, task_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_lineage
                    ON tasks (root_task_id, created_at, task_id);

                CREATE TRIGGER IF NOT EXISTS prevent_task_hard_delete
                BEFORE DELETE ON tasks
                BEGIN
                    SELECT RAISE(ABORT, 'AgentRelay protocol forbids hard deletion of tasks');
                END;
                """
            )

    def upsert_agent(
        self,
        agent_id: str,
        *,
        name: str,
        owner: str,
        enabled: bool,
        protocol_capabilities: list[str],
        now: int | None = None,
    ) -> dict[str, Any]:
        timestamp = _now(now)
        capabilities = json.dumps(sorted(set(protocol_capabilities)))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO agents (
                    agent_id, name, owner, enabled, protocol_capabilities_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    name = excluded.name,
                    owner = excluded.owner,
                    enabled = excluded.enabled,
                    protocol_capabilities_json = excluded.protocol_capabilities_json,
                    updated_at = excluded.updated_at
                """,
                (agent_id, name, owner, int(enabled), capabilities, timestamp, timestamp),
            )
            return self._agent_conn(conn, agent_id)

    def register_listener(
        self,
        agent_id: str,
        *,
        listener_instance_id: str,
        client_version: str,
        workspace_version: str,
        transport: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        timestamp = _now(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_agent_conn(conn, agent_id)
            row = conn.execute(
                "SELECT readiness_epoch FROM agent_listener_readiness WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            epoch = int(row["readiness_epoch"]) + 1 if row else 1
            conn.execute(
                """
                INSERT INTO agent_listener_readiness (
                    agent_id, protocol_version, client_version, workspace_version,
                    listener_instance_id, readiness_epoch, transport, ready,
                    observed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    protocol_version = excluded.protocol_version,
                    client_version = excluded.client_version,
                    workspace_version = excluded.workspace_version,
                    listener_instance_id = excluded.listener_instance_id,
                    readiness_epoch = excluded.readiness_epoch,
                    transport = excluded.transport,
                    ready = 0,
                    observed_at = excluded.observed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    agent_id, PROTOCOL_V05, client_version, workspace_version,
                    listener_instance_id, epoch, transport, timestamp, timestamp,
                ),
            )
            return self._readiness_conn(conn, agent_id)

    def publish_readiness(
        self,
        agent_id: str,
        *,
        listener_instance_id: str,
        readiness_epoch: int,
        ready: bool,
        now: int | None = None,
    ) -> dict[str, Any]:
        timestamp = _now(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE agent_listener_readiness
                SET ready = ?, observed_at = ?, updated_at = ?
                WHERE agent_id = ? AND listener_instance_id = ? AND readiness_epoch = ?
                """,
                (int(ready), timestamp, timestamp, agent_id, listener_instance_id, readiness_epoch),
            )
            if cursor.rowcount != 1:
                raise ConflictError("stale_readiness_epoch", code="stale_readiness_epoch")
            return self._readiness_conn(conn, agent_id)

    def create_task(
        self,
        payload: dict[str, Any],
        *,
        source_task_id: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        timestamp = _now(now)
        requester = str(payload["requester_agent_id"])
        target = str(payload["target_agent_id"])
        key = str(payload["idempotency_key"])
        scope = source_task_id or "__root__"
        request_hash = _fingerprint(payload)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._idempotent_result_conn(
                conn, "create", requester, scope, key, request_hash
            )
            if existing:
                return self._task_detail_conn(conn, existing)
            self._assert_admission_conn(conn, requester, timestamp)
            self._assert_admission_conn(conn, target, timestamp)

            root_task_id: str | None = None
            if source_task_id:
                source = self._task_row_conn(conn, source_task_id)
                if not source:
                    raise ValueError("source task not found")
                if source["status"] not in {"completed", "expired", "failed"}:
                    raise ConflictError("follow-up source must be terminal")
                if (
                    source["requester_agent_id"] != requester
                    or source["target_agent_id"] != target
                ):
                    raise ConflictError("follow-up participants must match the source task")
                root_task_id = str(source["root_task_id"])

            expires_at = int(payload.get("task_expires_at") or timestamp + DEFAULT_TASK_TTL_SECONDS)
            if expires_at <= timestamp:
                raise ValueError("task_expires_at must be in the future")
            max_turns = int(payload.get("max_turns") or 12)
            task_id = f"task_{uuid.uuid4().hex}"
            message_payload = payload["message"]
            message_id = str(message_payload.get("message_id") or f"msg_{uuid.uuid4().hex}")
            event_id = f"evt_{uuid.uuid4().hex}"
            root_task_id = root_task_id or task_id
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, root_task_id, protocol_version, requester_agent_id,
                    target_agent_id, done_criteria, status, turn_sequence,
                    current_message_id, from_agent_id, to_agent_id, task_version,
                    max_turns, task_expires_at, reason, terminal_by_agent_id,
                    completed_against_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', 1, ?, ?, ?, 1, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    task_id, root_task_id, PROTOCOL_V05, requester, target,
                    json.dumps(payload["done_criteria"], sort_keys=True), message_id,
                    requester, target, max_turns, expires_at, timestamp, timestamp,
                ),
            )
            self._insert_message_conn(
                conn,
                message_id=message_id,
                task_id=task_id,
                turn_sequence=1,
                from_agent_id=requester,
                to_agent_id=target,
                parts=message_payload["parts"],
                idempotency_key=key,
                now=timestamp,
            )
            self._insert_pending_event_conn(
                conn,
                event_id=event_id,
                task_id=task_id,
                message_id=message_id,
                target_agent_id=target,
                turn_sequence=1,
                task_version=1,
                now=timestamp,
            )
            self._audit_conn(
                conn, task_id, "task.created", requester, message_id,
                {"status": "open", "task_version": 1}, timestamp,
            )
            if source_task_id:
                self._audit_conn(
                    conn, source_task_id, "task.followup_created", requester, None,
                    {"source_task_id": source_task_id, "new_task_id": task_id, "root_task_id": root_task_id},
                    timestamp,
                )
            self._record_idempotency_conn(
                conn, "create", requester, scope, key, request_hash,
                task_id, message_id, timestamp,
            )
            return self._task_detail_conn(conn, task_id)

    def submit_message(
        self,
        task_id: str,
        payload: dict[str, Any],
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        timestamp = _now(now)
        actor = str(payload["actor_agent_id"])
        key = str(payload["idempotency_key"])
        request_hash = _fingerprint(payload)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._idempotent_result_conn(
                conn, "message", actor, task_id, key, request_hash
            )
            if existing:
                return self._task_detail_conn(conn, existing)
            task = self._task_row_conn(conn, task_id)
            if not task:
                return None
            self._assert_context(task, payload)
            current = self._message_row_conn(conn, task["current_message_id"])
            current_event = self._current_event_conn(conn, task_id, task["current_message_id"])
            if not current or current["delivery_status"] != "delivered":
                raise ConflictError("new message requires a delivered current Message")
            if not current_event or current_event["outbox_status"] != "acked":
                raise ConflictError("new message requires an acked current outbox Event")
            if actor != task["to_agent_id"] or actor == task["from_agent_id"]:
                raise ConflictError("only the current to_agent_id may send the next Message")

            next_turn = int(task["turn_sequence"])
            if actor == task["requester_agent_id"]:
                if task["from_agent_id"] != task["target_agent_id"]:
                    raise ConflictError("requester follow-up requires a delivered target response")
                if next_turn >= int(task["max_turns"]):
                    raise ConflictError("max_turns_reached", code="max_turns_reached")
                next_turn += 1
            elif actor != task["target_agent_id"] or task["from_agent_id"] != task["requester_agent_id"]:
                raise ConflictError("target response requires a delivered requester Message")

            message_id = f"msg_{uuid.uuid4().hex}"
            event_id = f"evt_{uuid.uuid4().hex}"
            to_agent = str(task["from_agent_id"])
            next_version = int(task["task_version"]) + 1
            self._insert_message_conn(
                conn,
                message_id=message_id,
                task_id=task_id,
                turn_sequence=next_turn,
                from_agent_id=actor,
                to_agent_id=to_agent,
                parts=payload["parts"],
                idempotency_key=key,
                now=timestamp,
            )
            cursor = conn.execute(
                """
                UPDATE tasks
                SET turn_sequence = ?, current_message_id = ?, from_agent_id = ?,
                    to_agent_id = ?, task_version = ?, updated_at = ?
                WHERE task_id = ? AND status = 'open' AND task_version = ?
                """,
                (
                    next_turn, message_id, actor, to_agent, next_version, timestamp,
                    task_id, task["task_version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("stale_task_version", code="stale_task_version")
            self._insert_pending_event_conn(
                conn,
                event_id=event_id,
                task_id=task_id,
                message_id=message_id,
                target_agent_id=to_agent,
                turn_sequence=next_turn,
                task_version=next_version,
                now=timestamp,
            )
            self._audit_conn(
                conn, task_id, "message.created", actor, message_id,
                {"turn_sequence": next_turn, "task_version": next_version}, timestamp,
            )
            self._record_idempotency_conn(
                conn, "message", actor, task_id, key, request_hash,
                task_id, message_id, timestamp,
            )
            return self._task_detail_conn(conn, task_id)

    def claim_due_event(
        self,
        agent_id: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        timestamp = _now(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT e.*
                FROM agent_events e
                JOIN tasks t ON t.task_id = e.task_id
                WHERE e.agent_id = ?
                  AND (e.can_transition_message = 0 OR t.status = 'open') AND (
                    e.outbox_status = 'queued'
                    OR (e.outbox_status = 'retry_wait' AND e.next_retry_at <= ?)
                )
                ORDER BY e.can_transition_message DESC,
                         COALESCE(e.next_retry_at, e.created_at), e.created_at, e.event_id
                LIMIT 1
                """,
                (agent_id, timestamp),
            ).fetchone()
            if not row:
                return None
            cursor = conn.execute(
                """
                UPDATE agent_events
                SET outbox_status = 'inflight', outbox_attempts = outbox_attempts + 1,
                    inflight_until = ?, next_retry_at = NULL, updated_at = ?
                WHERE event_id = ? AND outbox_status = ? AND outbox_attempts < ?
                """,
                (
                    timestamp + DELIVERY_ACK_LEASE_SECONDS, timestamp, row["event_id"],
                    row["outbox_status"], MAX_DELIVERY_ATTEMPTS,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return self._event_dict(
                conn.execute("SELECT * FROM agent_events WHERE event_id = ?", (row["event_id"],)).fetchone()
            )

    def recover_event(
        self,
        agent_id: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        timestamp = _now(now)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_events
                WHERE agent_id = ? AND outbox_status = 'inflight'
                  AND inflight_until > ?
                ORDER BY can_transition_message DESC, created_at, event_id LIMIT 1
                """,
                (agent_id, timestamp),
            ).fetchone()
            if row:
                return self._event_dict(row)
        return self.claim_due_event(agent_id, now=timestamp)

    def record_attempt_failure(
        self,
        event_id: str,
        error: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        if error not in OUTBOX_LAST_ERRORS:
            raise ValueError(f"unsupported outbox error: {error}")
        timestamp = _now(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM agent_events WHERE event_id = ?", (event_id,)).fetchone()
            if not row:
                return None
            self._record_attempt_failure_conn(conn, dict(row), error, timestamp)
            return self._event_dict(
                conn.execute("SELECT * FROM agent_events WHERE event_id = ?", (event_id,)).fetchone()
            )

    def expire_ack_leases(self, *, now: int | None = None) -> int:
        timestamp = _now(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM agent_events
                WHERE outbox_status = 'inflight' AND inflight_until <= ?
                ORDER BY inflight_until, event_id
                """,
                (timestamp,),
            ).fetchall()
            for row in rows:
                self._record_attempt_failure_conn(conn, dict(row), "ack_lease_expired", timestamp)
            return len(rows)

    def ack_message(
        self,
        agent_id: str,
        payload: dict[str, Any],
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        return self._finish_delivery(agent_id, payload, failed=False, now=now)

    def fail_delivery(
        self,
        agent_id: str,
        payload: dict[str, Any],
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        return self._finish_delivery(agent_id, payload, failed=True, now=now)

    def ack_informational_event(
        self,
        agent_id: str,
        event_id: str,
        payload: dict[str, Any],
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        timestamp = _now(now)
        key = str(payload["idempotency_key"])
        request_hash = _fingerprint(payload)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM agent_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if not row:
                return None
            existing = self._idempotent_result_conn(
                conn, "event_ack", agent_id, event_id, key, request_hash
            )
            if existing:
                return self._event_dict(row)
            self._assert_listener_epoch_conn(
                conn,
                agent_id,
                str(payload["listener_instance_id"]),
                int(payload["readiness_epoch"]),
            )
            if row["agent_id"] != agent_id or row["can_transition_message"]:
                raise ConflictError("Event is not an informational Event for this Agent")
            if row["outbox_status"] not in {"inflight", "retry_wait"}:
                raise ConflictError("informational Event is not ACK eligible")
            conn.execute(
                """
                UPDATE agent_events SET outbox_status = 'acked', inflight_until = NULL,
                    next_retry_at = NULL, acked_at = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (timestamp, timestamp, event_id),
            )
            self._record_idempotency_conn(
                conn, "event_ack", agent_id, event_id, key, request_hash,
                row["task_id"], row["message_id"], timestamp,
            )
            return self._event_dict(
                conn.execute("SELECT * FROM agent_events WHERE event_id = ?", (event_id,)).fetchone()
            )

    def complete_task(
        self,
        task_id: str,
        payload: dict[str, Any],
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        return self._terminal_task(task_id, payload, completed=True, now=now)

    def fail_task(
        self,
        task_id: str,
        payload: dict[str, Any],
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        return self._terminal_task(task_id, payload, completed=False, now=now)

    def expire_tasks(self, *, now: int | None = None) -> int:
        timestamp = _now(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'open' AND task_expires_at <= ?
                ORDER BY task_expires_at, task_id
                """,
                (timestamp,),
            ).fetchall()
            for row in rows:
                self._expire_task_conn(conn, dict(row), timestamp)
            return len(rows)

    def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self._task_detail_conn(conn, task_id)

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            return self._agent_conn(conn, agent_id) if row else None

    def get_readiness(self, agent_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT agent_id FROM agent_listener_readiness WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            return self._readiness_conn(conn, agent_id) if row else None

    def assert_listener_epoch(
        self,
        agent_id: str,
        listener_instance_id: str,
        readiness_epoch: int,
    ) -> None:
        with self.connect() as conn:
            self._assert_listener_epoch_conn(
                conn, agent_id, listener_instance_id, readiness_epoch
            )

    def list_due_agent_ids(self, *, now: int | None = None) -> list[str]:
        timestamp = _now(now)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT e.agent_id
                FROM agent_events e
                JOIN tasks t ON t.task_id = e.task_id
                WHERE (e.can_transition_message = 0 OR t.status = 'open') AND (
                    e.outbox_status = 'queued'
                    OR (e.outbox_status = 'retry_wait' AND e.next_retry_at <= ?)
                )
                ORDER BY e.agent_id
                """,
                (timestamp,),
            ).fetchall()
            return [str(row["agent_id"]) for row in rows]

    def get_lineage(self, task_id: str) -> list[dict[str, Any]] | None:
        with self.connect() as conn:
            task = self._task_row_conn(conn, task_id)
            if not task:
                return None
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE root_task_id = ? ORDER BY created_at, task_id",
                (task["root_task_id"],),
            ).fetchall()
            return [self._task_dict(self._task_row_conn(conn, row["task_id"])) for row in rows]

    def visibility(self, task_id: str, *, now: int | None = None) -> dict[str, Any] | None:
        timestamp = _now(now)
        with self.connect() as conn:
            task_row = self._task_row_conn(conn, task_id)
            if not task_row:
                return None
            message_row = self._message_row_conn(conn, task_row["current_message_id"])
            event_row = self._current_event_conn(conn, task_id, task_row["current_message_id"])
            if not message_row or not event_row:
                raise ConflictError("invariant_violation", code="invariant_violation")
            task = self._task_dict(task_row)
            message = self._message_dict(message_row)
            event = self._event_dict(event_row)
            diagnosis = _diagnose(task, message, event)
            return {
                "protocol_version": PROTOCOL_V05,
                "diagnosis_version": 1,
                "generated_at": timestamp,
                "task": task,
                "current_message": message,
                "outbox": event,
                "diagnosis": diagnosis,
            }

    def visibility_batch(
        self,
        task_ids: list[str],
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for task_id in task_ids:
            item = self.visibility(task_id, now=now)
            if item is None:
                errors.append({"task_id": task_id, "code": "task_not_found"})
            else:
                items.append(item)
        return {"items": items, "errors": errors}

    def admin_summary(self, *, now: int | None = None) -> dict[str, Any]:
        timestamp = _now(now)
        with self.connect() as conn:
            task_status = {
                str(row["status"]): int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
                ).fetchall()
            }
            delivery_status = {
                str(row["delivery_status"]): int(row["count"])
                for row in conn.execute(
                    "SELECT delivery_status, COUNT(*) AS count FROM messages GROUP BY delivery_status"
                ).fetchall()
            }
            outbox_status = {
                str(row["outbox_status"]): int(row["count"])
                for row in conn.execute(
                    "SELECT outbox_status, COUNT(*) AS count FROM agent_events GROUP BY outbox_status"
                ).fetchall()
            }
            recent = [
                {
                    **dict(row),
                    "payload": json.loads(row["payload_json"]),
                }
                for row in conn.execute(
                    """
                    SELECT audit_id, task_id, event_type, actor_agent_id, message_id,
                           payload_json, created_at
                    FROM task_audit_events
                    ORDER BY created_at DESC, audit_id DESC LIMIT 50
                    """
                ).fetchall()
            ]
            for item in recent:
                item.pop("payload_json", None)
            task_ids = [row["task_id"] for row in conn.execute("SELECT task_id FROM tasks")]
            stale_readiness = conn.execute(
                """
                SELECT COUNT(*) FROM agents a
                LEFT JOIN agent_listener_readiness r ON r.agent_id = a.agent_id
                WHERE a.enabled = 1 AND (
                    r.agent_id IS NULL OR r.ready = 0 OR r.observed_at < ?
                )
                """,
                (timestamp - LISTENER_READINESS_MAX_AGE_SECONDS,),
            ).fetchone()[0]
            due_events = conn.execute(
                """
                SELECT COUNT(*) FROM agent_events
                WHERE outbox_status = 'queued'
                   OR (outbox_status = 'retry_wait' AND next_retry_at <= ?)
                   OR (outbox_status = 'inflight' AND inflight_until <= ?)
                """,
                (timestamp, timestamp),
            ).fetchone()[0]
            agents = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]

        invariant_violations = 0
        for task_id in task_ids:
            try:
                if self.visibility(task_id, now=timestamp)["diagnosis"] == "invariant_violation":
                    invariant_violations += 1
            except ConflictError:
                invariant_violations += 1
        total_tasks = sum(task_status.values())
        alerts = []
        for code, count in (
            ("invariant_violation", invariant_violations),
            ("due_work_lag", int(due_events)),
            ("exhausted_outbox", outbox_status.get("exhausted", 0)),
            ("stale_enabled_agent", int(stale_readiness)),
        ):
            if count:
                alerts.append({"code": code, "count": count})
        return {
            "protocol_version": PROTOCOL_V05,
            "generated_at": timestamp,
            "agents": int(agents),
            "tasks": {
                "total": total_tasks,
                "active": task_status.get("open", 0),
                "by_status": task_status,
            },
            "messages": {"by_delivery_status": delivery_status},
            "outbox": {
                "by_status": outbox_status,
                "unacked": sum(outbox_status.get(key, 0) for key in ("queued", "inflight", "retry_wait")),
                "due": int(due_events),
                "exhausted": outbox_status.get("exhausted", 0),
            },
            "readiness": {"stale_enabled_agents": int(stale_readiness)},
            "invariant_violations": invariant_violations,
            "alerts": alerts,
            "recent_task_events": recent,
        }

    def admin_agents(self, *, now: int | None = None) -> list[dict[str, Any]]:
        timestamp = _now(now)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*, r.protocol_version AS readiness_protocol_version,
                       r.client_version, r.workspace_version, r.listener_instance_id,
                       r.readiness_epoch, r.transport, r.ready, r.observed_at,
                       (SELECT COUNT(*) FROM tasks t
                        WHERE t.status = 'open'
                          AND (t.requester_agent_id = a.agent_id OR t.target_agent_id = a.agent_id)) AS active_task_count,
                       (SELECT COUNT(*) FROM agent_events e
                        WHERE e.agent_id = a.agent_id
                          AND e.outbox_status IN ('queued', 'inflight', 'retry_wait')) AS pending_event_count
                FROM agents a
                LEFT JOIN agent_listener_readiness r ON r.agent_id = a.agent_id
                ORDER BY a.agent_id
                """
            ).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["enabled"] = bool(value["enabled"])
            value["protocol_capabilities"] = json.loads(value.pop("protocol_capabilities_json"))
            value["ready"] = bool(value["ready"]) if value["ready"] is not None else False
            value["readiness_fresh"] = bool(
                value["ready"]
                and value["observed_at"] is not None
                and int(value["observed_at"]) >= timestamp - LISTENER_READINESS_MAX_AGE_SECONDS
            )
            values.append(value)
        return values

    def admin_tasks(
        self,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        active: bool | None = None,
        limit: int = 100,
        now: int | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if agent_id:
            where.append("(requester_agent_id = ? OR target_agent_id = ?)")
            params.extend((agent_id, agent_id))
        if status:
            where.append("status = ?")
            params.append(status)
        if active is True:
            where.append("status = 'open'")
        elif active is False:
            where.append("status != 'open'")
        sql = "SELECT task_id FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, task_id LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            task_ids = [row["task_id"] for row in conn.execute(sql, params).fetchall()]
        values = []
        for task_id in task_ids:
            visibility = self.visibility(task_id, now=now)
            if visibility:
                values.append(
                    {
                        **visibility["task"],
                        "current_message": visibility["current_message"],
                        "outbox": visibility["outbox"],
                        "diagnosis": visibility["diagnosis"],
                    }
                )
        return values

    def admin_task_detail(self, task_id: str, *, now: int | None = None) -> dict[str, Any] | None:
        detail = self.get_task_detail(task_id)
        if not detail:
            return None
        visibility = self.visibility(task_id, now=now)
        with self.connect() as conn:
            audit_events = []
            for row in conn.execute(
                """
                SELECT * FROM task_audit_events
                WHERE task_id = ? ORDER BY created_at, audit_id
                """,
                (task_id,),
            ).fetchall():
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                audit_events.append(item)
        return {
            **detail,
            "visibility": visibility,
            "audit_events": audit_events,
        }

    def admin_outbox_events(
        self,
        *,
        agent_id: str | None = None,
        outbox_status: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if agent_id:
            where.append("e.agent_id = ?")
            params.append(agent_id)
        if outbox_status:
            where.append("e.outbox_status = ?")
            params.append(outbox_status)
        if task_id:
            where.append("e.task_id = ?")
            params.append(task_id)
        sql = """
            SELECT e.*, t.status AS task_status
            FROM agent_events e JOIN tasks t ON t.task_id = e.task_id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.created_at DESC, e.event_id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            value["can_transition_message"] = bool(value["can_transition_message"])
            values.append(value)
        return values

    def _finish_delivery(
        self,
        agent_id: str,
        payload: dict[str, Any],
        *,
        failed: bool,
        now: int | None,
    ) -> dict[str, Any] | None:
        timestamp = _now(now)
        task_id = str(payload["task_id"])
        operation = "delivery_fail" if failed else "ack"
        key = str(payload["idempotency_key"])
        request_hash = _fingerprint(payload)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._idempotent_result_conn(
                conn, operation, agent_id, task_id, key, request_hash
            )
            if existing:
                return self._task_detail_conn(conn, existing)
            self._assert_listener_epoch_conn(
                conn,
                agent_id,
                str(payload["listener_instance_id"]),
                int(payload["readiness_epoch"]),
            )
            task = self._task_row_conn(conn, task_id)
            if not task:
                return None
            self._assert_context(task, payload)
            if task["to_agent_id"] != agent_id:
                raise ConflictError("only current to_agent_id may ACK/NACK")
            event = conn.execute(
                "SELECT * FROM agent_events WHERE event_id = ?",
                (payload["event_id"],),
            ).fetchone()
            message = self._message_row_conn(conn, payload["message_id"])
            if (
                not event
                or not message
                or event["task_id"] != task_id
                or event["message_id"] != payload["message_id"]
                or not event["can_transition_message"]
            ):
                raise ConflictError("stale_message", code="stale_message")
            if event["outbox_status"] not in {"inflight", "retry_wait"}:
                raise ConflictError("delivery Event is not ACK/NACK eligible")
            if message["delivery_status"] != "pending":
                raise ConflictError("current Message is not pending")

            next_version = int(task["task_version"]) + 1
            if failed:
                reason = "listener_persistence_failed"
                conn.execute(
                    """
                    UPDATE agent_events SET outbox_status = 'exhausted', inflight_until = NULL,
                        next_retry_at = NULL, exhausted_at = ?, exhaustion_reason = ?, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (timestamp, reason, timestamp, event["event_id"]),
                )
                conn.execute(
                    """
                    UPDATE messages SET delivery_status = 'failed', failed_at = ?,
                        delivery_reason = ?, updated_at = ? WHERE message_id = ?
                    """,
                    (timestamp, reason, timestamp, message["message_id"]),
                )
                conn.execute(
                    """
                    UPDATE tasks SET status = 'failed', task_version = ?, reason = ?,
                        terminal_by_agent_id = ?, updated_at = ?
                    WHERE task_id = ? AND status = 'open' AND task_version = ?
                    """,
                    (next_version, reason, agent_id, timestamp, task_id, task["task_version"]),
                )
                audit_type = "message.delivery_failed"
            else:
                conn.execute(
                    """
                    UPDATE agent_events SET outbox_status = 'acked', inflight_until = NULL,
                        next_retry_at = NULL, acked_at = ?, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (timestamp, timestamp, event["event_id"]),
                )
                conn.execute(
                    """
                    UPDATE messages SET delivery_status = 'delivered', delivered_at = ?,
                        delivery_reason = NULL, updated_at = ? WHERE message_id = ?
                    """,
                    (timestamp, timestamp, message["message_id"]),
                )
                conn.execute(
                    """
                    UPDATE tasks SET task_version = ?, updated_at = ?
                    WHERE task_id = ? AND status = 'open' AND task_version = ?
                    """,
                    (next_version, timestamp, task_id, task["task_version"]),
                )
                audit_type = "message.delivered"
            self._audit_conn(
                conn, task_id, audit_type, agent_id, message["message_id"],
                {"task_version": next_version}, timestamp,
            )
            delivery_status = "failed" if failed else "delivered"
            self._insert_info_event_conn(
                conn,
                agent_id=message["from_agent_id"],
                event_type="message.delivery_changed",
                task_id=task_id,
                message_id=message["message_id"],
                payload={
                    "delivery_status": delivery_status,
                    "task_version": next_version,
                },
                idempotency_key=f"v05:{message['message_id']}:delivery:{delivery_status}",
                now=timestamp,
            )
            if failed:
                self._notify_task_status_conn(
                    conn, task, "failed", reason, agent_id, next_version, timestamp
                )
            self._record_idempotency_conn(
                conn, operation, agent_id, task_id, key, request_hash,
                task_id, message["message_id"], timestamp,
            )
            return self._task_detail_conn(conn, task_id)

    def _terminal_task(
        self,
        task_id: str,
        payload: dict[str, Any],
        *,
        completed: bool,
        now: int | None,
    ) -> dict[str, Any] | None:
        timestamp = _now(now)
        actor = str(payload["actor_agent_id"])
        operation = "complete" if completed else "fail"
        key = str(payload["idempotency_key"])
        request_hash = _fingerprint(payload)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._idempotent_result_conn(
                conn, operation, actor, task_id, key, request_hash
            )
            if existing:
                return self._task_detail_conn(conn, existing)
            task = self._task_row_conn(conn, task_id)
            if not task:
                return None
            self._assert_context(task, payload)
            message = self._message_row_conn(conn, task["current_message_id"])
            event = self._current_event_conn(conn, task_id, task["current_message_id"])
            if not message or not event:
                raise ConflictError("invariant_violation", code="invariant_violation")

            if completed:
                evidence = str(payload["completed_against_message_id"])
                if actor != task["requester_agent_id"]:
                    raise ConflictError("only requester may complete the Task")
                if (
                    message["delivery_status"] != "delivered"
                    or event["outbox_status"] != "acked"
                    or task["from_agent_id"] != task["target_agent_id"]
                    or evidence != task["current_message_id"]
                ):
                    raise ConflictError("completion requires current delivered target evidence")
                terminal_status = "completed"
                reason = "goal_met"
                completed_against = evidence
            else:
                reason = str(payload["reason"])
                if reason not in TASK_FAILURE_REASONS:
                    raise ValueError(f"unsupported failed reason: {reason}")
                self._assert_failure_authority(task, message, actor, reason)
                terminal_status = "failed"
                completed_against = None

            next_version = int(task["task_version"]) + 1
            if message["delivery_status"] == "pending":
                conn.execute(
                    """
                    UPDATE messages SET delivery_status = 'failed', failed_at = ?,
                        delivery_reason = ?, updated_at = ? WHERE message_id = ?
                    """,
                    (timestamp, reason, timestamp, message["message_id"]),
                )
                if event["outbox_status"] in {"queued", "inflight", "retry_wait"}:
                    conn.execute(
                        """
                        UPDATE agent_events SET outbox_status = 'exhausted', inflight_until = NULL,
                            next_retry_at = NULL, exhausted_at = ?, exhaustion_reason = 'task_failed',
                            updated_at = ? WHERE event_id = ?
                        """,
                        (timestamp, timestamp, event["event_id"]),
                    )
            cursor = conn.execute(
                """
                UPDATE tasks SET status = ?, task_version = ?, reason = ?,
                    terminal_by_agent_id = ?, completed_against_message_id = ?, updated_at = ?
                WHERE task_id = ? AND status = 'open' AND task_version = ?
                """,
                (
                    terminal_status, next_version, reason,
                    None if actor == "relay" else actor, completed_against,
                    timestamp, task_id, task["task_version"],
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("stale_task_version", code="stale_task_version")
            self._audit_conn(
                conn, task_id, f"task.{terminal_status}",
                None if actor == "relay" else actor, message["message_id"],
                {"reason": reason, "task_version": next_version}, timestamp,
            )
            self._notify_task_status_conn(
                conn, task, terminal_status, reason, actor, next_version, timestamp
            )
            self._record_idempotency_conn(
                conn, operation, actor, task_id, key, request_hash,
                task_id, message["message_id"], timestamp,
            )
            return self._task_detail_conn(conn, task_id)

    def _record_attempt_failure_conn(
        self,
        conn: sqlite3.Connection,
        event: dict[str, Any],
        error: str,
        now: int,
    ) -> None:
        if event["outbox_status"] != "inflight":
            raise ConflictError("delivery Event is not inflight")
        attempts = int(event["outbox_attempts"])
        if attempts >= MAX_DELIVERY_ATTEMPTS:
            conn.execute(
                """
                UPDATE agent_events SET outbox_status = 'exhausted', inflight_until = NULL,
                    next_retry_at = NULL, exhausted_at = ?, exhaustion_reason = 'delivery_retry_exhausted',
                    last_error = ?, updated_at = ? WHERE event_id = ?
                """,
                (now, error, now, event["event_id"]),
            )
            if event["can_transition_message"]:
                task = self._task_row_conn(conn, event["task_id"])
                message = self._message_row_conn(conn, event["message_id"])
                if (
                    task
                    and message
                    and task["status"] == "open"
                    and task["current_message_id"] == event["message_id"]
                    and message["delivery_status"] == "pending"
                ):
                    next_version = int(task["task_version"]) + 1
                    conn.execute(
                        """
                        UPDATE messages SET delivery_status = 'failed', failed_at = ?,
                            delivery_reason = 'delivery_retry_exhausted', updated_at = ?
                        WHERE message_id = ?
                        """,
                        (now, now, message["message_id"]),
                    )
                    conn.execute(
                        """
                        UPDATE tasks SET status = 'failed', task_version = ?,
                            reason = 'delivery_retry_exhausted', terminal_by_agent_id = NULL,
                            updated_at = ? WHERE task_id = ? AND status = 'open'
                        """,
                        (next_version, now, task["task_id"]),
                    )
                    self._audit_conn(
                        conn, task["task_id"], "task.failed", None, message["message_id"],
                        {"reason": "delivery_retry_exhausted", "task_version": next_version}, now,
                    )
                    self._insert_info_event_conn(
                        conn,
                        agent_id=message["from_agent_id"],
                        event_type="message.delivery_changed",
                        task_id=task["task_id"],
                        message_id=message["message_id"],
                        payload={
                            "delivery_status": "failed",
                            "task_version": next_version,
                        },
                        idempotency_key=f"v05:{message['message_id']}:delivery:failed",
                        now=now,
                    )
                    self._notify_task_status_conn(
                        conn, task, "failed", "delivery_retry_exhausted", None,
                        next_version, now,
                    )
            return
        backoff = RETRY_BACKOFF_SECONDS[attempts - 1]
        conn.execute(
            """
            UPDATE agent_events SET outbox_status = 'retry_wait', inflight_until = NULL,
                next_retry_at = ?, last_error = ?, updated_at = ? WHERE event_id = ?
            """,
            (now + backoff, error, now, event["event_id"]),
        )
        self._audit_conn(
            conn, event["task_id"], "message.delivery_attempt_failed", None,
            event["message_id"],
            {"attempt": attempts, "last_error": error, "next_retry_at": now + backoff}, now,
        )
        if event["can_transition_message"]:
            message = self._message_row_conn(conn, event["message_id"])
            if message:
                self._insert_info_event_conn(
                    conn,
                    agent_id=message["from_agent_id"],
                    event_type="message.delivery_attempt_failed",
                    task_id=event["task_id"],
                    message_id=event["message_id"],
                    payload={
                        "attempt": attempts,
                        "last_error": error,
                        "next_retry_at": now + backoff,
                    },
                    idempotency_key=f"v05:{event['event_id']}:attempt:{attempts}:failed",
                    now=now,
                )

    def _expire_task_conn(self, conn: sqlite3.Connection, task: dict[str, Any], now: int) -> None:
        message = self._message_row_conn(conn, task["current_message_id"])
        event = self._current_event_conn(conn, task["task_id"], task["current_message_id"])
        if message and message["delivery_status"] == "pending":
            conn.execute(
                """
                UPDATE messages SET delivery_status = 'failed', failed_at = ?,
                    delivery_reason = 'task_expired', updated_at = ? WHERE message_id = ?
                """,
                (now, now, message["message_id"]),
            )
        if event and event["outbox_status"] in {"queued", "inflight", "retry_wait"}:
            conn.execute(
                """
                UPDATE agent_events SET outbox_status = 'exhausted', inflight_until = NULL,
                    next_retry_at = NULL, exhausted_at = ?, exhaustion_reason = 'task_expired',
                    updated_at = ? WHERE event_id = ?
                """,
                (now, now, event["event_id"]),
            )
        next_version = int(task["task_version"]) + 1
        cursor = conn.execute(
            """
            UPDATE tasks SET status = 'expired', task_version = ?, reason = 'task_timeout',
                terminal_by_agent_id = NULL, updated_at = ?
            WHERE task_id = ? AND status = 'open' AND task_version = ?
            """,
            (next_version, now, task["task_id"], task["task_version"]),
        )
        if cursor.rowcount == 1:
            self._audit_conn(
                conn, task["task_id"], "task.expired", None, task["current_message_id"],
                {"reason": "task_timeout", "task_version": next_version}, now,
            )
            self._notify_task_status_conn(
                conn, task, "expired", "task_timeout", None, next_version, now
            )

    def _assert_failure_authority(
        self,
        task: sqlite3.Row | dict[str, Any],
        message: sqlite3.Row | dict[str, Any],
        actor: str,
        reason: str,
    ) -> None:
        if reason in {"delivery_retry_exhausted", "relay_persistence_failed", "internal_consistency_error"}:
            if actor != "relay":
                raise ConflictError(f"{reason} may only be recorded by Relay")
        elif reason == "listener_persistence_failed":
            if actor != task["to_agent_id"] or message["delivery_status"] != "pending":
                raise ConflictError("listener_persistence_failed requires current Listener")
        elif reason == "agent_reported_failure":
            if actor != task["to_agent_id"] or message["delivery_status"] != "delivered":
                raise ConflictError("agent_reported_failure requires current action owner")
        elif reason == "max_turns_exhausted":
            if (
                actor != task["requester_agent_id"]
                or int(task["turn_sequence"]) < int(task["max_turns"])
                or message["delivery_status"] != "delivered"
                or task["from_agent_id"] != task["target_agent_id"]
            ):
                raise ConflictError("max_turns_exhausted requires requester at delivered max_turns")

    def _assert_context(self, task: sqlite3.Row | dict[str, Any], payload: dict[str, Any]) -> None:
        if task["status"] != "open":
            raise ConflictError(f"task is terminal: {task['status']}")
        if payload.get("message_id") != task["current_message_id"]:
            raise ConflictError("stale_message", code="stale_message", current_task=self._task_dict(task))
        if int(payload.get("turn_sequence")) != int(task["turn_sequence"]):
            raise ConflictError("stale_turn", code="stale_turn", current_task=self._task_dict(task))
        if int(payload.get("expected_task_version")) != int(task["task_version"]):
            raise ConflictError(
                "stale_task_version", code="stale_task_version", current_task=self._task_dict(task)
            )

    def _assert_admission_conn(self, conn: sqlite3.Connection, agent_id: str, now: int) -> None:
        agent = self._require_agent_conn(conn, agent_id)
        capabilities = json.loads(agent["protocol_capabilities_json"])
        if not agent["enabled"] or PROTOCOL_V05 not in capabilities:
            raise ConflictError("protocol_v05_required", code="protocol_v05_required")
        readiness = conn.execute(
            "SELECT * FROM agent_listener_readiness WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if (
            not readiness
            or readiness["protocol_version"] != PROTOCOL_V05
            or not readiness["ready"]
            or int(readiness["observed_at"]) < now - LISTENER_READINESS_MAX_AGE_SECONDS
        ):
            raise ConflictError("listener_not_ready", code="listener_not_ready")

    def _assert_listener_epoch_conn(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        listener_instance_id: str,
        readiness_epoch: int,
    ) -> None:
        row = conn.execute(
            "SELECT * FROM agent_listener_readiness WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if (
            not row
            or row["listener_instance_id"] != listener_instance_id
            or int(row["readiness_epoch"]) != readiness_epoch
        ):
            raise ConflictError("stale_readiness_epoch", code="stale_readiness_epoch")

    def _insert_message_conn(
        self,
        conn: sqlite3.Connection,
        *,
        message_id: str,
        task_id: str,
        turn_sequence: int,
        from_agent_id: str,
        to_agent_id: str,
        parts: list[dict[str, Any]],
        idempotency_key: str,
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO messages (
                message_id, task_id, turn_sequence, from_agent_id, to_agent_id,
                parts_json, idempotency_key, delivery_status, max_delivery_attempts,
                delivered_at, failed_at, delivery_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, NULL, ?, ?)
            """,
            (
                message_id, task_id, turn_sequence, from_agent_id, to_agent_id,
                json.dumps(parts, sort_keys=True), idempotency_key,
                MAX_DELIVERY_ATTEMPTS, now, now,
            ),
        )

    def _insert_pending_event_conn(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        task_id: str,
        message_id: str,
        target_agent_id: str,
        turn_sequence: int,
        task_version: int,
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO agent_events (
                event_id, agent_id, event_type, task_id, message_id, payload_json,
                idempotency_key, outbox_status, outbox_attempts, inflight_until,
                next_retry_at, acked_at, exhausted_at, exhaustion_reason, last_error,
                can_transition_message, created_at, updated_at
            ) VALUES (?, ?, 'message.pending', ?, ?, ?, ?, 'queued', 0, NULL,
                NULL, NULL, NULL, NULL, NULL, 1, ?, ?)
            """,
            (
                event_id, target_agent_id, task_id, message_id,
                json.dumps(
                    {
                        "protocol_version": PROTOCOL_V05,
                        "task_id": task_id,
                        "message_id": message_id,
                        "turn_sequence": turn_sequence,
                        "task_version": task_version,
                    },
                    sort_keys=True,
                ),
                f"v05:{message_id}:pending", now, now,
            ),
        )

    def _insert_info_event_conn(
        self,
        conn: sqlite3.Connection,
        *,
        agent_id: str,
        event_type: str,
        task_id: str,
        message_id: str | None,
        payload: dict[str, Any],
        idempotency_key: str,
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO agent_events (
                event_id, agent_id, event_type, task_id, message_id, payload_json,
                idempotency_key, outbox_status, outbox_attempts, inflight_until,
                next_retry_at, acked_at, exhausted_at, exhaustion_reason, last_error,
                can_transition_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, NULL, NULL, NULL, NULL,
                NULL, NULL, 0, ?, ?)
            """,
            (
                f"evt_{uuid.uuid4().hex}", agent_id, event_type, task_id, message_id,
                json.dumps({"protocol_version": PROTOCOL_V05, **payload}, sort_keys=True),
                idempotency_key, now, now,
            ),
        )

    def _notify_task_status_conn(
        self,
        conn: sqlite3.Connection,
        task: sqlite3.Row | dict[str, Any],
        status: str,
        reason: str,
        actor_agent_id: str | None,
        task_version: int,
        now: int,
    ) -> None:
        participants = {str(task["requester_agent_id"]), str(task["target_agent_id"])}
        recipients = participants - {actor_agent_id} if actor_agent_id in participants else participants
        for recipient in sorted(recipients):
            self._insert_info_event_conn(
                conn,
                agent_id=recipient,
                event_type="task.status_changed",
                task_id=task["task_id"],
                message_id=task["current_message_id"],
                payload={
                    "status": status,
                    "reason": reason,
                    "task_version": task_version,
                },
                idempotency_key=f"v05:{task['task_id']}:status:{task_version}:{recipient}",
                now=now,
            )

    def _audit_conn(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        event_type: str,
        actor_agent_id: str | None,
        message_id: str | None,
        payload: dict[str, Any],
        created_at: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_audit_events (
                audit_id, task_id, event_type, actor_agent_id, message_id,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit_{uuid.uuid4().hex}", task_id, event_type, actor_agent_id,
                message_id, json.dumps(payload, sort_keys=True), created_at,
            ),
        )

    def _idempotent_result_conn(
        self,
        conn: sqlite3.Connection,
        operation: str,
        actor: str,
        scope: str,
        key: str,
        request_hash: str,
    ) -> str | None:
        row = conn.execute(
            """
            SELECT result_task_id, request_hash FROM idempotency_records
            WHERE operation = ? AND actor_agent_id = ? AND task_scope = ?
              AND idempotency_key = ?
            """,
            (operation, actor, scope, key),
        ).fetchone()
        if not row:
            return None
        if row["request_hash"] != request_hash:
            raise ConflictError("idempotency_key was reused with a different request")
        return str(row["result_task_id"])

    def _record_idempotency_conn(
        self,
        conn: sqlite3.Connection,
        operation: str,
        actor: str,
        scope: str,
        key: str,
        request_hash: str,
        task_id: str,
        message_id: str | None,
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO idempotency_records (
                operation, actor_agent_id, task_scope, idempotency_key,
                request_hash, result_task_id, result_message_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (operation, actor, scope, key, request_hash, task_id, message_id, now),
        )

    def _task_detail_conn(self, conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
        task = self._task_row_conn(conn, task_id)
        if not task:
            return None
        messages = conn.execute(
            """
            SELECT * FROM messages WHERE task_id = ?
            ORDER BY turn_sequence,
                CASE WHEN from_agent_id = ? THEN 0 ELSE 1 END,
                created_at, message_id
            """,
            (task_id, task["requester_agent_id"]),
        ).fetchall()
        return {
            "task": self._task_dict(task),
            "messages": [self._message_dict(row) for row in messages],
        }

    def _task_row_conn(self, conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()

    def _message_row_conn(self, conn: sqlite3.Connection, message_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()

    def _current_event_conn(
        self, conn: sqlite3.Connection, task_id: str, message_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM agent_events
            WHERE task_id = ? AND message_id = ? AND can_transition_message = 1
            ORDER BY created_at DESC, event_id DESC LIMIT 1
            """,
            (task_id, message_id),
        ).fetchone()

    def _require_agent_conn(self, conn: sqlite3.Connection, agent_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if not row:
            raise ValueError(f"unknown agent: {agent_id}")
        return row

    def _agent_conn(self, conn: sqlite3.Connection, agent_id: str) -> dict[str, Any]:
        row = self._require_agent_conn(conn, agent_id)
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        value["protocol_capabilities"] = json.loads(value.pop("protocol_capabilities_json"))
        return value

    def _readiness_conn(self, conn: sqlite3.Connection, agent_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM agent_listener_readiness WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"listener readiness not found: {agent_id}")
        value = dict(row)
        value["ready"] = bool(value["ready"])
        return value

    @staticmethod
    def _task_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
        if row is None:
            raise ValueError("task row is required")
        value = dict(row)
        value["done_criteria"] = json.loads(value["done_criteria"])
        return value

    @staticmethod
    def _message_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value["parts"] = json.loads(value.pop("parts_json"))
        return value

    @staticmethod
    def _event_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        value.pop("payload_json", None)
        value["can_transition_message"] = bool(value["can_transition_message"])
        return value


def _diagnose(task: dict[str, Any], message: dict[str, Any], event: dict[str, Any]) -> str:
    if task["status"] == "completed":
        return "task_completed"
    if task["status"] == "expired":
        return "task_expired"
    if task["status"] == "failed":
        if message["delivery_reason"] in DELIVERY_FAILURE_REASONS:
            return "task_failed_delivery"
        return "task_failed"
    if message["delivery_status"] == "pending":
        return {
            "queued": "message_queued",
            "inflight": "message_inflight",
            "retry_wait": "message_pending_retry",
        }.get(event["outbox_status"], "invariant_violation")
    if message["delivery_status"] == "delivered" and event["outbox_status"] == "acked":
        if task["from_agent_id"] == task["requester_agent_id"]:
            return "waiting_target_response"
        if task["from_agent_id"] == task["target_agent_id"]:
            return "waiting_requester_decision"
    return "invariant_violation"


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _now(value: int | None) -> int:
    return int(time.time()) if value is None else int(value)
