from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Any
import uuid

from server.protocol_v06 import (
    COORDINATOR_GRANT_OPERATIONS,
    DELIVERY_ACK_LEASE_SECONDS,
    DELIVERY_FAILURE_REASONS,
    LISTENER_READINESS_MAX_AGE_SECONDS,
    MAX_AGENT_UNACKED_EVENTS,
    MAX_DELIVERY_ATTEMPTS,
    OUTBOX_LAST_ERRORS,
    PROTOCOL_V06,
    TASK_FAILURE_REASONS,
)
from server.delivery_control import (
    DeliveryClaimContext,
    DeliveryControl,
    default_delivery_control,
)
from server.store import ConflictError


DEFAULT_TASK_TTL_SECONDS = 24 * 60 * 60
INSTALL_HEALTHCHECK_AGENT_ID = "agentrelay-healthcheck"
INSTALL_HEALTHCHECK_TTL_SECONDS = 10 * 60


class CoordinatorGrantPermissionError(PermissionError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class V06Store:
    """Native Protocol v0.6 storage for the clean writable database."""

    def __init__(
        self,
        db_path: str,
        *,
        delivery_control: DeliveryControl | None = None,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()
        self.delivery_control = delivery_control or default_delivery_control(
            self.db_path, PROTOCOL_V06
        )

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

                CREATE TABLE IF NOT EXISTS agent_profiles (
                    agent_id TEXT PRIMARY KEY,
                    card_revision INTEGER NOT NULL CHECK (card_revision >= 1),
                    profile_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    root_task_id TEXT NOT NULL,
                    protocol_version TEXT NOT NULL CHECK (protocol_version = 'agent-collab-v0.6'),
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
                    subject TEXT,
                    metadata_json TEXT,
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
                    outbox_status TEXT NOT NULL CHECK (outbox_status IN ('queued', 'inflight', 'acked', 'retry_wait', 'parked', 'exhausted')),
                    outbox_attempts INTEGER NOT NULL CHECK (outbox_attempts BETWEEN 0 AND 4),
                    recovery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (recovery_attempts >= 0),
                    inflight_via TEXT CHECK (inflight_via IN ('push', 'recovery') OR inflight_via IS NULL),
                    inflight_until INTEGER,
                    inflight_started_at INTEGER,
                    parked_at INTEGER,
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

                CREATE TABLE IF NOT EXISTS coordinator_grants (
                    grant_id TEXT PRIMARY KEY,
                    issuance_key TEXT NOT NULL,
                    coordinator_agent_id TEXT NOT NULL,
                    investigation_id TEXT NOT NULL,
                    round_id TEXT NOT NULL,
                    approved_plan_digest TEXT NOT NULL,
                    authority_ref TEXT NOT NULL,
                    task_count INTEGER NOT NULL CHECK (task_count >= 1),
                    task_expires_at INTEGER NOT NULL,
                    grant_expires_at INTEGER NOT NULL,
                    operations_json TEXT NOT NULL,
                    claims_digest TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    token_version INTEGER NOT NULL CHECK (token_version >= 1),
                    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
                    used_task_count INTEGER NOT NULL DEFAULT 0 CHECK (used_task_count >= 0),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE (coordinator_agent_id, issuance_key),
                    UNIQUE (coordinator_agent_id, investigation_id, round_id),
                    FOREIGN KEY (coordinator_agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS coordinator_grant_targets (
                    grant_id TEXT NOT NULL,
                    target_agent_id TEXT NOT NULL,
                    PRIMARY KEY (grant_id, target_agent_id),
                    FOREIGN KEY (grant_id) REFERENCES coordinator_grants(grant_id) ON DELETE RESTRICT,
                    FOREIGN KEY (target_agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS coordinator_grant_tasks (
                    grant_id TEXT NOT NULL,
                    work_item_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE,
                    target_agent_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (grant_id, work_item_id),
                    UNIQUE (grant_id, idempotency_key),
                    FOREIGN KEY (grant_id) REFERENCES coordinator_grants(grant_id) ON DELETE RESTRICT,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
                    FOREIGN KEY (target_agent_id) REFERENCES agents(agent_id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS coordinator_grant_audit (
                    audit_id TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_agent_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (grant_id) REFERENCES coordinator_grants(grant_id) ON DELETE RESTRICT
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
                CREATE INDEX IF NOT EXISTS idx_coordinator_grants_expiry
                    ON coordinator_grants (status, grant_expires_at, grant_id);
                CREATE INDEX IF NOT EXISTS idx_coordinator_grant_tasks_task
                    ON coordinator_grant_tasks (task_id, grant_id);

                CREATE TRIGGER IF NOT EXISTS prevent_task_hard_delete
                BEFORE DELETE ON tasks
                BEGIN
                    SELECT RAISE(ABORT, 'AgentRelay protocol forbids hard deletion of tasks');
                END;
                """
            )
            message_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "subject" not in message_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN subject TEXT")
            if "metadata_json" not in message_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN metadata_json TEXT")
            event_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(agent_events)").fetchall()
            }
            if "recovery_attempts" not in event_columns:
                conn.execute(
                    "ALTER TABLE agent_events ADD COLUMN recovery_attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "inflight_via" not in event_columns:
                conn.execute("ALTER TABLE agent_events ADD COLUMN inflight_via TEXT")
            if "inflight_started_at" not in event_columns:
                conn.execute("ALTER TABLE agent_events ADD COLUMN inflight_started_at INTEGER")
            if "parked_at" not in event_columns:
                conn.execute("ALTER TABLE agent_events ADD COLUMN parked_at INTEGER")
            conn.execute(
                """
                UPDATE agent_events SET parked_at = updated_at
                WHERE outbox_status = 'parked' AND parked_at IS NULL
                """
            )
            conn.execute(
                """
                UPDATE agents SET enabled = 0, updated_at = ?
                WHERE agent_id = ? AND enabled != 0
                """,
                (int(time.time()), INSTALL_HEALTHCHECK_AGENT_ID),
            )
            for agent in conn.execute(
                """SELECT agent_id, name, owner, created_at FROM agents
                   WHERE agent_id NOT IN (SELECT agent_id FROM agent_profiles)"""
            ).fetchall():
                self._upsert_agent_profile_conn(
                    conn,
                    str(agent["agent_id"]),
                    _default_agent_profile(
                        str(agent["agent_id"]), str(agent["name"]), str(agent["owner"])
                    ),
                    int(agent["created_at"]),
                )

    def upsert_agent(
        self,
        agent_id: str,
        *,
        name: str,
        owner: str,
        enabled: bool,
        protocol_capabilities: list[str],
        profile: dict[str, Any] | None = None,
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
            if profile is not None:
                self._upsert_agent_profile_conn(conn, agent_id, profile, timestamp)
            elif conn.execute(
                "SELECT 1 FROM agent_profiles WHERE agent_id = ?", (agent_id,)
            ).fetchone() is None:
                self._upsert_agent_profile_conn(
                    conn,
                    agent_id,
                    _default_agent_profile(agent_id, name, owner),
                    timestamp,
                )
            return self._agent_conn(conn, agent_id)

    def upsert_agent_profile(
        self,
        agent_id: str,
        profile: dict[str, Any],
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        timestamp = _now(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_agent_conn(conn, agent_id)
            self._upsert_agent_profile_conn(conn, agent_id, profile, timestamp)
            return self.get_agent_profile(agent_id, conn=conn)

    def get_agent_profile(
        self,
        agent_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        if conn is None:
            with self.connect() as owned_conn:
                return self.get_agent_profile(agent_id, conn=owned_conn)
        row = conn.execute(
            """
            SELECT a.agent_id, a.name, a.owner, a.enabled,
                   a.protocol_capabilities_json, p.card_revision, p.profile_json
            FROM agents a
            JOIN agent_profiles p ON p.agent_id = a.agent_id
            WHERE a.agent_id = ?
            """,
            (agent_id,),
        ).fetchone()
        return self._agent_profile_dict(row) if row else None

    def list_agent_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.agent_id, a.name, a.owner, a.enabled,
                       a.protocol_capabilities_json, p.card_revision, p.profile_json
                FROM agents a
                JOIN agent_profiles p ON p.agent_id = a.agent_id
                ORDER BY a.agent_id
                """
            ).fetchall()
        return [self._agent_profile_dict(row) for row in rows]

    def register_listener(
        self,
        agent_id: str,
        *,
        listener_instance_id: str,
        client_version: str,
        workspace_version: str,
        transport: str,
        recover_if_stale: bool = False,
        now: int | None = None,
    ) -> dict[str, Any]:
        timestamp = _now(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_agent_conn(conn, agent_id)
            row = conn.execute(
                "SELECT readiness_epoch, observed_at FROM agent_listener_readiness WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if (
                recover_if_stale
                and row
                and int(row["observed_at"]) >= timestamp - LISTENER_READINESS_MAX_AGE_SECONDS
            ):
                raise ConflictError(
                    "listener_recovery_not_allowed",
                    code="listener_recovery_not_allowed",
                )
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
                    agent_id, PROTOCOL_V06, client_version, workspace_version,
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

    def issue_coordinator_grant(
        self,
        payload: dict[str, Any],
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        timestamp = _now(now)
        if int(payload["grant_expires_at"]) <= timestamp:
            raise ValueError("grant_expires_at must be in the future")
        if int(payload["grant_expires_at"]) <= int(payload["task_expires_at"]):
            raise ValueError("grant_expires_at must be later than task_expires_at")
        coordinator = str(payload["coordinator_agent_id"])
        claims = {
            key: payload[key]
            for key in (
                "protocol_version",
                "coordinator_agent_id",
                "investigation_id",
                "round_id",
                "approved_plan_digest",
                "authority_ref",
                "target_agent_ids",
                "task_count",
                "task_expires_at",
                "grant_expires_at",
                "operations",
            )
        }
        claims_digest = f"sha256:{_fingerprint(claims)}"
        token = secrets.token_urlsafe(32)
        token_hash = _coordinator_token_hash(token)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_admission_conn(conn, coordinator, timestamp)
            for target in payload["target_agent_ids"]:
                self._assert_admission_conn(conn, str(target), timestamp)
            existing = conn.execute(
                """
                SELECT * FROM coordinator_grants
                WHERE coordinator_agent_id = ? AND issuance_key = ?
                """,
                (coordinator, str(payload["issuance_key"])),
            ).fetchone()
            existing_round = conn.execute(
                """
                SELECT issuance_key FROM coordinator_grants
                WHERE coordinator_agent_id = ? AND investigation_id = ? AND round_id = ?
                """,
                (
                    coordinator,
                    str(payload["investigation_id"]),
                    str(payload["round_id"]),
                ),
            ).fetchone()
            if existing_round and not existing:
                raise ConflictError(
                    "Investigation Round already has a different coordinator grant issuance",
                    code="coordinator_grant_claim_mismatch",
                )
            if existing:
                if str(existing["claims_digest"]) != claims_digest:
                    raise ConflictError(
                        "coordinator grant issuance key was reused with different claims",
                        code="coordinator_grant_claim_mismatch",
                    )
                if str(existing["status"]) != "active":
                    raise ConflictError(
                        "revoked coordinator grant issuance cannot be reused",
                        code="coordinator_grant_revoked",
                    )
                token_version = int(existing["token_version"]) + 1
                grant_id = str(existing["grant_id"])
                conn.execute(
                    """
                    UPDATE coordinator_grants
                    SET token_hash = ?, token_version = ?, updated_at = ?
                    WHERE grant_id = ?
                    """,
                    (token_hash, token_version, timestamp, grant_id),
                )
                self._coordinator_grant_audit_conn(
                    conn,
                    grant_id,
                    "grant.token_rotated",
                    coordinator,
                    {"token_version": token_version, "claims_digest": claims_digest},
                    timestamp,
                )
            else:
                if int(payload["task_expires_at"]) <= timestamp:
                    raise ValueError("task_expires_at must be in the future")
                grant_id = f"cgrant_{uuid.uuid4().hex}"
                token_version = 1
                conn.execute(
                    """
                    INSERT INTO coordinator_grants (
                        grant_id, issuance_key, coordinator_agent_id,
                        investigation_id, round_id, approved_plan_digest,
                        authority_ref, task_count, task_expires_at,
                        grant_expires_at, operations_json, claims_digest,
                        token_hash, token_version, status, used_task_count,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?)
                    """,
                    (
                        grant_id,
                        str(payload["issuance_key"]),
                        coordinator,
                        str(payload["investigation_id"]),
                        str(payload["round_id"]),
                        str(payload["approved_plan_digest"]),
                        str(payload["authority_ref"]),
                        int(payload["task_count"]),
                        int(payload["task_expires_at"]),
                        int(payload["grant_expires_at"]),
                        json.dumps(payload["operations"], sort_keys=True),
                        claims_digest,
                        token_hash,
                        token_version,
                        timestamp,
                        timestamp,
                    ),
                )
                for target in payload["target_agent_ids"]:
                    conn.execute(
                        """
                        INSERT INTO coordinator_grant_targets (grant_id, target_agent_id)
                        VALUES (?, ?)
                        """,
                        (grant_id, str(target)),
                    )
                self._coordinator_grant_audit_conn(
                    conn,
                    grant_id,
                    "grant.issued",
                    coordinator,
                    {"token_version": token_version, "claims_digest": claims_digest},
                    timestamp,
                )
            grant = self._coordinator_grant_dict_conn(conn, grant_id)
            return {"grant": grant, "coordinator_grant_token": token}

    def resolve_coordinator_grant_task(
        self,
        grant_id: str,
        coordinator_grant_token: str,
        coordinator_agent_id: str,
        *,
        idempotency_key: str,
        work_item_id: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        timestamp = _now(now)
        with self.connect() as conn:
            grant = self._require_coordinator_grant_conn(
                conn,
                coordinator_grant_token,
                coordinator_agent_id,
                "read",
                timestamp,
                expected_grant_id=grant_id,
            )
            mapping = conn.execute(
                """
                SELECT * FROM coordinator_grant_tasks
                WHERE grant_id = ? AND idempotency_key = ?
                """,
                (grant["grant_id"], idempotency_key),
            ).fetchone()
            if not mapping:
                return {"grant": self._coordinator_grant_public(grant), "mapping": None}
            if work_item_id is not None and str(mapping["work_item_id"]) != work_item_id:
                raise ConflictError(
                    "coordinator grant mapping does not match work_item_id",
                    code="coordinator_grant_claim_mismatch",
                )
            return {
                "grant": self._coordinator_grant_public(grant),
                "mapping": self._coordinator_mapping_dict(mapping),
                "task": self._task_detail_conn(conn, str(mapping["task_id"])),
            }

    def is_coordinator_managed_task(self, task_id: str, coordinator_agent_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM coordinator_grant_tasks m
                JOIN coordinator_grants g ON g.grant_id = m.grant_id
                WHERE m.task_id = ? AND g.coordinator_agent_id = ?
                """,
                (task_id, coordinator_agent_id),
            ).fetchone()
            return row is not None

    def authorize_coordinator_task(
        self,
        task_id: str,
        coordinator_grant_token: str,
        coordinator_agent_id: str,
        operation: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            grant, _ = self._require_coordinator_task_conn(
                conn,
                task_id,
                coordinator_grant_token,
                coordinator_agent_id,
                operation,
                _now(now),
            )
            return self._coordinator_grant_public(grant)

    def create_task(
        self,
        payload: dict[str, Any],
        *,
        source_task_id: str | None = None,
        coordinator_grant_token: str | None = None,
        coordinator_agent_id: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        timestamp = _now(now)
        requester = str(payload["requester_agent_id"])
        target = str(payload["target_agent_id"])
        key = str(payload["idempotency_key"])
        request_hash = _fingerprint(payload)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            grant = None
            work_item_id = None
            if coordinator_grant_token is not None:
                if not coordinator_agent_id:
                    raise CoordinatorGrantPermissionError(
                        "coordinator identity is required",
                        code="coordinator_grant_identity_mismatch",
                    )
                if source_task_id:
                    raise CoordinatorGrantPermissionError(
                        "coordinator grant does not authorize follow-up Tasks",
                        code="coordinator_grant_operation_forbidden",
                    )
                grant = self._require_coordinator_grant_conn(
                    conn,
                    coordinator_grant_token,
                    coordinator_agent_id,
                    "create",
                    timestamp,
                )
                work_item_id = self._assert_coordinator_create_claims_conn(
                    conn, grant, payload
                )
                mapping = self._coordinator_mapping_for_create_conn(
                    conn,
                    str(grant["grant_id"]),
                    work_item_id,
                    key,
                    request_hash,
                )
                if mapping:
                    return self._task_detail_conn(conn, str(mapping["task_id"]))
                if int(grant["used_task_count"]) >= int(grant["task_count"]):
                    raise ConflictError(
                        "coordinator grant task quota exhausted",
                        code="coordinator_grant_quota_exhausted",
                    )
                scope = f"coordinator_grant:{grant['grant_id']}"
            else:
                scope = source_task_id or "__root__"
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
                    task_id, root_task_id, PROTOCOL_V06, requester, target,
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
                subject=str(message_payload["subject"]).strip() if message_payload.get("subject") else None,
                metadata=message_payload.get("metadata"),
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
                {
                    "status": "open",
                    "task_version": 1,
                    "message_subject_present": bool(message_payload.get("subject")),
                    "message_metadata_present": "metadata" in message_payload,
                    "correlation": _correlation_metadata(message_payload.get("metadata")),
                },
                timestamp,
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
            if grant is not None and work_item_id is not None:
                conn.execute(
                    """
                    INSERT INTO coordinator_grant_tasks (
                        grant_id, work_item_id, idempotency_key, request_hash,
                        task_id, target_agent_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        grant["grant_id"], work_item_id, key, request_hash,
                        task_id, target, timestamp,
                    ),
                )
                updated = conn.execute(
                    """
                    UPDATE coordinator_grants
                    SET used_task_count = used_task_count + 1, updated_at = ?
                    WHERE grant_id = ? AND used_task_count < task_count
                    """,
                    (timestamp, grant["grant_id"]),
                )
                if updated.rowcount != 1:
                    raise ConflictError(
                        "coordinator grant task quota exhausted",
                        code="coordinator_grant_quota_exhausted",
                    )
                self._coordinator_grant_audit_conn(
                    conn,
                    str(grant["grant_id"]),
                    "grant.task_created",
                    requester,
                    {
                        "task_id": task_id,
                        "work_item_id": work_item_id,
                        "target_agent_id": target,
                        "used_task_count": int(grant["used_task_count"]) + 1,
                    },
                    timestamp,
                )
            return self._task_detail_conn(conn, task_id)

    def create_install_healthcheck(
        self,
        requester_agent_id: str,
        *,
        idempotency_key: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        timestamp = _now(now)
        requester = str(requester_agent_id)
        key = str(idempotency_key)
        request_hash = _fingerprint({"requester_agent_id": requester})
        scope = "__install_healthcheck__"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._idempotent_result_conn(
                conn, "install_healthcheck", requester, scope, key, request_hash
            )
            if existing:
                return self._task_detail_conn(conn, existing)
            self._assert_admission_conn(conn, requester, timestamp)
            conn.execute(
                """
                INSERT INTO agents (
                    agent_id, name, owner, enabled, protocol_capabilities_json,
                    created_at, updated_at
                ) VALUES (?, ?, 'AgentRelay', 0, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    name = excluded.name,
                    enabled = 0,
                    protocol_capabilities_json = excluded.protocol_capabilities_json,
                    updated_at = excluded.updated_at
                """,
                (
                    INSTALL_HEALTHCHECK_AGENT_ID,
                    "AgentRelay Install Healthcheck",
                    json.dumps([PROTOCOL_V06]),
                    timestamp,
                    timestamp,
                ),
            )
            if conn.execute(
                "SELECT 1 FROM agent_profiles WHERE agent_id = ?",
                (INSTALL_HEALTHCHECK_AGENT_ID,),
            ).fetchone() is None:
                self._upsert_agent_profile_conn(
                    conn,
                    INSTALL_HEALTHCHECK_AGENT_ID,
                    _default_agent_profile(
                        INSTALL_HEALTHCHECK_AGENT_ID,
                        "AgentRelay Install Healthcheck",
                        "AgentRelay",
                    ),
                    timestamp,
                )

            task_id = f"task_{uuid.uuid4().hex}"
            request_message_id = f"msg_{uuid.uuid4().hex}"
            request_event_id = f"evt_{uuid.uuid4().hex}"
            ack_message_id = f"msg_{uuid.uuid4().hex}"
            ack_event_id = f"evt_{uuid.uuid4().hex}"
            expires_at = timestamp + INSTALL_HEALTHCHECK_TTL_SECONDS
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, root_task_id, protocol_version, requester_agent_id,
                    target_agent_id, done_criteria, status, turn_sequence,
                    current_message_id, from_agent_id, to_agent_id, task_version,
                    max_turns, task_expires_at, reason, terminal_by_agent_id,
                    completed_against_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', 1, ?, ?, ?, 3, 1, ?,
                    NULL, NULL, NULL, ?, ?)
                """,
                (
                    task_id,
                    task_id,
                    PROTOCOL_V06,
                    requester,
                    INSTALL_HEALTHCHECK_AGENT_ID,
                    json.dumps("Requester receives the synthetic ACK in the Local Inbox."),
                    ack_message_id,
                    INSTALL_HEALTHCHECK_AGENT_ID,
                    requester,
                    expires_at,
                    timestamp,
                    timestamp,
                ),
            )
            self._insert_message_conn(
                conn,
                message_id=request_message_id,
                task_id=task_id,
                turn_sequence=1,
                from_agent_id=requester,
                to_agent_id=INSTALL_HEALTHCHECK_AGENT_ID,
                subject="AgentRelay install loopback health check",
                parts=[{"kind": "text", "text": "Return the synthetic install ACK."}],
                idempotency_key=f"{key}:request",
                now=timestamp,
            )
            self._insert_pending_event_conn(
                conn,
                event_id=request_event_id,
                task_id=task_id,
                message_id=request_message_id,
                target_agent_id=INSTALL_HEALTHCHECK_AGENT_ID,
                turn_sequence=1,
                task_version=1,
                now=timestamp,
            )
            conn.execute(
                """
                UPDATE messages SET delivery_status = 'delivered', delivered_at = ?, updated_at = ?
                WHERE message_id = ?
                """,
                (timestamp, timestamp, request_message_id),
            )
            conn.execute(
                """
                UPDATE agent_events SET outbox_status = 'acked', acked_at = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (timestamp, timestamp, request_event_id),
            )
            ack_text = "\n".join(
                [
                    f"ACK from {INSTALL_HEALTHCHECK_AGENT_ID}",
                    f"requester={requester}",
                    f"task={task_id}",
                    "scope=agentrelay-install-loopback",
                ]
            )
            self._insert_message_conn(
                conn,
                message_id=ack_message_id,
                task_id=task_id,
                turn_sequence=1,
                from_agent_id=INSTALL_HEALTHCHECK_AGENT_ID,
                to_agent_id=requester,
                parts=[{"kind": "text", "text": ack_text}],
                idempotency_key=f"{key}:ack",
                now=timestamp,
            )
            self._insert_pending_event_conn(
                conn,
                event_id=ack_event_id,
                task_id=task_id,
                message_id=ack_message_id,
                target_agent_id=requester,
                turn_sequence=1,
                task_version=3,
                now=timestamp,
            )
            self._audit_conn(
                conn,
                task_id,
                "task.created",
                requester,
                request_message_id,
                {"status": "open", "task_version": 1, "install_healthcheck": True},
                timestamp,
            )
            self._audit_conn(
                conn,
                task_id,
                "message.delivery_changed",
                INSTALL_HEALTHCHECK_AGENT_ID,
                request_message_id,
                {"delivery_status": "delivered", "task_version": 2, "install_healthcheck": True},
                timestamp,
            )
            self._audit_conn(
                conn,
                task_id,
                "message.created",
                INSTALL_HEALTHCHECK_AGENT_ID,
                ack_message_id,
                {"turn_sequence": 1, "task_version": 3, "install_healthcheck": True},
                timestamp,
            )
            self._record_idempotency_conn(
                conn,
                "install_healthcheck",
                requester,
                scope,
                key,
                request_hash,
                task_id,
                ack_message_id,
                timestamp,
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
            self._expire_and_reject_if_due_conn(conn, task, timestamp)
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
        self.expire_tasks(now=timestamp)
        return self.delivery_control.run_claim(
            agent_id,
            lambda context: self._claim_due_event(context, timestamp),
            now=timestamp,
        )

    def _claim_due_event(
        self,
        context: DeliveryClaimContext,
        timestamp: int,
    ) -> dict[str, Any] | None:
        if not context.has_capacity():
            return None
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
                (context.agent_id, timestamp),
            ).fetchone()
            if not row:
                return None
            cursor = conn.execute(
                """
                UPDATE agent_events
                SET outbox_status = 'inflight', outbox_attempts = outbox_attempts + 1,
                    inflight_via = 'push', inflight_until = ?, inflight_started_at = ?,
                    next_retry_at = NULL,
                    updated_at = ?
                WHERE event_id = ? AND outbox_status = ? AND outbox_attempts < ?
                """,
                (
                    timestamp + DELIVERY_ACK_LEASE_SECONDS, timestamp, timestamp,
                    row["event_id"],
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
        listener_instance_id: str,
        readiness_epoch: int,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        timestamp = _now(now)
        self.expire_tasks(now=timestamp)
        return self.delivery_control.run_claim(
            agent_id,
            lambda context: self._recover_event(
                context,
                listener_instance_id,
                readiness_epoch,
                timestamp,
            ),
            now=timestamp,
        )

    def _recover_event(
        self,
        context: DeliveryClaimContext,
        listener_instance_id: str,
        readiness_epoch: int,
        timestamp: int,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_listener_epoch_conn(
                conn, context.agent_id, listener_instance_id, readiness_epoch
            )
            expired_rows = conn.execute(
                """
                SELECT * FROM agent_events
                WHERE agent_id = ? AND outbox_status = 'inflight'
                  AND inflight_until <= ?
                ORDER BY inflight_until, event_id
                """,
                (context.agent_id, timestamp),
            ).fetchall()
            for expired_row in expired_rows:
                self._record_attempt_failure_conn(
                    conn, dict(expired_row), "ack_lease_expired", timestamp
                )
            row = conn.execute(
                """
                SELECT * FROM agent_events
                WHERE agent_id = ? AND outbox_status = 'inflight'
                  AND inflight_until > ?
                ORDER BY can_transition_message DESC, created_at, event_id LIMIT 1
                """,
                (context.agent_id, timestamp),
            ).fetchone()
            if row:
                return self._event_dict(row)
            local_inflight = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM agent_events
                    WHERE agent_id = ? AND outbox_status = 'inflight'
                    """,
                    (context.agent_id,),
                ).fetchone()[0]
            )
            if not context.has_capacity_for_lane(self.db_path, local_inflight):
                return None
            row = conn.execute(
                """
                SELECT e.* FROM agent_events e
                JOIN tasks t ON t.task_id = e.task_id
                WHERE e.agent_id = ?
                  AND (e.can_transition_message = 0 OR t.status = 'open')
                  AND e.outbox_status IN ('parked', 'queued', 'retry_wait')
                ORDER BY e.can_transition_message DESC, e.created_at, e.event_id
                LIMIT 1
                """,
                (context.agent_id,),
            ).fetchone()
            if not row:
                return None
            cursor = conn.execute(
                """
                UPDATE agent_events
                SET outbox_status = 'inflight', recovery_attempts = recovery_attempts + 1,
                    inflight_via = 'recovery', inflight_until = ?, inflight_started_at = ?,
                    next_retry_at = NULL,
                    updated_at = ?
                WHERE event_id = ? AND outbox_status = ?
                """,
                (
                    timestamp + DELIVERY_ACK_LEASE_SECONDS,
                    timestamp,
                    timestamp,
                    row["event_id"],
                    row["outbox_status"],
                ),
            )
            if cursor.rowcount != 1:
                return None
            return self._event_dict(
                conn.execute(
                    "SELECT * FROM agent_events WHERE event_id = ?", (row["event_id"],)
                ).fetchone()
            )

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
        coordinator_grant_token: str | None = None,
        coordinator_agent_id: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        return self._terminal_task(
            task_id,
            payload,
            completed=True,
            coordinator_grant_token=coordinator_grant_token,
            coordinator_agent_id=coordinator_agent_id,
            now=now,
        )

    def fail_task(
        self,
        task_id: str,
        payload: dict[str, Any],
        *,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        return self._terminal_task(
            task_id,
            payload,
            completed=False,
            coordinator_grant_token=None,
            coordinator_agent_id=None,
            now=now,
        )

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

    def expire_task_if_due(self, task_id: str, *, now: int | None = None) -> bool:
        timestamp = _now(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = self._task_row_conn(conn, task_id)
            if (
                not task
                or task["status"] != "open"
                or int(task["task_expires_at"]) > timestamp
            ):
                return False
            self._expire_task_conn(conn, dict(task), timestamp)
            return True

    def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self._task_detail_conn(conn, task_id)

    def record_client_runtime_audit(
        self,
        task_id: str,
        actor_agent_id: str | None,
        metadata: dict[str, Any],
        *,
        now: int | None = None,
    ) -> None:
        timestamp = _now(now)
        with self.connect() as conn:
            if not self._task_row_conn(conn, task_id):
                return
            self._audit_conn(
                conn, task_id, "protocol.client_runtime", actor_agent_id, None,
                {"trust": "client_reported", **metadata}, timestamp,
            )

    def record_coordinator_compatibility_create(
        self,
        task_id: str,
        actor_agent_id: str,
        *,
        now: int | None = None,
    ) -> None:
        timestamp = _now(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._task_row_conn(conn, task_id):
                raise ValueError("task not found")
            self._audit_conn(
                conn,
                task_id,
                "coordinator.compatibility_create",
                actor_agent_id,
                None,
                {"compatibility_mode": True},
                timestamp,
            )

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
                "protocol_version": PROTOCOL_V06,
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
            exhausted_transitionable_events = conn.execute(
                """
                SELECT COUNT(*) FROM agent_events
                WHERE outbox_status = 'exhausted' AND can_transition_message = 1
                """
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
            ("exhausted_outbox", int(exhausted_transitionable_events)),
            ("stale_enabled_agent", int(stale_readiness)),
        ):
            if count:
                alerts.append({"code": code, "count": count})
        return {
            "protocol_version": PROTOCOL_V06,
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
                "unacked": sum(outbox_status.get(key, 0) for key in ("queued", "inflight", "retry_wait", "parked")),
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
                          AND e.outbox_status IN ('queued', 'inflight', 'retry_wait', 'parked')) AS pending_event_count
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

    def list_agents(self, *, now: int | None = None) -> list[dict[str, Any]]:
        timestamp = _now(now)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.agent_id, a.enabled, a.protocol_capabilities_json,
                       p.card_revision, r.protocol_version AS readiness_protocol_version,
                       r.workspace_version, r.listener_instance_id, r.readiness_epoch,
                       r.transport, r.ready, r.observed_at,
                       (SELECT COUNT(*) FROM tasks t
                        WHERE t.status = 'open'
                          AND t.task_expires_at > ?
                          AND (t.requester_agent_id = a.agent_id OR t.target_agent_id = a.agent_id)) AS active_task_count
                FROM agents a
                JOIN agent_profiles p ON p.agent_id = a.agent_id
                LEFT JOIN agent_listener_readiness r ON r.agent_id = a.agent_id
                ORDER BY a.agent_id
                """,
                (timestamp,),
            ).fetchall()
        agents = []
        for row in rows:
            observed_at = int(row["observed_at"]) if row["observed_at"] is not None else None
            ready = bool(row["ready"]) if row["ready"] is not None else False
            agents.append(
                {
                    "agent_id": str(row["agent_id"]),
                    "enabled": bool(row["enabled"]),
                    "protocol_capabilities": json.loads(row["protocol_capabilities_json"]),
                    "card_revision": int(row["card_revision"]),
                    "card_ref": f"/agentrelay/api/agents/{row['agent_id']}/card",
                    "ready": ready,
                    "readiness_fresh": bool(
                        ready
                        and observed_at is not None
                        and observed_at >= timestamp - LISTENER_READINESS_MAX_AGE_SECONDS
                    ),
                    "readiness_protocol_version": row["readiness_protocol_version"],
                    "workspace_version": row["workspace_version"],
                    "listener_instance_id": row["listener_instance_id"],
                    "readiness_epoch": (
                        int(row["readiness_epoch"])
                        if row["readiness_epoch"] is not None
                        else None
                    ),
                    "transport": row["transport"],
                    "observed_at": observed_at,
                    "active_task_count": int(row["active_task_count"]),
                }
            )
        return agents

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
                conn, agent_id, str(payload["listener_instance_id"]), int(payload["readiness_epoch"])
            )
            task = self._task_row_conn(conn, task_id)
            if not task:
                return None
            self._expire_and_reject_if_due_conn(conn, task, timestamp)
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
            if event["outbox_status"] not in {"inflight", "retry_wait", "parked"}:
                raise ConflictError("delivery Event is not ACK/NACK eligible")
            if message["delivery_status"] != "pending":
                raise ConflictError("current Message is not pending")

            if failed:
                reason = "listener_persistence_failed"
                conn.execute(
                    """
                    UPDATE agent_events SET outbox_status = 'parked', inflight_until = NULL,
                        inflight_via = NULL, next_retry_at = NULL, exhausted_at = NULL,
                        exhaustion_reason = NULL,
                        parked_at = COALESCE(parked_at, ?),
                        last_error = ?, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (timestamp, reason, timestamp, event["event_id"]),
                )
                audit_type = "message.delivery_parked"
                result_version = int(task["task_version"])
            else:
                next_version = int(task["task_version"]) + 1
                conn.execute(
                    """
                    UPDATE agent_events SET outbox_status = 'acked', inflight_until = NULL,
                        inflight_via = NULL, next_retry_at = NULL, acked_at = ?, updated_at = ?
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
                result_version = next_version
            self._audit_conn(
                conn, task_id, audit_type, agent_id, message["message_id"],
                {"task_version": result_version, "reason": reason if failed else None}, timestamp,
            )
            delivery_status = "pending" if failed else "delivered"
            if message["from_agent_id"] != INSTALL_HEALTHCHECK_AGENT_ID:
                self._insert_info_event_conn(
                    conn,
                    agent_id=message["from_agent_id"],
                    event_type="message.delivery_changed",
                    task_id=task_id,
                    message_id=message["message_id"],
                    payload={
                        "delivery_status": delivery_status,
                        "task_version": result_version,
                        "diagnosis": "waiting_listener" if failed else None,
                    },
                    idempotency_key=f"v06:{message['message_id']}:delivery:{delivery_status}:{result_version}",
                    now=timestamp,
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
        coordinator_grant_token: str | None,
        coordinator_agent_id: str | None,
        now: int | None,
    ) -> dict[str, Any] | None:
        timestamp = _now(now)
        actor = str(payload["actor_agent_id"])
        operation = "complete" if completed else "fail"
        key = str(payload["idempotency_key"])
        request_hash = _fingerprint(payload)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if coordinator_grant_token is not None:
                if not completed or not coordinator_agent_id:
                    raise CoordinatorGrantPermissionError(
                        "coordinator grant operation is forbidden",
                        code="coordinator_grant_operation_forbidden",
                    )
                self._require_coordinator_task_conn(
                    conn,
                    task_id,
                    coordinator_grant_token,
                    coordinator_agent_id,
                    "complete-own",
                    timestamp,
                )
            existing = self._idempotent_result_conn(
                conn, operation, actor, task_id, key, request_hash
            )
            if existing:
                return self._task_detail_conn(conn, existing)
            task = self._task_row_conn(conn, task_id)
            if not task:
                return None
            self._expire_and_reject_if_due_conn(conn, task, timestamp)
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
                if event["outbox_status"] in {"queued", "inflight", "retry_wait", "parked"}:
                    conn.execute(
                        """
                        UPDATE agent_events SET outbox_status = 'exhausted', inflight_until = NULL,
                            inflight_via = NULL, next_retry_at = NULL,
                            exhausted_at = ?, exhaustion_reason = 'task_failed',
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
        if event.get("inflight_via") == "recovery":
            conn.execute(
                """
                UPDATE agent_events SET outbox_status = 'parked', inflight_until = NULL,
                    inflight_via = NULL, next_retry_at = NULL,
                    parked_at = COALESCE(parked_at, ?), last_error = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (now, error, now, event["event_id"]),
            )
            return
        conn.execute(
            """
            UPDATE agent_events SET outbox_status = 'parked', inflight_until = NULL,
                inflight_via = NULL, next_retry_at = NULL, exhausted_at = NULL,
                exhaustion_reason = NULL, parked_at = COALESCE(parked_at, ?),
                last_error = ?, updated_at = ? WHERE event_id = ?
            """,
            (now, error, now, event["event_id"]),
        )
        if event["can_transition_message"]:
            message = self._message_row_conn(conn, event["message_id"])
            task = self._task_row_conn(conn, event["task_id"])
            if task and message:
                self._audit_conn(
                    conn, task["task_id"], "message.delivery_parked", None,
                    message["message_id"],
                    {"attempt": event["outbox_attempts"], "last_error": error}, now,
                )
            if task and message and message["from_agent_id"] != INSTALL_HEALTHCHECK_AGENT_ID:
                self._insert_info_event_conn(
                    conn,
                    agent_id=message["from_agent_id"],
                    event_type="message.delivery_waiting",
                    task_id=task["task_id"],
                    message_id=message["message_id"],
                    payload={
                        "delivery_status": "pending",
                        "diagnosis": "waiting_listener",
                        "task_version": task["task_version"],
                    },
                    idempotency_key=f"v06:{message['message_id']}:waiting-listener",
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
        if event and event["outbox_status"] in {"queued", "inflight", "retry_wait", "parked"}:
            conn.execute(
                """
                UPDATE agent_events SET outbox_status = 'exhausted', inflight_until = NULL,
                    inflight_via = NULL,
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

    def _expire_and_reject_if_due_conn(
        self,
        conn: sqlite3.Connection,
        task: sqlite3.Row | dict[str, Any],
        now: int,
    ) -> None:
        if task["status"] == "open" and int(task["task_expires_at"]) <= now:
            self._expire_task_conn(conn, dict(task), now)
            expired = self._task_row_conn(conn, str(task["task_id"]))
            conn.commit()
            raise ConflictError(
                "task is terminal: expired",
                code="task_expired",
                current_task=self._task_dict(expired),
            )

    def _assert_failure_authority(
        self,
        task: sqlite3.Row | dict[str, Any],
        message: sqlite3.Row | dict[str, Any],
        actor: str,
        reason: str,
    ) -> None:
        if reason in {"relay_persistence_failed", "internal_consistency_error"}:
            if actor != "relay":
                raise ConflictError(f"{reason} may only be recorded by Relay")
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
        if not agent["enabled"] or PROTOCOL_V06 not in capabilities:
            raise ConflictError("protocol_v06_required", code="protocol_v06_required")

    def _listener_ready_conn(
        self, conn: sqlite3.Connection, agent_id: str, now: int
    ) -> bool:
        readiness = conn.execute(
            "SELECT * FROM agent_listener_readiness WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        return bool(
            readiness
            and readiness["protocol_version"] == PROTOCOL_V06
            and readiness["ready"]
            and int(readiness["observed_at"]) >= now - LISTENER_READINESS_MAX_AGE_SECONDS
        )

    def _assert_backlog_capacity_conn(
        self, conn: sqlite3.Connection, agent_id: str
    ) -> None:
        unacked = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM agent_events
                WHERE agent_id = ?
                  AND outbox_status IN ('queued', 'inflight', 'retry_wait', 'parked')
                """,
                (agent_id,),
            ).fetchone()[0]
        )
        if unacked >= MAX_AGENT_UNACKED_EVENTS:
            raise ConflictError("agent_backlog_full", code="agent_backlog_full")

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
        subject: str | None = None,
        metadata: dict[str, Any] | None = None,
        parts: list[dict[str, Any]],
        idempotency_key: str,
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO messages (
                message_id, task_id, turn_sequence, from_agent_id, to_agent_id,
                subject, metadata_json, parts_json, idempotency_key, delivery_status, max_delivery_attempts,
                delivered_at, failed_at, delivery_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, NULL, ?, ?)
            """,
            (
                message_id, task_id, turn_sequence, from_agent_id, to_agent_id,
                subject, json.dumps(metadata, sort_keys=True) if metadata is not None else None,
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
        self._assert_backlog_capacity_conn(conn, target_agent_id)
        initial_status = (
            "queued" if self._listener_ready_conn(conn, target_agent_id, now) else "parked"
        )
        conn.execute(
            """
            INSERT INTO agent_events (
                event_id, agent_id, event_type, task_id, message_id, payload_json,
                idempotency_key, outbox_status, outbox_attempts, inflight_until,
                recovery_attempts, inflight_via,
                next_retry_at, acked_at, exhausted_at, exhaustion_reason, last_error,
                can_transition_message, created_at, updated_at
            ) VALUES (?, ?, 'message.pending', ?, ?, ?, ?, ?, 0, NULL,
                0, NULL, NULL, NULL, NULL, NULL, NULL, 1, ?, ?)
            """,
            (
                event_id, target_agent_id, task_id, message_id,
                json.dumps(
                    {
                        "protocol_version": PROTOCOL_V06,
                        "task_id": task_id,
                        "message_id": message_id,
                        "turn_sequence": turn_sequence,
                        "task_version": task_version,
                    },
                    sort_keys=True,
                ),
                f"v06:{message_id}:pending", initial_status, now, now,
            ),
        )
        if initial_status == "parked":
            conn.execute(
                "UPDATE agent_events SET parked_at = ? WHERE event_id = ?",
                (now, event_id),
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
        initial_status = "queued" if self._listener_ready_conn(conn, agent_id, now) else "parked"
        conn.execute(
            """
            INSERT INTO agent_events (
                event_id, agent_id, event_type, task_id, message_id, payload_json,
                idempotency_key, outbox_status, outbox_attempts, inflight_until,
                recovery_attempts, inflight_via,
                next_retry_at, acked_at, exhausted_at, exhaustion_reason, last_error,
                can_transition_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 0, NULL, NULL, NULL, NULL,
                NULL, NULL, 0, ?, ?)
            """,
            (
                f"evt_{uuid.uuid4().hex}", agent_id, event_type, task_id, message_id,
                json.dumps({"protocol_version": PROTOCOL_V06, **payload}, sort_keys=True),
                idempotency_key, initial_status, now, now,
            ),
        )
        if initial_status == "parked":
            conn.execute(
                """
                UPDATE agent_events SET parked_at = ?
                WHERE agent_id = ? AND idempotency_key = ?
                """,
                (now, agent_id, idempotency_key),
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
        recipients.discard(INSTALL_HEALTHCHECK_AGENT_ID)
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
                idempotency_key=f"v06:{task['task_id']}:status:{task_version}:{recipient}",
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

    def _require_coordinator_grant_conn(
        self,
        conn: sqlite3.Connection,
        token: str,
        coordinator_agent_id: str,
        operation: str,
        now: int,
        *,
        expected_grant_id: str | None = None,
    ) -> sqlite3.Row:
        if operation not in COORDINATOR_GRANT_OPERATIONS:
            raise CoordinatorGrantPermissionError(
                "coordinator grant operation is forbidden",
                code="coordinator_grant_operation_forbidden",
            )
        token_hash = _coordinator_token_hash(token)
        row = conn.execute(
            "SELECT * FROM coordinator_grants WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not row or not hmac.compare_digest(str(row["token_hash"]), token_hash):
            raise CoordinatorGrantPermissionError(
                "invalid coordinator grant",
                code="invalid_coordinator_grant",
            )
        if expected_grant_id and not hmac.compare_digest(
            str(row["grant_id"]), expected_grant_id
        ):
            raise CoordinatorGrantPermissionError(
                "coordinator grant does not match requested grant",
                code="invalid_coordinator_grant",
            )
        if str(row["status"]) != "active":
            raise CoordinatorGrantPermissionError(
                "coordinator grant is revoked",
                code="coordinator_grant_revoked",
            )
        if int(row["grant_expires_at"]) <= now:
            raise CoordinatorGrantPermissionError(
                "coordinator grant is expired",
                code="coordinator_grant_expired",
            )
        if not hmac.compare_digest(
            str(row["coordinator_agent_id"]), coordinator_agent_id
        ):
            raise CoordinatorGrantPermissionError(
                "coordinator grant identity mismatch",
                code="coordinator_grant_identity_mismatch",
            )
        operations = set(json.loads(str(row["operations_json"])))
        if operation not in operations:
            raise CoordinatorGrantPermissionError(
                "coordinator grant operation is forbidden",
                code="coordinator_grant_operation_forbidden",
            )
        return row

    def _require_coordinator_task_conn(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        token: str,
        coordinator_agent_id: str,
        operation: str,
        now: int,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        grant = self._require_coordinator_grant_conn(
            conn, token, coordinator_agent_id, operation, now
        )
        mapping = conn.execute(
            """
            SELECT * FROM coordinator_grant_tasks
            WHERE grant_id = ? AND task_id = ?
            """,
            (grant["grant_id"], task_id),
        ).fetchone()
        if not mapping:
            raise ConflictError(
                "Task is not owned by this coordinator grant",
                code="coordinator_grant_task_not_owned",
            )
        task = self._task_row_conn(conn, task_id)
        if not task or str(task["requester_agent_id"]) != coordinator_agent_id:
            raise ConflictError(
                "Task is not owned by the coordinator identity",
                code="coordinator_grant_task_not_owned",
            )
        return grant, mapping

    def _assert_coordinator_create_claims_conn(
        self,
        conn: sqlite3.Connection,
        grant: sqlite3.Row,
        payload: dict[str, Any],
    ) -> str:
        if str(payload["requester_agent_id"]) != str(grant["coordinator_agent_id"]):
            raise ConflictError(
                "Task requester does not match coordinator grant",
                code="coordinator_grant_claim_mismatch",
            )
        target = str(payload["target_agent_id"])
        allowed_target = conn.execute(
            """
            SELECT 1 FROM coordinator_grant_targets
            WHERE grant_id = ? AND target_agent_id = ?
            """,
            (grant["grant_id"], target),
        ).fetchone()
        if not allowed_target:
            raise ConflictError(
                "Task target is outside coordinator grant target set",
                code="coordinator_grant_claim_mismatch",
            )
        if int(payload.get("max_turns") or 0) != 1:
            raise ConflictError(
                "Coordinator Task max_turns must be 1",
                code="coordinator_grant_claim_mismatch",
            )
        if int(payload.get("task_expires_at") or 0) != int(grant["task_expires_at"]):
            raise ConflictError(
                "Coordinator Task deadline must match grant deadline",
                code="coordinator_grant_claim_mismatch",
            )
        metadata = payload.get("message", {}).get("metadata")
        if not isinstance(metadata, dict):
            raise ConflictError(
                "Coordinator Task requires correlation metadata",
                code="coordinator_grant_claim_mismatch",
            )
        expected = {
            "investigation_id": str(grant["investigation_id"]),
            "round_id": str(grant["round_id"]),
            "approved_plan_digest": str(grant["approved_plan_digest"]),
        }
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise ConflictError(
                "Coordinator Task correlation does not match grant claims",
                code="coordinator_grant_claim_mismatch",
            )
        work_item_id = metadata.get("work_item_id")
        if not isinstance(work_item_id, str) or not work_item_id.strip():
            raise ConflictError(
                "Coordinator Task requires work_item_id",
                code="coordinator_grant_claim_mismatch",
            )
        return work_item_id.strip()

    def _coordinator_mapping_for_create_conn(
        self,
        conn: sqlite3.Connection,
        grant_id: str,
        work_item_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> sqlite3.Row | None:
        row = conn.execute(
            """
            SELECT * FROM coordinator_grant_tasks
            WHERE grant_id = ? AND (work_item_id = ? OR idempotency_key = ?)
            """,
            (grant_id, work_item_id, idempotency_key),
        ).fetchone()
        if not row:
            return None
        if (
            str(row["work_item_id"]) != work_item_id
            or str(row["idempotency_key"]) != idempotency_key
            or str(row["request_hash"]) != request_hash
        ):
            raise ConflictError(
                "coordinator grant mapping was reused with different Task claims",
                code="coordinator_grant_claim_mismatch",
            )
        return row

    def _coordinator_grant_dict_conn(
        self, conn: sqlite3.Connection, grant_id: str
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM coordinator_grants WHERE grant_id = ?", (grant_id,)
        ).fetchone()
        if not row:
            raise ValueError("coordinator grant not found")
        result = self._coordinator_grant_public(row)
        result["target_agent_ids"] = [
            str(item["target_agent_id"])
            for item in conn.execute(
                """
                SELECT target_agent_id FROM coordinator_grant_targets
                WHERE grant_id = ? ORDER BY target_agent_id
                """,
                (grant_id,),
            ).fetchall()
        ]
        return result

    @staticmethod
    def _coordinator_grant_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "grant_id": str(row["grant_id"]),
            "coordinator_agent_id": str(row["coordinator_agent_id"]),
            "investigation_id": str(row["investigation_id"]),
            "round_id": str(row["round_id"]),
            "approved_plan_digest": str(row["approved_plan_digest"]),
            "authority_ref": str(row["authority_ref"]),
            "task_count": int(row["task_count"]),
            "used_task_count": int(row["used_task_count"]),
            "task_expires_at": int(row["task_expires_at"]),
            "grant_expires_at": int(row["grant_expires_at"]),
            "operations": json.loads(str(row["operations_json"])),
            "claims_digest": str(row["claims_digest"]),
            "token_version": int(row["token_version"]),
            "status": str(row["status"]),
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
        }

    @staticmethod
    def _coordinator_mapping_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "grant_id": str(row["grant_id"]),
            "work_item_id": str(row["work_item_id"]),
            "idempotency_key": str(row["idempotency_key"]),
            "task_id": str(row["task_id"]),
            "target_agent_id": str(row["target_agent_id"]),
            "created_at": int(row["created_at"]),
        }

    def _coordinator_grant_audit_conn(
        self,
        conn: sqlite3.Connection,
        grant_id: str,
        event_type: str,
        actor_agent_id: str,
        payload: dict[str, Any],
        created_at: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO coordinator_grant_audit (
                audit_id, grant_id, event_type, actor_agent_id,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"cgaudit_{uuid.uuid4().hex}",
                grant_id,
                event_type,
                actor_agent_id,
                json.dumps(payload, sort_keys=True),
                created_at,
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

    def _upsert_agent_profile_conn(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        profile: dict[str, Any],
        now: int,
    ) -> None:
        normalized = _normalize_agent_profile(profile)
        encoded = json.dumps(normalized, sort_keys=True)
        existing = conn.execute(
            "SELECT card_revision, profile_json, created_at FROM agent_profiles WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if existing and str(existing["profile_json"]) == encoded:
            return
        revision = int(existing["card_revision"]) + 1 if existing else 1
        created_at = int(existing["created_at"]) if existing else now
        conn.execute(
            """
            INSERT INTO agent_profiles (
                agent_id, card_revision, profile_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                card_revision = excluded.card_revision,
                profile_json = excluded.profile_json,
                updated_at = excluded.updated_at
            """,
            (agent_id, revision, encoded, created_at, now),
        )

    @staticmethod
    def _agent_profile_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        profile = json.loads(value.pop("profile_json"))
        value["enabled"] = bool(value["enabled"])
        value["protocol_capabilities"] = json.loads(
            value.pop("protocol_capabilities_json")
        )
        return {**value, **profile}

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
        metadata_json = value.pop("metadata_json", None)
        value["metadata"] = json.loads(metadata_json) if metadata_json is not None else None
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
            "parked": "waiting_listener",
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


def _coordinator_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _correlation_metadata(metadata: Any) -> dict[str, str]:
    if not isinstance(metadata, dict):
        return {}
    return {
        key: str(metadata[key])
        for key in ("investigation_id", "round_id", "work_item_id")
        if isinstance(metadata.get(key), str) and metadata[key]
    }


def _default_agent_profile(agent_id: str, name: str, owner: str) -> dict[str, Any]:
    is_service = agent_id in {INSTALL_HEALTHCHECK_AGENT_ID, "project-hermes"}
    role = "service_agent" if is_service else "personal_agent"
    return {
        "description": f"{'Service' if is_service else 'Personal'} agent for {owner}.",
        "agent_role": role,
        "execution_mode": "autonomous" if is_service else "notify_only",
        "skills": [
            {
                "id": "service-collaboration" if is_service else "general-collaboration",
                "name": "Service collaboration" if is_service else "General collaboration",
                "description": f"Handle bounded AgentRelay work for {name}.",
            }
        ],
        "accepted_task_types": ["agent.task"],
        "input_modes": ["application/json", "text/plain"],
        "output_modes": ["application/json", "text/plain"],
        "data_boundaries": ["Only data available in the Agent's governed environment."],
        "permission_boundaries": ["No authority beyond the Agent's configured local policy."],
        "capabilities": (
            ["task_claim", "task_execute", "artifact_submit", "task_complete_owned"]
            if agent_id == "project-hermes"
            else (["task_claim", "task_execute", "artifact_submit"] if is_service else ["task_create", "task_review", "task_complete_owned"])
        ),
        "policy": {
            "autonomous_execution_allowed": is_service,
            "can_amend_goal": False if is_service else True,
            "can_close_owned_task": agent_id == "project-hermes" or not is_service,
            "high_impact_requires_approval": True,
            "secret_safe_push_only": True,
        },
    }


def _normalize_agent_profile(profile: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValueError("agent profile must be an object")
    required_strings = ("description", "agent_role", "execution_mode")
    normalized = dict(profile)
    for field in required_strings:
        value = normalized.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"agent profile {field} must be a non-empty string")
        normalized[field] = value.strip()
    if normalized["agent_role"] not in {"personal_agent", "service_agent"}:
        raise ValueError("agent profile agent_role is invalid")
    if normalized["execution_mode"] not in {"notify_only", "manual", "semi_auto", "autonomous"}:
        raise ValueError("agent profile execution_mode is invalid")
    for field in (
        "skills",
        "accepted_task_types",
        "input_modes",
        "output_modes",
        "data_boundaries",
        "permission_boundaries",
        "capabilities",
    ):
        value = normalized.get(field)
        if not isinstance(value, list) or not value:
            raise ValueError(f"agent profile {field} must be a non-empty array")
    if any(
        not isinstance(item, str) or not item.strip()
        for field in (
            "accepted_task_types",
            "input_modes",
            "output_modes",
            "data_boundaries",
            "permission_boundaries",
            "capabilities",
        )
        for item in normalized[field]
    ):
        raise ValueError("agent profile string arrays must contain non-empty strings")
    for skill in normalized["skills"]:
        if not isinstance(skill, dict) or any(
            not isinstance(skill.get(field), str) or not skill[field].strip()
            for field in ("id", "name", "description")
        ):
            raise ValueError("agent profile skills require id, name, and description")
    if not isinstance(normalized.get("policy"), dict):
        raise ValueError("agent profile policy must be an object")
    allowed = {
        "description", "agent_role", "execution_mode", "skills",
        "accepted_task_types", "input_modes", "output_modes", "data_boundaries",
        "permission_boundaries", "capabilities", "policy",
    }
    unknown = sorted(set(normalized) - allowed)
    if unknown:
        raise ValueError(f"unknown agent profile fields: {unknown}")
    return normalized


def _now(value: int | None) -> int:
    return int(time.time()) if value is None else int(value)
