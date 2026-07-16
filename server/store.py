from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from server.protocol_v03 import (
    PROTOCOL_V03,
    PREVIOUS_GOAL_DISPOSITIONS,
    normalize_completion_authority,
    normalize_human_authority,
    normalize_source_refs,
)
from server.protocol_v04 import FAILED_REASONS, PROTOCOL_V04
from server.timeline import build_timeline_entry
from server.transitions import (
    CLAIMABLE_STATES,
    TERMINAL_STATES,
    TransitionError,
    assert_artifact_allowed,
    assert_claim_allowed,
    assert_close_allowed,
    assert_delivery_allowed,
    assert_known_status,
    assert_max_turns,
    assert_update_status_allowed,
    is_exhausted_for_pending_agent,
    next_turn_count,
)


PROTOCOL_VERSION = PROTOCOL_V03
AGENT_EVENT_DELIVERY_STATES = {"pending", "inflight", "done", "failed"}
HEALTHCHECK_AGENT_ID = "agentrelay-healthcheck"
HEALTHCHECK_TTL_SECONDS = 30 * 60
DEFAULT_TASK_TTL_SECONDS = 24 * 60 * 60
AGENT_ROLES = {"personal_agent", "service_agent"}
EXECUTION_MODES = {"notify_only", "manual", "semi_auto", "autonomous"}
DEFAULT_PERSONAL_CAPABILITIES = [
    "task_create",
    "task_review",
    "task_close",
    "task_amend_with_human_authority",
]
DEFAULT_SERVICE_CAPABILITIES = [
    "task_claim",
    "task_execute",
    "artifact_submit",
    "clarification_request",
]
DEFAULT_PERSONAL_POLICY = {
    "autonomous_execution_allowed": False,
    "can_amend_goal": True,
    "can_close_owned_task": True,
    "requires_human_authority_for_amend": True,
    "requires_human_authority_for_close": True,
    "high_impact_requires_approval": True,
    "secret_safe_push_only": True,
}
DEFAULT_SERVICE_POLICY = {
    "autonomous_execution_allowed": True,
    "can_amend_goal": False,
    "can_close_owned_task": False,
    "requires_human_authority_for_amend": False,
    "requires_human_authority_for_close": False,
    "high_impact_requires_approval": True,
    "secret_safe_push_only": True,
}
MAX_TURNS_TERMINAL_REASON = (
    "Task exceeded max_turns before the pending agent could make another legal handoff."
)


class ConflictError(Exception):
    """Raised when a task exists but cannot perform the requested state transition."""

    def __init__(self, message: str, *, code: str = "CONFLICT", current_task: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.current_task = current_task


def assert_legacy_mutation_allowed(task: dict[str, Any], operation: str) -> None:
    if task.get("protocol_version") == PROTOCOL_V04:
        raise ConflictError(f"{operation} is not available for Protocol v0.4 tasks")


class Store:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
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
                    description TEXT NOT NULL,
                    agent_role TEXT NOT NULL DEFAULT 'personal_agent',
                    execution_mode TEXT NOT NULL DEFAULT 'notify_only',
                    capabilities_json TEXT NOT NULL DEFAULT '',
                    policy_json TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    context_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requester_agent_id TEXT NOT NULL,
                    target_agent_id TEXT NOT NULL,
                    requester_thread_id TEXT,
                    target_thread_id TEXT,
                    requester_thread_policy TEXT NOT NULL DEFAULT 'reuse-origin-thread',
                    target_thread_policy TEXT NOT NULL DEFAULT 'reuse-task-thread',
                    done_criteria TEXT NOT NULL DEFAULT '',
                    completion_owner_agent_id TEXT NOT NULL DEFAULT '',
                    pending_on_agent_id TEXT,
                    pending_on_human_id TEXT,
                    next_action TEXT,
                    terminal_reason TEXT,
                    parent_task_id TEXT,
                    delivery_status TEXT NOT NULL DEFAULT '',
                    delivered_to_thread_id TEXT,
                    delivered_at INTEGER,
                    delivery_error TEXT,
                    subject TEXT NOT NULL,
                    ttl INTEGER,
                    max_turns INTEGER NOT NULL DEFAULT 12,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    goal_version INTEGER NOT NULL DEFAULT 1,
                    exchange_epoch INTEGER NOT NULL DEFAULT 1,
                    claimed_by TEXT,
                    claimed_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_target_status
                    ON tasks (target_agent_id, status, created_at);

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    context_id TEXT NOT NULL,
                    from_agent_id TEXT NOT NULL,
                    to_agent_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    parts_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    from_agent_id TEXT NOT NULL,
                    to_agent_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    parts_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    event_sequence INTEGER,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE TABLE IF NOT EXISTS agent_events (
                    event_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    idempotency_key TEXT,
                    delivery_state TEXT NOT NULL DEFAULT 'pending',
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    inflight_until INTEGER,
                    done_at INTEGER,
                    failed_at INTEGER,
                    last_error TEXT,
                    acked_at INTEGER,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (agent_id) REFERENCES agents(agent_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_agent_events_agent_created
                    ON agent_events (agent_id, created_at, event_id);

                CREATE INDEX IF NOT EXISTS idx_agent_events_agent_acked
                    ON agent_events (agent_id, acked_at, created_at);

                CREATE TABLE IF NOT EXISTS task_thread_bindings (
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    thread_role TEXT NOT NULL DEFAULT 'agent_inbox',
                    thread_id TEXT NOT NULL,
                    project_path TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (task_id, agent_id, thread_role),
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id),
                    FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
                );

                CREATE INDEX IF NOT EXISTS idx_task_thread_bindings_agent
                    ON task_thread_bindings (agent_id, updated_at);
                """
            )
            self.ensure_agent_columns(conn)
            self.ensure_task_columns(conn)
            self.ensure_message_columns(conn)
            self.ensure_task_event_columns(conn)
            self.ensure_agent_event_columns(conn)
            self.ensure_v04_storage(conn)
            self.ensure_seed_agents(conn)

    def ensure_agent_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(agents)").fetchall()
        }
        migrations = {
            "agent_role": "ALTER TABLE agents ADD COLUMN agent_role TEXT NOT NULL DEFAULT 'personal_agent'",
            "execution_mode": "ALTER TABLE agents ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'notify_only'",
            "capabilities_json": "ALTER TABLE agents ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT ''",
            "policy_json": "ALTER TABLE agents ADD COLUMN policy_json TEXT NOT NULL DEFAULT ''",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)
        conn.execute(
            """
            UPDATE agents
            SET agent_role = 'service_agent',
                execution_mode = 'autonomous',
                capabilities_json = ?,
                policy_json = ?
            WHERE agent_id IN ('project-hermes', ?)
              AND agent_role = 'personal_agent'
            """,
            (
                json.dumps(DEFAULT_SERVICE_CAPABILITIES),
                json.dumps(DEFAULT_SERVICE_POLICY),
                HEALTHCHECK_AGENT_ID,
            ),
        )

    def ensure_task_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        migrations = {
            "done_criteria": "ALTER TABLE tasks ADD COLUMN done_criteria TEXT NOT NULL DEFAULT ''",
            "completion_owner_agent_id": "ALTER TABLE tasks ADD COLUMN completion_owner_agent_id TEXT NOT NULL DEFAULT ''",
            "pending_on_agent_id": "ALTER TABLE tasks ADD COLUMN pending_on_agent_id TEXT",
            "pending_on_human_id": "ALTER TABLE tasks ADD COLUMN pending_on_human_id TEXT",
            "next_action": "ALTER TABLE tasks ADD COLUMN next_action TEXT",
            "terminal_reason": "ALTER TABLE tasks ADD COLUMN terminal_reason TEXT",
            "parent_task_id": "ALTER TABLE tasks ADD COLUMN parent_task_id TEXT",
            "delivery_status": "ALTER TABLE tasks ADD COLUMN delivery_status TEXT NOT NULL DEFAULT ''",
            "delivered_to_thread_id": "ALTER TABLE tasks ADD COLUMN delivered_to_thread_id TEXT",
            "delivered_at": "ALTER TABLE tasks ADD COLUMN delivered_at INTEGER",
            "delivery_error": "ALTER TABLE tasks ADD COLUMN delivery_error TEXT",
            "ttl": "ALTER TABLE tasks ADD COLUMN ttl INTEGER",
            "max_turns": "ALTER TABLE tasks ADD COLUMN max_turns INTEGER NOT NULL DEFAULT 12",
            "turn_count": "ALTER TABLE tasks ADD COLUMN turn_count INTEGER NOT NULL DEFAULT 0",
            "goal_version": "ALTER TABLE tasks ADD COLUMN goal_version INTEGER NOT NULL DEFAULT 1",
            "exchange_epoch": "ALTER TABLE tasks ADD COLUMN exchange_epoch INTEGER NOT NULL DEFAULT 1",
            "root_task_id": "ALTER TABLE tasks ADD COLUMN root_task_id TEXT",
            "protocol_version": f"ALTER TABLE tasks ADD COLUMN protocol_version TEXT NOT NULL DEFAULT '{PROTOCOL_V03}'",
            "turn_sequence": "ALTER TABLE tasks ADD COLUMN turn_sequence INTEGER",
            "current_message_id": "ALTER TABLE tasks ADD COLUMN current_message_id TEXT",
            "from_agent_id": "ALTER TABLE tasks ADD COLUMN from_agent_id TEXT",
            "to_agent_id": "ALTER TABLE tasks ADD COLUMN to_agent_id TEXT",
            "status_version": "ALTER TABLE tasks ADD COLUMN status_version INTEGER",
            "task_expires_at": "ALTER TABLE tasks ADD COLUMN task_expires_at INTEGER",
            "reason": "ALTER TABLE tasks ADD COLUMN reason TEXT",
            "terminal_by_agent_id": "ALTER TABLE tasks ADD COLUMN terminal_by_agent_id TEXT",
            "completed_against_message_id": "ALTER TABLE tasks ADD COLUMN completed_against_message_id TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)
        conn.execute("UPDATE tasks SET root_task_id = task_id WHERE root_task_id IS NULL")

    def ensure_message_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        migrations = {
            "turn_sequence": "ALTER TABLE messages ADD COLUMN turn_sequence INTEGER",
            "idempotency_key": "ALTER TABLE messages ADD COLUMN idempotency_key TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_v04_idempotency
            ON messages (task_id, from_agent_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )

    def ensure_task_event_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_events)").fetchall()
        }
        if "event_sequence" not in columns:
            conn.execute("ALTER TABLE task_events ADD COLUMN event_sequence INTEGER")
        migrations = {
            "status_version": "ALTER TABLE task_events ADD COLUMN status_version INTEGER",
            "message_id": "ALTER TABLE task_events ADD COLUMN message_id TEXT",
            "turn_sequence": "ALTER TABLE task_events ADD COLUMN turn_sequence INTEGER",
            "from_status": "ALTER TABLE task_events ADD COLUMN from_status TEXT",
            "to_status": "ALTER TABLE task_events ADD COLUMN to_status TEXT",
            "reason": "ALTER TABLE task_events ADD COLUMN reason TEXT",
            "actor_agent_id": "ALTER TABLE task_events ADD COLUMN actor_agent_id TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_task_events_task_sequence
                ON task_events (task_id, event_sequence, created_at, event_id)
            """
        )

    def ensure_agent_event_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(agent_events)").fetchall()
        }
        migrations = {
            "idempotency_key": "ALTER TABLE agent_events ADD COLUMN idempotency_key TEXT",
            "delivery_state": "ALTER TABLE agent_events ADD COLUMN delivery_state TEXT NOT NULL DEFAULT 'pending'",
            "delivery_attempts": "ALTER TABLE agent_events ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0",
            "inflight_until": "ALTER TABLE agent_events ADD COLUMN inflight_until INTEGER",
            "done_at": "ALTER TABLE agent_events ADD COLUMN done_at INTEGER",
            "failed_at": "ALTER TABLE agent_events ADD COLUMN failed_at INTEGER",
            "last_error": "ALTER TABLE agent_events ADD COLUMN last_error TEXT",
            "message_id": "ALTER TABLE agent_events ADD COLUMN message_id TEXT",
            "can_transition_task": "ALTER TABLE agent_events ADD COLUMN can_transition_task INTEGER NOT NULL DEFAULT 0",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_events_agent_delivery
                ON agent_events (agent_id, delivery_state, created_at, event_id)
            """
        )

    def ensure_v04_storage(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS task_mutations (
                task_id TEXT NOT NULL,
                actor_agent_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                result_task_id TEXT NOT NULL,
                result_message_id TEXT,
                request_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (task_id, actor_agent_id, operation, idempotency_key),
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
                FOREIGN KEY (result_task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS task_create_requests (
                requester_agent_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                task_id TEXT NOT NULL,
                source_task_id TEXT,
                request_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (requester_agent_id, idempotency_key),
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
            );

            CREATE TRIGGER IF NOT EXISTS prevent_task_hard_delete
            BEFORE DELETE ON tasks
            BEGIN
                SELECT RAISE(ABORT, 'AgentRelay protocol forbids hard deletion of tasks');
            END;
            """
        )
        for table, additions in {
            "task_mutations": {
                "request_hash": "ALTER TABLE task_mutations ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''",
            },
            "task_create_requests": {
                "source_task_id": "ALTER TABLE task_create_requests ADD COLUMN source_task_id TEXT",
                "request_hash": "ALTER TABLE task_create_requests ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''",
            },
        }.items():
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, sql in additions.items():
                if column not in columns:
                    conn.execute(sql)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_events_idempotency
                ON agent_events (agent_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL
            """
        )

    def ensure_seed_agents(self, conn: sqlite3.Connection) -> None:
        now = int(time.time())
        agents = [
            (
                "zac-agent",
                "Zac Agent",
                "Zac",
                "Personal coordinator agent for Zac.",
                "personal_agent",
                "notify_only",
                DEFAULT_PERSONAL_CAPABILITIES,
                DEFAULT_PERSONAL_POLICY,
            ),
            (
                "frank-agent",
                "Frank Agent",
                "Frank",
                "Personal coordinator agent for Frank.",
                "personal_agent",
                "notify_only",
                DEFAULT_PERSONAL_CAPABILITIES,
                DEFAULT_PERSONAL_POLICY,
            ),
        ]
        for agent_id, name, owner, description, agent_role, execution_mode, capabilities, policy in agents:
            conn.execute(
                """
                INSERT OR IGNORE INTO agents
                    (agent_id, name, owner, description, agent_role, execution_mode, capabilities_json, policy_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    name,
                    owner,
                    description,
                    agent_role,
                    execution_mode,
                    json.dumps(capabilities),
                    json.dumps(policy),
                    now,
                ),
            )

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_agents(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM agents ORDER BY agent_id").fetchall()
            return [dict(row) for row in rows]

    def upsert_agent(
        self,
        agent_id: str,
        owner: str,
        name: str | None = None,
        description: str | None = None,
        agent_role: str | None = None,
        execution_mode: str | None = None,
        capabilities: list[str] | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        agent_name = name or f"{owner} Agent"
        agent_description = description or f"Personal coordinator agent for {owner}."
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            created_at = int(existing["created_at"]) if existing else now
            normalized_role = normalize_agent_role(agent_role or (existing["agent_role"] if existing else None), agent_id)
            normalized_execution_mode = normalize_execution_mode(
                execution_mode or (existing["execution_mode"] if existing else None),
                normalized_role,
            )
            normalized_capabilities = capabilities
            if normalized_capabilities is None and existing:
                normalized_capabilities = parse_json_list(existing["capabilities_json"])
            if normalized_capabilities is None:
                normalized_capabilities = default_agent_capabilities(normalized_role)
            normalized_policy = policy
            if normalized_policy is None and existing:
                normalized_policy = parse_json_object(existing["policy_json"])
            if normalized_policy is None:
                normalized_policy = default_agent_policy(normalized_role)
            conn.execute(
                """
                INSERT OR REPLACE INTO agents
                    (agent_id, name, owner, description, agent_role, execution_mode, capabilities_json, policy_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    agent_name,
                    owner,
                    agent_description,
                    normalized_role,
                    normalized_execution_mode,
                    json.dumps(normalized_capabilities),
                    json.dumps(normalized_policy),
                    created_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            return dict(row)

    def create_install_healthcheck(
        self,
        requester_agent_id: str,
        requester_owner: str | None = None,
        requester_thread_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        task_id = install_healthcheck_task_id(requester_agent_id, idempotency_key)
        context_id = f"ctx_install_health_{uuid.uuid4().hex}"
        message_id = f"msg_{uuid.uuid4().hex}"
        artifact_id = f"art_{uuid.uuid4().hex}"
        ttl = now + HEALTHCHECK_TTL_SECONDS
        subject = "AgentRelay install loopback health check"
        done_criteria = (
            "AgentRelay server creates a synthetic ACK artifact, sends a task.pending "
            "event to the requester agent, and the local inbox records the task."
        )
        request_text = (
            "Run an AgentRelay install loopback health check. This task is synthetic "
            "and must not call a remote agent adapter."
        )
        ack_text = install_healthcheck_ack_text(requester_agent_id, task_id)

        with self.connect() as conn:
            ensure_agent_conn(conn, requester_agent_id, requester_owner or requester_agent_id, now)
            ensure_healthcheck_agent_conn(conn, now)
            self.expire_stale_install_healthchecks_conn(conn, now)
            existing = self.get_task_conn(conn, task_id) if idempotency_key else None
            if existing:
                event = latest_agent_event_conn(conn, requester_agent_id, task_id)
                return {
                    "task": existing,
                    "event": event,
                    "ack": {
                        "actor_agent_id": HEALTHCHECK_AGENT_ID,
                        "intent": "install_health_ack",
                        "text": install_healthcheck_ack_text(requester_agent_id, task_id),
                    },
                    "idempotent": True,
                }
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, context_id, status, requester_agent_id, target_agent_id,
                    requester_thread_id, requester_thread_policy, target_thread_policy,
                    done_criteria, completion_owner_agent_id,
                    pending_on_agent_id, pending_on_human_id, next_action, parent_task_id,
                    delivery_status, subject, ttl, max_turns, turn_count,
                    created_at, updated_at
                )
                VALUES (?, ?, 'delivery_pending', ?, ?, ?, 'reuse-origin-thread',
                    'reuse-task-thread', ?, ?, ?, NULL, ?, NULL, 'pending', ?, ?, 2, 1, ?, ?)
                """,
                (
                    task_id,
                    context_id,
                    requester_agent_id,
                    HEALTHCHECK_AGENT_ID,
                    requester_thread_id,
                    done_criteria,
                    requester_agent_id,
                    requester_agent_id,
                    "Requester agent should verify the synthetic ACK reached the local inbox, then close this health check task.",
                    subject,
                    ttl,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO messages (
                    message_id, task_id, context_id, from_agent_id, to_agent_id,
                    role, parts_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, 'user', ?, ?)
                """,
                (
                    message_id,
                    task_id,
                    context_id,
                    requester_agent_id,
                    HEALTHCHECK_AGENT_ID,
                    json.dumps([{"kind": "text", "text": request_text}]),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, task_id, from_agent_id, to_agent_id,
                    kind, parts_json, created_at
                )
                VALUES (?, ?, ?, ?, 'install_health_ack', ?, ?)
                """,
                (
                    artifact_id,
                    task_id,
                    HEALTHCHECK_AGENT_ID,
                    requester_agent_id,
                    json.dumps([{"kind": "text", "text": ack_text}]),
                    now,
                ),
            )
            self.add_event_conn(
                conn,
                task_id,
                "task.created",
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "idempotency_key": idempotency_key,
                    "requester_agent_id": requester_agent_id,
                    "target_agent_id": HEALTHCHECK_AGENT_ID,
                    "actor_agent_id": requester_agent_id,
                    "intent": "install_health_check",
                    "requesterThreadId": requester_thread_id,
                    "doneCriteria": done_criteria,
                    "done_criteria": done_criteria,
                    "completionOwnerAgentId": requester_agent_id,
                    "pendingOnAgentId": requester_agent_id,
                    "pending_on_agent_id": requester_agent_id,
                    "next_action": "Requester agent should verify the synthetic ACK reached the local inbox, then close this health check task.",
                },
                now,
            )
            artifact_source_refs = [
                {
                    "type": "tool_result",
                    "label": "AgentRelay install loopback ACK",
                    "summary": "Synthetic ACK generated by AgentRelay server healthcheck endpoint.",
                    "visibility": "public",
                    "uri": f"agentrelay://tasks/{task_id}/artifacts/{artifact_id}",
                }
            ]
            self.add_event_conn(
                conn,
                task_id,
                "artifact.submitted",
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "idempotency_key": idempotency_key,
                    "artifactId": artifact_id,
                    "actor_agent_id": HEALTHCHECK_AGENT_ID,
                    "intent": "install_health_ack",
                    "kind": "install_health_ack",
                    "summary": f"ACK from {HEALTHCHECK_AGENT_ID}",
                    "source_refs": artifact_source_refs,
                    "target_agent_id": requester_agent_id,
                    "requester_agent_id": requester_agent_id,
                    "nextStatus": "delivery_pending",
                    "pendingOnAgentId": requester_agent_id,
                    "pending_on_agent_id": requester_agent_id,
                    "nextAction": "Requester agent should verify the synthetic ACK reached the local inbox, then close this health check task.",
                },
                now,
            )
            self.add_event_conn(
                conn,
                task_id,
                "ownership.transferred",
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "actor_agent_id": HEALTHCHECK_AGENT_ID,
                    "pendingOnAgentId": requester_agent_id,
                    "pending_on_agent_id": requester_agent_id,
                },
                now,
            )
            event = self.create_pending_agent_event_conn(conn, task_id, "install.healthcheck", now)
            task = self.get_task_conn(conn, task_id)
            return {
                "task": task,
                "event": event,
                "ack": {
                    "actor_agent_id": HEALTHCHECK_AGENT_ID,
                    "intent": "install_health_ack",
                    "text": ack_text,
                },
            }

    def expire_stale_install_healthchecks_conn(self, conn: sqlite3.Connection, now: int) -> None:
        rows = conn.execute(
            """
            SELECT task_id FROM tasks
            WHERE target_agent_id = ?
              AND status NOT IN ('completed', 'failed', 'cancelled', 'expired', 'rejected')
              AND ttl IS NOT NULL
              AND ttl <= ?
            """,
            (HEALTHCHECK_AGENT_ID, now),
        ).fetchall()
        for row in rows:
            task_id = row["task_id"]
            conn.execute(
                """
                UPDATE tasks
                SET status = 'expired',
                    pending_on_agent_id = NULL,
                    pending_on_human_id = NULL,
                    next_action = NULL,
                    claimed_by = NULL,
                    claimed_at = NULL,
                    terminal_reason = 'Install loopback health check expired before local completion.',
                    updated_at = ?
                WHERE task_id = ?
                """,
                (now, task_id),
            )
            self.add_event_conn(
                conn,
                task_id,
                "task.expired",
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "actor_agent_id": HEALTHCHECK_AGENT_ID,
                    "reason": "install.healthcheck.ttl_expired",
                    "terminalReason": "Install loopback health check expired before local completion.",
                    "terminal_reason": "Install loopback health check expired before local completion.",
                },
                now,
            )
            self.cleanup_terminal_task_delivery_conn(conn, task_id, now)

    def expire_stale_tasks_conn(self, conn: sqlite3.Connection, now: int) -> None:
        self.fail_max_turns_exhausted_tasks_conn(conn, now)
        rows = conn.execute(
            f"""
            SELECT task_id FROM tasks
            WHERE status NOT IN ({", ".join("?" for _ in TERMINAL_STATES)})
              AND ttl IS NOT NULL
              AND CAST(ttl AS INTEGER) > 1000000000
              AND CAST(ttl AS INTEGER) <= ?
              AND NOT EXISTS (
                SELECT 1 FROM artifacts
                WHERE artifacts.task_id = tasks.task_id
                  AND artifacts.from_agent_id = tasks.target_agent_id
              )
            ORDER BY ttl, created_at, task_id
            """,
            (*sorted(TERMINAL_STATES), now),
        ).fetchall()
        for row in rows:
            task_id = row["task_id"]
            task = self.get_task_conn(conn, task_id)
            if not task or task["status"] in TERMINAL_STATES:
                continue
            terminal_reason = (
                f"Task expired before {task['target_agent_id']} replied within the configured TTL."
            )
            conn.execute(
                """
                UPDATE tasks
                SET status = 'expired',
                    pending_on_agent_id = NULL,
                    pending_on_human_id = NULL,
                    next_action = NULL,
                    claimed_by = NULL,
                    claimed_at = NULL,
                    terminal_reason = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (terminal_reason, now, task_id),
            )
            self.add_event_conn(
                conn,
                task_id,
                "task.expired",
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "actor_agent_id": "agentrelay",
                    "requester_agent_id": task["requester_agent_id"],
                    "target_agent_id": task["target_agent_id"],
                    "reason": "task.ttl_expired",
                    "ttl": task.get("ttl"),
                    "expired_at": now,
                    "terminalReason": terminal_reason,
                    "terminal_reason": terminal_reason,
                },
                now,
            )
            self.cleanup_terminal_task_delivery_conn(conn, task_id, now)
            expired_task = self.get_task_conn(conn, task_id)
            if expired_task:
                self.create_agent_event_conn(
                    conn,
                    expired_task["requester_agent_id"],
                    "task.pending",
                    task_id,
                    expired_task_event_payload(expired_task, now),
                    idempotency_key=f"{task_id}:{expired_task['requester_agent_id']}:task.ttl_expired:{expired_task.get('ttl')}",
                    created_at=now,
                )

    def fail_max_turns_exhausted_tasks_conn(self, conn: sqlite3.Connection, now: int) -> None:
        rows = conn.execute(
            f"""
            SELECT * FROM tasks
            WHERE status NOT IN ({", ".join("?" for _ in TERMINAL_STATES)})
              AND pending_on_agent_id IS NOT NULL
              AND pending_on_agent_id != COALESCE(NULLIF(completion_owner_agent_id, ''), requester_agent_id)
              AND CAST(turn_count AS INTEGER) >= CAST(max_turns AS INTEGER)
            ORDER BY updated_at, created_at, task_id
            """,
            (*sorted(TERMINAL_STATES),),
        ).fetchall()
        for row in rows:
            task = dict(row)
            if is_exhausted_for_pending_agent(task):
                self.fail_task_max_turns_conn(
                    conn,
                    task,
                    now,
                    reason="task.max_turns_exhausted",
                )

    def fail_task_max_turns_conn(
        self,
        conn: sqlite3.Connection,
        task: dict[str, Any],
        now: int,
        *,
        reason: str,
        attempted_actor_agent_id: str | None = None,
        attempted_pending_on_agent_id: str | None = None,
        attempted_turn_count: int | None = None,
    ) -> None:
        task_id = task["task_id"]
        if task.get("status") in TERMINAL_STATES:
            return
        previous_pending_on_agent_id = task.get("pending_on_agent_id")
        turn_count = int(task.get("turn_count") or 0)
        max_turns = int(task.get("max_turns") or 12)
        conn.execute(
            """
            UPDATE tasks
            SET status = 'failed',
                pending_on_agent_id = NULL,
                pending_on_human_id = NULL,
                next_action = NULL,
                claimed_by = NULL,
                claimed_at = NULL,
                terminal_reason = ?,
                updated_at = ?
            WHERE task_id = ?
            """,
            (MAX_TURNS_TERMINAL_REASON, now, task_id),
        )
        self.add_event_conn(
            conn,
            task_id,
            "task.status_updated",
            {
                "protocol_version": PROTOCOL_VERSION,
                "actor_agent_id": "agentrelay",
                "status": "failed",
                "reason": reason,
                "terminalReason": MAX_TURNS_TERMINAL_REASON,
                "terminal_reason": MAX_TURNS_TERMINAL_REASON,
                "requester_agent_id": task.get("requester_agent_id"),
                "target_agent_id": task.get("target_agent_id"),
                "completion_owner_agent_id": task.get("completion_owner_agent_id"),
                "previousPendingOnAgentId": previous_pending_on_agent_id,
                "previous_pending_on_agent_id": previous_pending_on_agent_id,
                "turnCount": turn_count,
                "turn_count": turn_count,
                "maxTurns": max_turns,
                "max_turns": max_turns,
                "attemptedActorAgentId": attempted_actor_agent_id,
                "attempted_actor_agent_id": attempted_actor_agent_id,
                "attemptedPendingOnAgentId": attempted_pending_on_agent_id,
                "attempted_pending_on_agent_id": attempted_pending_on_agent_id,
                "attemptedTurnCount": attempted_turn_count,
                "attempted_turn_count": attempted_turn_count,
            },
            now,
        )
        self.cleanup_terminal_task_delivery_conn(conn, task_id, now)
        failed_task = self.get_task_conn(conn, task_id)
        requester_agent_id = task.get("requester_agent_id")
        if failed_task and requester_agent_id:
            self.create_agent_event_conn(
                conn,
                requester_agent_id,
                "task.pending",
                task_id,
                failed_task_event_payload(
                    failed_task,
                    now,
                    reason=reason,
                    previous_pending_on_agent_id=previous_pending_on_agent_id,
                    attempted_actor_agent_id=attempted_actor_agent_id,
                    attempted_pending_on_agent_id=attempted_pending_on_agent_id,
                    attempted_turn_count=attempted_turn_count,
                ),
                idempotency_key=f"{task_id}:{requester_agent_id}:task.max_turns_failed:{max_turns}:{turn_count}",
                created_at=now,
            )

    def add_v04_event_conn(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        *,
        actor_agent_id: str | None,
        payload: dict[str, Any],
        created_at: int,
    ) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(event_sequence), 0) + 1 AS next_sequence FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO task_events (
                event_id, task_id, event_type, payload_json, event_sequence, created_at,
                status_version, message_id, turn_sequence, from_status, to_status,
                reason, actor_agent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"evt_{uuid.uuid4().hex}", task_id, event_type, json.dumps(payload),
                int(row["next_sequence"]), created_at, payload.get("status_version"),
                payload.get("message_id"), payload.get("turn_sequence"), from_status,
                to_status, payload.get("reason"), actor_agent_id,
            ),
        )

    def create_v04_agent_event_conn(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        event_type: str,
        task_id: str,
        message_id: str | None,
        payload: dict[str, Any],
        *,
        can_transition_task: bool,
        idempotency_key: str,
        created_at: int,
    ) -> None:
        event = self.create_agent_event_conn(
            conn, agent_id, event_type, task_id, payload,
            idempotency_key=idempotency_key, created_at=created_at,
        )
        conn.execute(
            "UPDATE agent_events SET message_id = ?, can_transition_task = ? WHERE event_id = ?",
            (message_id, 1 if can_transition_task else 0, event["event_id"]),
        )

    def record_v04_transition_conn(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        from_status: str | None,
        to_status: str,
        actor_agent_id: str | None,
        message_id: str,
        turn_sequence: int,
        status_version: int,
        reason: str | None,
        created_at: int,
    ) -> None:
        task = dict(conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())
        event_payload = {
            "task_id": task_id,
            "protocol_version": PROTOCOL_V04,
            "message_id": message_id,
            "turn_sequence": turn_sequence,
            "status_version": status_version,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "actor_agent_id": actor_agent_id,
        }
        self.add_v04_event_conn(
            conn, task_id, "task.status_changed", from_status, to_status,
            actor_agent_id=actor_agent_id, payload=event_payload, created_at=created_at,
        )
        for participant in {task["requester_agent_id"], task["target_agent_id"]}:
            self.create_v04_agent_event_conn(
                conn, participant, "task.status_changed", task_id, message_id,
                event_payload, can_transition_task=False,
                idempotency_key=f"v04:{task_id}:status:{status_version}:{participant}",
                created_at=created_at,
            )
        if to_status == "submitted":
            self.create_v04_agent_event_conn(
                conn, task["to_agent_id"], "task.message_pending", task_id, message_id,
                {
                    **event_payload,
                    "from_agent_id": task["from_agent_id"],
                    "to_agent_id": task["to_agent_id"],
                    "parts": json.loads(conn.execute("SELECT parts_json FROM messages WHERE message_id = ?", (message_id,)).fetchone()["parts_json"]),
                },
                can_transition_task=True,
                idempotency_key=f"v04:{task_id}:message:{message_id}:pending",
                created_at=created_at,
            )

    def expire_v04_tasks_conn(self, conn: sqlite3.Connection, now: int, task_id: str | None = None) -> None:
        params: list[Any] = [PROTOCOL_V04, now]
        task_filter = ""
        if task_id:
            task_filter = "AND task_id = ?"
            params.append(task_id)
        rows = conn.execute(
            f"""
            SELECT * FROM tasks
            WHERE protocol_version = ? AND status IN ('submitted', 'delivered')
              AND task_expires_at <= ? {task_filter}
            """,
            params,
        ).fetchall()
        for row in rows:
            task = dict(row)
            next_version = int(task["status_version"]) + 1
            cursor = conn.execute(
                """
                UPDATE tasks SET status = 'expired', status_version = ?, reason = 'task_timeout',
                    terminal_by_agent_id = NULL, updated_at = ?
                WHERE task_id = ? AND status_version = ? AND status IN ('submitted', 'delivered')
                """,
                (next_version, now, task["task_id"], task["status_version"]),
            )
            if cursor.rowcount == 1:
                self.record_v04_transition_conn(
                    conn, task["task_id"], task["status"], "expired", None,
                    task["current_message_id"], task["turn_sequence"], next_version,
                    "task_timeout", now,
                )

    def assert_v04_context(self, task: dict[str, Any], payload: dict[str, Any]) -> None:
        if task.get("protocol_version") != PROTOCOL_V04:
            raise ConflictError("operation requires a v0.4 task")
        if task["status"] in {"completed", "expired", "failed"}:
            raise ConflictError(f"task is terminal: {task['status']}")
        if payload.get("current_message_id") != task["current_message_id"]:
            raise ConflictError("stale_task_state: current_message_id mismatch", code="stale_task_state", current_task=task)
        if int(payload.get("turn_sequence")) != int(task["turn_sequence"]):
            raise ConflictError("stale_task_state: turn_sequence mismatch", code="stale_task_state", current_task=task)
        if int(payload.get("expected_status_version")) != int(task["status_version"]):
            raise ConflictError("stale_task_state: expected_status_version mismatch", code="stale_task_state", current_task=task)

    def create_task_v04(self, payload: dict[str, Any], *, source_task_id: str | None = None) -> dict[str, Any]:
        now = int(time.time())
        requester = str(payload["requester_agent_id"])
        target = str(payload["target_agent_id"])
        idempotency_key = str(payload["idempotency_key"])
        max_turns = int(payload.get("max_turns") or 12)
        expires_at = int(payload.get("task_expires_at") or (now + DEFAULT_TASK_TTL_SECONDS))
        if expires_at <= now:
            raise ValueError("task_expires_at must be in the future")
        message = payload["message"]
        parts = message["parts"]
        task_id = f"task_{uuid.uuid4().hex}"
        message_id = message.get("message_id") or f"msg_{uuid.uuid4().hex}"
        done_criteria_value = payload["done_criteria"]
        done_criteria = json.dumps(done_criteria_value, sort_keys=True) if isinstance(done_criteria_value, dict) else str(done_criteria_value)
        request_hash = request_fingerprint(payload)

        with self.connect() as conn:
            assert_agent_exists(conn, requester)
            assert_agent_exists(conn, target)
            existing = conn.execute(
                "SELECT task_id, source_task_id, request_hash FROM task_create_requests WHERE requester_agent_id = ? AND idempotency_key = ?",
                (requester, idempotency_key),
            ).fetchone()
            if existing:
                if existing["source_task_id"] != source_task_id or existing["request_hash"] != request_hash:
                    raise ConflictError("idempotency_key was already used for a different Task create request")
                task = self.get_task_conn(conn, existing["task_id"])
                if task:
                    return task
                raise ConflictError("idempotent create result is missing")

            root_task_id = task_id
            if source_task_id:
                source = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (source_task_id,)).fetchone()
                if not source:
                    raise ValueError("source task not found")
                source = dict(source)
                self.expire_v04_tasks_conn(conn, now, source_task_id)
                source = dict(conn.execute("SELECT * FROM tasks WHERE task_id = ?", (source_task_id,)).fetchone())
                if source.get("protocol_version") != PROTOCOL_V04:
                    raise ConflictError("follow-up source must be a v0.4 task")
                if source["status"] not in {"completed", "expired", "failed"}:
                    raise ConflictError("follow-up source must be terminal")
                if source["requester_agent_id"] != requester or source["target_agent_id"] != target:
                    raise ConflictError("follow-up participants must match the source task")
                root_task_id = source["root_task_id"]

            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, root_task_id, protocol_version, context_id, status,
                    requester_agent_id, target_agent_id, done_criteria,
                    completion_owner_agent_id, subject, max_turns, turn_sequence,
                    current_message_id, from_agent_id, to_agent_id, status_version,
                    task_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    task_id, root_task_id, PROTOCOL_V04, f"ctx_{uuid.uuid4().hex}", requester,
                    target, done_criteria, requester, payload.get("subject") or "AgentRelay task",
                    max_turns, message_id, requester, target, expires_at, now, now,
                ),
            )
            context_id = conn.execute("SELECT context_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()["context_id"]
            conn.execute(
                """
                INSERT INTO messages (
                    message_id, task_id, context_id, from_agent_id, to_agent_id,
                    role, parts_json, created_at, turn_sequence, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, 'user', ?, ?, 1, ?)
                """,
                (message_id, task_id, context_id, requester, target, json.dumps(parts), now, idempotency_key),
            )
            conn.execute(
                """
                INSERT INTO task_create_requests (
                    requester_agent_id, idempotency_key, task_id, source_task_id, request_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (requester, idempotency_key, task_id, source_task_id, request_hash, now),
            )
            self.record_v04_transition_conn(conn, task_id, None, "submitted", requester, message_id, 1, 1, None, now)
            if source_task_id:
                self.add_v04_event_conn(
                    conn, source_task_id, "task.followup_created", None, None,
                    actor_agent_id=requester,
                    payload={"source_task_id": source_task_id, "new_task_id": task_id, "root_task_id": root_task_id},
                    created_at=now,
                )
            return self.get_task_conn(conn, task_id)

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("protocol_version") == PROTOCOL_V04:
            return self.create_task_v04(payload)
        now = int(time.time())
        normalized = normalize_task_create(payload)
        task_id = normalized["task_id"]
        context_id = normalized["context_id"]
        requester_agent_id = normalized["requester_agent_id"]
        target_agent_id = normalized["target_agent_id"]
        message = normalized["message"]
        subject = normalized["subject"]
        parts = message["parts"]
        requester_thread_id = normalized["requester_thread_id"]
        ttl = normalize_task_ttl(
            normalized["ttl"],
            ttl_seconds=normalized["ttl_seconds"],
            now=now,
        )
        max_turns = normalized["max_turns"]
        done_criteria = normalized["done_criteria"]
        completion_owner_agent_id = normalized["completion_owner_agent_id"]
        pending_on_agent_id = normalized["pending_on_agent_id"]
        pending_on_human_id = normalized["pending_on_human_id"]
        next_action = normalized["next_action"]
        parent_task_id = normalized["parent_task_id"]
        protocol_version = normalized["protocol_version"]
        idempotency_key = normalized["idempotency_key"]
        done_criteria_payload = normalized["done_criteria_payload"]

        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            assert_agent_exists(conn, requester_agent_id)
            assert_agent_exists(conn, target_agent_id)
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, context_id, status, requester_agent_id, target_agent_id,
                    requester_thread_id, requester_thread_policy, target_thread_policy,
                    done_criteria, completion_owner_agent_id,
                    pending_on_agent_id, pending_on_human_id, next_action, parent_task_id,
                    subject, ttl, max_turns, created_at, updated_at
                )
                VALUES (?, ?, 'submitted', ?, ?, ?, 'reuse-origin-thread',
                    'reuse-task-thread', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    context_id,
                    requester_agent_id,
                    target_agent_id,
                    requester_thread_id,
                    done_criteria,
                    completion_owner_agent_id,
                    pending_on_agent_id,
                    pending_on_human_id,
                    next_action,
                    parent_task_id,
                    subject,
                    ttl,
                    max_turns,
                    now,
                    now,
                ),
            )
            message_id = message["message_id"]
            conn.execute(
                """
                INSERT INTO messages (
                    message_id, task_id, context_id, from_agent_id, to_agent_id,
                    role, parts_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    task_id,
                    context_id,
                    requester_agent_id,
                    target_agent_id,
                    message["legacy_role"],
                    json.dumps(parts),
                    now,
                ),
            )
            self.add_event_conn(
                conn,
                task_id,
                "task.created",
                {
                    "protocol_version": protocol_version,
                    "contextId": context_id,
                    "idempotency_key": idempotency_key,
                    "requester_agent_id": requester_agent_id,
                    "target_agent_id": target_agent_id,
                    "actor_agent_id": message["actor_agent_id"],
                    "intent": message["intent"],
                    "requesterThreadId": requester_thread_id,
                    "doneCriteria": done_criteria,
                    "done_criteria": done_criteria_payload,
                    "completionOwnerAgentId": completion_owner_agent_id,
                    "completion_owner_agent_id": completion_owner_agent_id,
                    "pendingOnAgentId": pending_on_agent_id,
                    "pending_on_agent_id": pending_on_agent_id,
                    "next_action": next_action,
                    "ttl": ttl,
                    "maxTurns": max_turns,
                    "max_turns": max_turns,
                    "goal_version": 1,
                    "exchange_epoch": 1,
                },
                now,
            )
            self.create_pending_agent_event_conn(conn, task_id, "task.created", now)
            return self.get_task_conn(conn, task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            self.expire_v04_tasks_conn(conn, now, task_id)
            return self.get_task_conn(conn, task_id)

    def existing_v04_mutation_conn(
        self, conn: sqlite3.Connection, task_id: str, actor: str, operation: str, key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT result_task_id, request_hash FROM task_mutations
            WHERE task_id = ? AND actor_agent_id = ? AND operation = ? AND idempotency_key = ?
            """,
            (task_id, actor, operation, key),
        ).fetchone()
        if row and row["request_hash"] != request_fingerprint(payload):
            raise ConflictError("idempotency_key was already used for a different mutation request")
        return self.get_task_conn(conn, row["result_task_id"]) if row else None

    def record_v04_mutation_conn(
        self, conn: sqlite3.Connection, task_id: str, actor: str, operation: str,
        key: str, result_task_id: str, message_id: str | None, payload: dict[str, Any], now: int
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_mutations (
                task_id, actor_agent_id, operation, idempotency_key,
                result_task_id, result_message_id, request_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, actor, operation, key, result_task_id, message_id, request_fingerprint(payload), now),
        )

    def submit_v04_message(self, task_id: str, actor: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = int(time.time())
        key = str(payload["idempotency_key"])
        with self.connect() as conn:
            existing = self.existing_v04_mutation_conn(conn, task_id, actor, "message", key, payload)
            if existing:
                return existing
            self.expire_v04_tasks_conn(conn, now, task_id)
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            task = dict(row)
            self.assert_v04_context(task, payload)
            if task["status"] != "delivered":
                raise ConflictError("new message requires delivered status")
            if actor != task["to_agent_id"]:
                raise ConflictError("only the current to_agent_id may send the next message")
            if actor == task["from_agent_id"]:
                raise ConflictError("same Agent cannot send consecutive messages")
            next_turn = int(task["turn_sequence"])
            if actor == task["requester_agent_id"]:
                if task["from_agent_id"] != task["target_agent_id"]:
                    raise ConflictError("requester follow-up requires a delivered target response")
                if next_turn >= int(task["max_turns"]):
                    raise ConflictError("max_turns_reached")
                next_turn += 1
            elif actor != task["target_agent_id"] or task["from_agent_id"] != task["requester_agent_id"]:
                raise ConflictError("target response requires a delivered requester message")

            message_id = payload.get("message_id") or f"msg_{uuid.uuid4().hex}"
            next_version = int(task["status_version"]) + 1
            conn.execute(
                """
                INSERT INTO messages (
                    message_id, task_id, context_id, from_agent_id, to_agent_id,
                    role, parts_json, created_at, turn_sequence, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, 'agent', ?, ?, ?, ?)
                """,
                (message_id, task_id, task["context_id"], actor, task["from_agent_id"],
                 json.dumps(payload["parts"]), now, next_turn, key),
            )
            cursor = conn.execute(
                """
                UPDATE tasks SET status = 'submitted', turn_sequence = ?, current_message_id = ?,
                    from_agent_id = ?, to_agent_id = ?, status_version = ?, updated_at = ?
                WHERE task_id = ? AND status_version = ? AND status = 'delivered'
                """,
                (next_turn, message_id, actor, task["from_agent_id"], next_version, now,
                 task_id, task["status_version"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("stale_task_state")
            self.record_v04_transition_conn(
                conn, task_id, "delivered", "submitted", actor, message_id,
                next_turn, next_version, None, now,
            )
            self.record_v04_mutation_conn(conn, task_id, actor, "message", key, task_id, message_id, payload, now)
            return self.get_task_conn(conn, task_id)

    def ack_v04_message(self, agent_id: str, message_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = int(time.time())
        task_id = str(payload.get("task_id") or "")
        key = str(payload["idempotency_key"])
        with self.connect() as conn:
            existing = self.existing_v04_mutation_conn(conn, task_id, agent_id, "ack", key, payload)
            if existing:
                return existing
            self.expire_v04_tasks_conn(conn, now, task_id)
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            task = dict(row)
            self.assert_v04_context(task, payload)
            if message_id != task["current_message_id"] or payload.get("message_id") != message_id:
                raise ConflictError("stale_task_state: message_id mismatch")
            if task["status"] != "submitted" or task["to_agent_id"] != agent_id:
                raise ConflictError("only current to_agent_id may ACK a submitted message")
            pending = conn.execute(
                """
                SELECT * FROM agent_events
                WHERE agent_id = ? AND task_id = ? AND message_id = ?
                  AND event_type = 'task.message_pending' AND can_transition_task = 1
                ORDER BY created_at DESC LIMIT 1
                """,
                (agent_id, task_id, message_id),
            ).fetchone()
            if not pending:
                raise ConflictError("current message pending event not found")
            next_version = int(task["status_version"]) + 1
            cursor = conn.execute(
                """
                UPDATE tasks SET status = 'delivered', status_version = ?, updated_at = ?
                WHERE task_id = ? AND status = 'submitted' AND status_version = ?
                """,
                (next_version, now, task_id, task["status_version"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("stale_task_state")
            conn.execute(
                """
                UPDATE agent_events SET delivery_state = 'done', acked_at = ?, done_at = ?,
                    failed_at = NULL, inflight_until = NULL, last_error = NULL
                WHERE event_id = ?
                """,
                (now, now, pending["event_id"]),
            )
            self.record_v04_transition_conn(
                conn, task_id, "submitted", "delivered", agent_id, message_id,
                task["turn_sequence"], next_version, None, now,
            )
            self.record_v04_mutation_conn(conn, task_id, agent_id, "ack", key, task_id, message_id, payload, now)
            return self.get_task_conn(conn, task_id)

    def complete_v04_task(self, task_id: str, actor: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        return self.terminal_v04_task(task_id, actor, "completed", payload)

    def fail_v04_task(self, task_id: str, actor: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        return self.terminal_v04_task(task_id, actor, "failed", payload)

    def terminal_v04_task(
        self, task_id: str, actor: str, terminal_status: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        now = int(time.time())
        key = str(payload["idempotency_key"])
        operation = terminal_status
        with self.connect() as conn:
            existing = self.existing_v04_mutation_conn(conn, task_id, actor, operation, key, payload)
            if existing:
                return existing
            self.expire_v04_tasks_conn(conn, now, task_id)
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            task = dict(row)
            self.assert_v04_context(task, payload)
            if terminal_status == "completed":
                if actor != task["requester_agent_id"]:
                    raise ConflictError("only requester may complete the task")
                if task["status"] != "delivered" or task["from_agent_id"] != task["target_agent_id"]:
                    raise ConflictError("completion requires a delivered target response")
                evidence = payload["completed_against_message_id"]
                if evidence != task["current_message_id"]:
                    raise ConflictError("completion evidence must be the current target response")
                reason = "goal_met"
                completed_against = evidence
            else:
                reason = str(payload["reason"])
                if reason not in FAILED_REASONS:
                    raise ValueError(f"unsupported failed reason: {reason}")
                relay_reasons = {"delivery_retry_exhausted", "relay_persistence_failed", "internal_consistency_error"}
                if reason in relay_reasons:
                    if actor != "relay":
                        raise ConflictError(f"{reason} may only be recorded by Relay")
                    if reason == "delivery_retry_exhausted" and task["status"] != "submitted":
                        raise ConflictError("delivery_retry_exhausted requires submitted status")
                elif reason == "listener_persistence_failed":
                    if task["status"] != "submitted" or actor != task["to_agent_id"]:
                        raise ConflictError("listener_persistence_failed requires current Listener in submitted")
                elif reason == "agent_reported_failure":
                    if task["status"] != "delivered" or actor != task["to_agent_id"]:
                        raise ConflictError("agent_reported_failure requires current action owner in delivered")
                elif reason == "max_turns_exhausted":
                    if actor != task["requester_agent_id"] or task["status"] != "delivered" or int(task["turn_sequence"]) < int(task["max_turns"]):
                        raise ConflictError("max_turns_exhausted requires requester at delivered max_turns")
                completed_against = None
            next_version = int(task["status_version"]) + 1
            cursor = conn.execute(
                """
                UPDATE tasks SET status = ?, status_version = ?, reason = ?,
                    terminal_by_agent_id = ?, completed_against_message_id = ?, updated_at = ?
                WHERE task_id = ? AND status_version = ? AND status IN ('submitted', 'delivered')
                """,
                (terminal_status, next_version, reason, None if actor == "relay" else actor,
                 completed_against, now, task_id, task["status_version"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("stale_task_state")
            self.record_v04_transition_conn(
                conn, task_id, task["status"], terminal_status,
                None if actor == "relay" else actor, task["current_message_id"],
                task["turn_sequence"], next_version, reason, now,
            )
            self.record_v04_mutation_conn(conn, task_id, actor, operation, key, task_id, task["current_message_id"], payload, now)
            return self.get_task_conn(conn, task_id)

    def get_task_conn(self, conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
        task_row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not task_row:
            return None
        task = dict(task_row)
        messages = conn.execute(
            "SELECT * FROM messages WHERE task_id = ? ORDER BY created_at, message_id",
            (task_id,),
        ).fetchall()
        artifacts = conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at, artifact_id",
            (task_id,),
        ).fetchall()
        bindings = conn.execute(
            """
            SELECT * FROM task_thread_bindings
            WHERE task_id = ?
            ORDER BY agent_id, thread_role
            """,
            (task_id,),
        ).fetchall()
        task["messages"] = [decode_parts(row) for row in messages]
        task["artifacts"] = [decode_parts(row) for row in artifacts]
        task["threadBindings"] = [dict(row) for row in bindings]
        return task

    def get_task_lineage(self, task_id: str) -> list[dict[str, Any]] | None:
        now = int(time.time())
        with self.connect() as conn:
            self.expire_v04_tasks_conn(conn, now)
            row = conn.execute("SELECT root_task_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE root_task_id = ? ORDER BY created_at, task_id",
                (row["root_task_id"],),
            ).fetchall()
            return [self.get_task_conn(conn, item["task_id"]) for item in rows]

    def get_events(self, task_id: str) -> list[dict[str, Any]] | None:
        now = int(time.time())
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            if not self.get_task_conn(conn, task_id):
                return None
            rows = conn.execute(
                """
                SELECT *, rowid AS _rowid
                FROM task_events
                WHERE task_id = ?
                ORDER BY COALESCE(event_sequence, _rowid), created_at, event_id
                """,
                (task_id,),
            ).fetchall()
            return [decode_payload(row) for row in rows]

    def get_timeline(self, task_id: str) -> dict[str, Any] | None:
        events = self.get_events(task_id)
        if events is None:
            return None
        entries = [
            build_timeline_entry(event, index + 1)
            for index, event in enumerate(events)
        ]
        return {
            "task_id": task_id,
            "entries": entries,
            "summary": summarize_timeline(entries),
        }

    def list_pending_tasks(self, agent_id: str, limit: int = 100) -> list[dict[str, Any]]:
        claimable_placeholders = ", ".join("?" for _ in CLAIMABLE_STATES)
        now = int(time.time())
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            assert_agent_exists(conn, agent_id)
            rows = conn.execute(
                f"""
                SELECT * FROM tasks
                WHERE pending_on_agent_id = ?
                  AND protocol_version != ?
                  AND (
                    status IN ({claimable_placeholders})
                    OR (status = 'claimed' AND claimed_by = ?)
                  )
                  AND (claimed_by IS NULL OR claimed_by = ?)
                  AND (
                    CAST(turn_count AS INTEGER) < CAST(max_turns AS INTEGER)
                    OR pending_on_agent_id = COALESCE(NULLIF(completion_owner_agent_id, ''), requester_agent_id)
                  )
                ORDER BY updated_at, created_at, task_id
                LIMIT ?
                """,
                (agent_id, PROTOCOL_V04, *sorted(CLAIMABLE_STATES), agent_id, agent_id, limit),
            ).fetchall()
            return [summarize_task(row) for row in rows]

    def claim_task(self, agent_id: str) -> dict[str, Any] | None:
        now = int(time.time())
        claimable_placeholders = ", ".join("?" for _ in CLAIMABLE_STATES)
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            assert_agent_exists(conn, agent_id)
            row = conn.execute(
                f"""
                SELECT task_id FROM tasks
                WHERE pending_on_agent_id = ?
                  AND protocol_version != ?
                  AND status IN ({claimable_placeholders})
                  AND (claimed_by IS NULL OR claimed_by = ?)
                  AND (
                    CAST(turn_count AS INTEGER) < CAST(max_turns AS INTEGER)
                    OR pending_on_agent_id = COALESCE(NULLIF(completion_owner_agent_id, ''), requester_agent_id)
                  )
                ORDER BY created_at
                LIMIT 1
                """,
                (agent_id, PROTOCOL_V04, *sorted(CLAIMABLE_STATES), agent_id),
            ).fetchone()
            if not row:
                return None
            task_id = row["task_id"]
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            assert_claim_allowed(task, agent_id)
            conn.execute(
                """
                UPDATE tasks
                SET status = 'claimed',
                    claimed_by = ?,
                    claimed_at = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (agent_id, now, now, task_id),
            )
            self.add_event_conn(conn, task_id, "task.claimed", {"agentId": agent_id}, now)
            return self.get_task_conn(conn, task_id)

    def claim_task_by_id(self, agent_id: str, task_id: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            assert_agent_exists(conn, agent_id)
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            assert_legacy_mutation_allowed(task, "claim")
            try:
                assert_claim_allowed(task, agent_id)
            except TransitionError as exc:
                raise ConflictError(str(exc)) from exc
            if task["status"] == "claimed":
                return task
            conn.execute(
                """
                UPDATE tasks
                SET status = 'claimed',
                    claimed_by = ?,
                    claimed_at = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (agent_id, now, now, task_id),
            )
            self.add_event_conn(
                conn,
                task_id,
                "task.claimed",
                {"agentId": agent_id, "claimMode": "by_task_id"},
                now,
            )
            return self.get_task_conn(conn, task_id)

    def set_thread(self, agent_id: str, task_id: str, thread_id: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            assert_legacy_mutation_allowed(task, "thread binding mutation")
            allowed_agents = {
                task.get("requester_agent_id"),
                task.get("target_agent_id"),
                task.get("completion_owner_agent_id"),
                task.get("pending_on_agent_id"),
                task.get("claimed_by"),
            }
            if agent_id not in allowed_agents:
                raise ValueError("agent is not associated with the task")
            existing_thread_id = task.get("target_thread_id")
            event_type = "thread.reused" if existing_thread_id else "thread.created"
            if task["target_agent_id"] == agent_id:
                conn.execute(
                    """
                    UPDATE tasks
                    SET target_thread_id = ?,
                        status = CASE WHEN status = 'claimed' THEN 'input_required' ELSE status END,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (thread_id, now, task_id),
                )
            else:
                conn.execute("UPDATE tasks SET updated_at = ? WHERE task_id = ?", (now, task_id))
            self.upsert_thread_binding_conn(conn, task_id, agent_id, thread_id, created_or_updated_at=now)
            self.add_event_conn(
                conn,
                task_id,
                event_type,
                {"agentId": agent_id, "threadId": thread_id},
                now,
            )
            return self.get_task_conn(conn, task_id)

    def update_status(self, task_id: str, status: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = int(time.time())
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            assert_legacy_mutation_allowed(task, "status mutation")
            assert_update_status_allowed(task, status, payload)
            terminal_reason = payload.get("terminalReason")
            next_action = payload.get("nextAction")
            pending_on_agent_id = read_alias(payload, "pending_on_agent_id", "pendingOnAgentId")
            pending_on_human_id = payload.get("pendingOnHumanId")
            conn.execute(
                """
                UPDATE tasks
                SET status = ?,
                    delivery_status = CASE
                        WHEN ? IN ('completed', 'failed', 'cancelled', 'expired', 'rejected')
                         AND delivery_status = 'pending'
                        THEN 'delivered'
                        ELSE delivery_status
                    END,
                    claimed_by = CASE
                        WHEN ? IN ('completed', 'failed', 'cancelled', 'expired', 'rejected')
                        THEN NULL
                        ELSE claimed_by
                    END,
                    claimed_at = CASE
                        WHEN ? IN ('completed', 'failed', 'cancelled', 'expired', 'rejected')
                        THEN NULL
                        ELSE claimed_at
                    END,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (status, status, status, status, now, task_id),
            )
            update_fields = {"status": status, **payload}
            if terminal_reason is not None:
                update_fields["terminalReason"] = terminal_reason
                conn.execute(
                    "UPDATE tasks SET terminal_reason = ? WHERE task_id = ?",
                    (terminal_reason, task_id),
                )
            if next_action is not None:
                update_fields["nextAction"] = next_action
                conn.execute(
                    "UPDATE tasks SET next_action = ? WHERE task_id = ?",
                    (next_action, task_id),
                )
            if pending_on_agent_id is not None:
                update_fields["pendingOnAgentId"] = pending_on_agent_id
                conn.execute(
                    "UPDATE tasks SET pending_on_agent_id = ? WHERE task_id = ?",
                    (pending_on_agent_id, task_id),
                )
            if pending_on_human_id is not None:
                update_fields["pendingOnHumanId"] = pending_on_human_id
                conn.execute(
                    "UPDATE tasks SET pending_on_human_id = ? WHERE task_id = ?",
                    (pending_on_human_id, task_id),
                )
            self.add_event_conn(conn, task_id, "task.status_updated", update_fields, now)
            self.create_pending_agent_event_conn(conn, task_id, "task.status_updated", now)
            if status in TERMINAL_STATES:
                self.cleanup_terminal_task_delivery_conn(conn, task_id, now)
            return self.get_task_conn(conn, task_id)

    def amend_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = int(time.time())
        normalized = normalize_task_amend(payload, now=now)
        actor_agent_id = normalized["actor_agent_id"]
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            assert_legacy_mutation_allowed(task, "goal amendment")
            if task.get("status") in TERMINAL_STATES:
                raise ConflictError(f"cannot amend terminal task: {task.get('status')}")
            if actor_agent_id != task.get("requester_agent_id"):
                raise ConflictError("only requester_agent_id can amend task goals")
            if actor_agent_id != task.get("completion_owner_agent_id"):
                raise ConflictError("only completion_owner_agent_id can amend task goals")
            if normalized["human_authority"].get("via_agent_id") != actor_agent_id:
                raise ConflictError("human_authority.via_agent_id must match actor_agent_id")
            current_goal_version = int(task.get("goal_version") or 1)
            if normalized["expected_goal_version"] != current_goal_version:
                raise ConflictError(
                    f"expected_goal_version {normalized['expected_goal_version']} does not match current goal_version {current_goal_version}"
                )
            if task.get("pending_on_agent_id") != actor_agent_id:
                raise ConflictError("task can only be amended while pending on requester review")
            assert_agent_exists(conn, task["target_agent_id"])

            next_goal_version = current_goal_version + 1
            next_exchange_epoch = int(task.get("exchange_epoch") or 1) + 1
            previous_done_criteria = task.get("done_criteria") or ""
            previous_max_turns = int(task.get("max_turns") or 12)
            previous_ttl = task.get("ttl")
            new_max_turns = normalized["new_max_turns"] or previous_max_turns
            new_ttl = normalized["ttl"]
            next_action = normalized["next_action"] or "Target agent should respond to the amended goal."

            conn.execute(
                """
                UPDATE tasks
                SET status = 'submitted',
                    done_criteria = ?,
                    pending_on_agent_id = target_agent_id,
                    pending_on_human_id = NULL,
                    next_action = ?,
                    ttl = ?,
                    max_turns = ?,
                    turn_count = 0,
                    goal_version = ?,
                    exchange_epoch = ?,
                    delivery_status = 'pending',
                    claimed_by = NULL,
                    claimed_at = NULL,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    normalized["new_done_criteria"],
                    next_action,
                    new_ttl,
                    new_max_turns,
                    next_goal_version,
                    next_exchange_epoch,
                    now,
                    task_id,
                ),
            )
            self.add_event_conn(
                conn,
                task_id,
                "task.amended",
                {
                    "protocol_version": normalized["protocol_version"],
                    "idempotency_key": normalized["idempotency_key"],
                    "actor_agent_id": actor_agent_id,
                    "requester_agent_id": task["requester_agent_id"],
                    "target_agent_id": task["target_agent_id"],
                    "previous_goal_version": current_goal_version,
                    "goal_version": next_goal_version,
                    "previous_exchange_epoch": task.get("exchange_epoch"),
                    "exchange_epoch": next_exchange_epoch,
                    "previous_done_criteria": previous_done_criteria,
                    "new_done_criteria": normalized["new_done_criteria_payload"],
                    "previous_goal_disposition": normalized["previous_goal_disposition"],
                    "previous_max_turns": previous_max_turns,
                    "new_max_turns": new_max_turns,
                    "previous_ttl": previous_ttl,
                    "ttl": new_ttl,
                    "pendingOnAgentId": task["target_agent_id"],
                    "pending_on_agent_id": task["target_agent_id"],
                    "nextAction": next_action,
                    "next_action": next_action,
                    "human_authority": normalized["human_authority"],
                    "reason": normalized["reason"],
                },
                now,
            )
            self.create_pending_agent_event_conn(conn, task_id, "task.amended", now)
            return self.get_task_conn(conn, task_id)

    def submit_artifact(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = int(time.time())
        normalized = normalize_artifact_submit(payload)
        actor_agent_id = normalized["actor_agent_id"]
        to_agent = normalized["to_agent_id"]
        artifact = normalized["artifact"]
        parts = artifact["parts"]
        artifact_id = artifact["artifact_id"]
        artifact_intent = artifact["intent"]
        source_refs = normalize_source_refs(artifact["source_refs"])
        artifact_summary = artifact["summary"]
        protocol_version = normalized["protocol_version"]
        idempotency_key = normalized["idempotency_key"]
        next_status = normalized["next_status"]
        pending_on_agent_id = normalized["pending_on_agent_id"]
        pending_on_human_id = normalized["pending_on_human_id"]
        next_action = normalized["next_action"]
        response_to_goal_version = normalized["response_to_goal_version"]
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            assert_legacy_mutation_allowed(task, "artifact submission")
            if response_to_goal_version is None:
                response_to_goal_version = int(task.get("goal_version") or 1)
            if not to_agent:
                to_agent = other_task_agent(task, actor_agent_id)
            if not pending_on_agent_id and actor_agent_id != task["completion_owner_agent_id"]:
                pending_on_agent_id = task["completion_owner_agent_id"]
            if not next_status:
                next_status = "delivery_pending" if pending_on_agent_id else "working"
            pending_on_agent_id = normalize_artifact_handoff(
                task,
                actor_agent_id,
                next_status,
                pending_on_agent_id,
            )
            if not next_action and pending_on_agent_id:
                next_action = "Requester agent should evaluate the artifact against done_criteria."
            assert_artifact_allowed(task, actor_agent_id, next_status, pending_on_agent_id, next_action)
            turn_count = next_turn_count(task, pending_on_agent_id)
            try:
                assert_max_turns(task, turn_count)
            except TransitionError as exc:
                if str(exc) != "task exceeded max_turns":
                    raise
                self.fail_task_max_turns_conn(
                    conn,
                    task,
                    now,
                    reason="artifact.max_turns_exceeded",
                    attempted_actor_agent_id=actor_agent_id,
                    attempted_pending_on_agent_id=pending_on_agent_id,
                    attempted_turn_count=turn_count,
                )
                conn.commit()
                raise ValueError(str(exc)) from exc
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, task_id, from_agent_id, to_agent_id,
                    kind, parts_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    task_id,
                    actor_agent_id,
                    to_agent,
                    artifact.get("kind") or "text",
                    json.dumps(parts),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE tasks
                SET status = ?,
                    pending_on_agent_id = ?,
                    pending_on_human_id = ?,
                    next_action = ?,
                    turn_count = ?,
                    delivery_status = CASE WHEN ? = 'delivery_pending' THEN 'pending' ELSE delivery_status END,
                    claimed_by = NULL,
                    claimed_at = NULL,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    next_status,
                    pending_on_agent_id,
                    pending_on_human_id,
                    next_action,
                    turn_count,
                    next_status,
                    now,
                    task_id,
                ),
            )
            self.add_event_conn(
                conn,
                task_id,
                "artifact.submitted",
                {
                    "protocol_version": protocol_version,
                    "idempotency_key": idempotency_key,
                    "artifactId": artifact_id,
                    "actor_agent_id": actor_agent_id,
                    "intent": artifact_intent,
                    "kind": artifact.get("kind"),
                    "summary": artifact_summary,
                    "source_refs": source_refs,
                    "target_agent_id": to_agent,
                    "requester_agent_id": task["requester_agent_id"],
                    "goal_version": task.get("goal_version"),
                    "exchange_epoch": task.get("exchange_epoch"),
                    "response_to_goal_version": response_to_goal_version,
                    "nextStatus": next_status,
                    "pendingOnAgentId": pending_on_agent_id,
                    "pending_on_agent_id": pending_on_agent_id,
                    "nextAction": next_action,
                },
                now,
            )
            self.add_event_conn(
                conn,
                task_id,
                "ownership.transferred",
                {
                    "protocol_version": protocol_version,
                    "actor_agent_id": actor_agent_id,
                    "pendingOnAgentId": pending_on_agent_id,
                    "pending_on_agent_id": pending_on_agent_id,
                },
                now,
            )
            self.create_pending_agent_event_conn(conn, task_id, "ownership.transferred", now)
            return self.get_task_conn(conn, task_id)

    def mark_delivery(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = int(time.time())
        delivered_by_agent_id = required(payload, "deliveredByAgentId")
        thread_id = required(payload, "threadId")
        delivery_status = payload.get("deliveryStatus") or "delivered"
        if delivery_status not in {"delivered", "failed"}:
            raise ValueError("deliveryStatus must be delivered or failed")
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            assert_legacy_mutation_allowed(task, "delivery mutation")
            assert_delivery_allowed(task, delivered_by_agent_id, thread_id)
            if delivery_status == "delivered":
                next_status = payload.get("nextStatus") or "waiting_human"
                pending_on_human_id = payload.get("pendingOnHumanId")
                next_action = payload.get("nextAction") or "Waiting for requester human confirmation."
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?,
                        delivery_status = 'delivered',
                        delivered_to_thread_id = ?,
                        delivered_at = ?,
                        delivery_error = NULL,
                        pending_on_agent_id = NULL,
                        pending_on_human_id = ?,
                        next_action = ?,
                        claimed_by = NULL,
                        claimed_at = NULL,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (next_status, thread_id, now, pending_on_human_id, next_action, now, task_id),
                )
                self.add_event_conn(
                    conn,
                    task_id,
                    "reply.delivered",
                    {
                        "deliveredByAgentId": delivered_by_agent_id,
                        "threadId": thread_id,
                        "nextStatus": next_status,
                        "pendingOnHumanId": pending_on_human_id,
                        "nextAction": next_action,
                    },
                    now,
                )
            else:
                error = payload.get("error") or "delivery failed"
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'delivery_pending',
                        delivery_status = 'failed',
                        delivered_to_thread_id = ?,
                        delivery_error = ?,
                        pending_on_agent_id = ?,
                        pending_on_human_id = NULL,
                        next_action = 'Retry delivery to requester thread.',
                        claimed_by = NULL,
                        claimed_at = NULL,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (thread_id, error, delivered_by_agent_id, now, task_id),
                )
                self.add_event_conn(
                    conn,
                    task_id,
                    "reply.delivery_failed",
                    {
                        "deliveredByAgentId": delivered_by_agent_id,
                        "threadId": thread_id,
                        "error": error,
                    },
                    now,
                )
                self.create_pending_agent_event_conn(conn, task_id, "reply.delivery_failed", now)
            return self.get_task_conn(conn, task_id)

    def close_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = int(time.time())
        closed_by_agent_id = read_alias(payload, "closed_by_agent_id", "closedByAgentId")
        if not closed_by_agent_id:
            raise ValueError("missing required field: closedByAgentId")
        terminal_reason = payload.get("terminalReason") or "requester closed task"
        protocol_version = payload.get("protocol_version") or PROTOCOL_VERSION
        completion_authority = normalize_completion_authority(payload.get("completion_authority"))
        final_artifact = normalize_final_artifact(payload.get("final_artifact"))
        idempotency_key = payload.get("idempotency_key")
        closed_against_goal_version = payload.get("closed_against_goal_version") or payload.get("closedAgainstGoalVersion")
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            assert_legacy_mutation_allowed(task, "legacy completion")
            if closed_against_goal_version is None:
                closed_against_goal_version = int(task.get("goal_version") or 1)
            assert_close_allowed(task, payload)
            evidence_ref = latest_artifact_source_ref_conn(conn, task_id)
            if evidence_ref and completion_authority is not None and not completion_authority.get("source_refs"):
                completion_authority = {
                    **completion_authority,
                    "source_refs": normalize_source_refs([evidence_ref]),
                }
            if evidence_ref and final_artifact is not None and not final_artifact.get("source_refs"):
                final_artifact = {
                    **final_artifact,
                    "source_refs": normalize_source_refs([evidence_ref], field="final_artifact.source_refs"),
                }
            conn.execute(
                """
                UPDATE tasks
                SET status = 'completed',
                    pending_on_agent_id = NULL,
                    pending_on_human_id = NULL,
                    next_action = NULL,
                    delivery_status = CASE
                        WHEN delivery_status = 'pending' THEN 'delivered'
                        ELSE delivery_status
                    END,
                    claimed_by = NULL,
                    claimed_at = NULL,
                    terminal_reason = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (terminal_reason, now, task_id),
            )
            self.add_event_conn(
                conn,
                task_id,
                "task.completed",
                {
                    "protocol_version": protocol_version,
                    "idempotency_key": idempotency_key,
                    "closedByAgentId": closed_by_agent_id,
                    "closed_by_agent_id": closed_by_agent_id,
                    "completion_authority": completion_authority,
                    "goal_version": task.get("goal_version"),
                    "exchange_epoch": task.get("exchange_epoch"),
                    "closed_against_goal_version": closed_against_goal_version,
                    "terminalReason": terminal_reason,
                    "terminal_reason": terminal_reason,
                    "final_artifact": final_artifact,
                },
                now,
            )
            self.cleanup_terminal_task_delivery_conn(conn, task_id, now)
            return self.get_task_conn(conn, task_id)

    def cleanup_terminal_task_delivery_conn(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        closed_at: int,
    ) -> None:
        conn.execute(
            """
            UPDATE agent_events
            SET delivery_state = 'done',
                acked_at = COALESCE(acked_at, ?),
                done_at = COALESCE(done_at, ?),
                failed_at = NULL,
                inflight_until = NULL,
                last_error = NULL
            WHERE task_id = ?
              AND acked_at IS NULL
              AND delivery_state IN ('pending', 'inflight', 'failed')
            """,
            (closed_at, closed_at, task_id),
        )

    def create_agent_event(
        self,
        agent_id: str,
        event_type: str,
        task_id: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            return self.create_agent_event_conn(
                conn,
                agent_id,
                event_type,
                task_id,
                payload,
                idempotency_key=idempotency_key,
            )

    def create_pending_agent_event_conn(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        reason: str,
        created_at: int | None = None,
    ) -> dict[str, Any] | None:
        task = self.get_task_conn(conn, task_id)
        if not task:
            return None
        agent_id = task.get("pending_on_agent_id")
        if not agent_id or task["status"] in TERMINAL_STATES:
            return None
        if is_exhausted_for_pending_agent(task):
            return None
        return self.create_agent_event_conn(
            conn,
            agent_id,
            "task.pending",
            task_id,
            pending_event_payload(task, reason),
            idempotency_key=f"{task_id}:{agent_id}:{reason}:{task.get('updated_at')}",
            created_at=created_at,
        )

    def create_agent_event_conn(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        event_type: str,
        task_id: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
        created_at: int | None = None,
    ) -> dict[str, Any]:
        assert_agent_exists(conn, agent_id)
        if not self.get_task_conn(conn, task_id):
            raise ValueError(f"unknown task: {task_id}")
        now = created_at or int(time.time())
        if idempotency_key:
            existing = conn.execute(
                """
                SELECT * FROM agent_events
                WHERE agent_id = ? AND idempotency_key = ?
                """,
                (agent_id, idempotency_key),
            ).fetchone()
            if existing:
                return decode_payload(existing)
        event_id = f"aevt_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO agent_events (
                event_id, agent_id, event_type, task_id, payload_json,
                idempotency_key, delivery_state, delivery_attempts,
                inflight_until, done_at, failed_at, last_error,
                acked_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL, NULL, ?)
            """,
            (event_id, agent_id, event_type, task_id, json.dumps(payload), idempotency_key, now),
        )
        row = conn.execute(
            "SELECT * FROM agent_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return decode_payload(row)

    def list_agent_events(
        self,
        agent_id: str,
        include_acked: bool = False,
        limit: int = 100,
        after_cursor: str | None = None,
        delivery_state: str | None = None,
    ) -> list[dict[str, Any]]:
        now = int(time.time())
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            assert_agent_exists(conn, agent_id)
            ack_filter = "" if include_acked else "AND acked_at IS NULL"
            state_filter = ""
            params: list[Any] = [agent_id]
            if delivery_state:
                if delivery_state not in AGENT_EVENT_DELIVERY_STATES:
                    raise ValueError(f"unknown delivery_state: {delivery_state}")
                state_filter = "AND delivery_state = ?"
                params.append(delivery_state)
            cursor_filter = ""
            if after_cursor:
                created_at, event_id = parse_agent_event_cursor(after_cursor)
                cursor_filter = "AND (created_at > ? OR (created_at = ? AND event_id > ?))"
                params.extend([created_at, created_at, event_id])
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT * FROM agent_events
                WHERE agent_id = ? {ack_filter} {state_filter} {cursor_filter}
                ORDER BY created_at, event_id
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [decode_payload(row) for row in rows]

    def claim_agent_events(
        self,
        agent_id: str,
        limit: int = 100,
        after_cursor: str | None = None,
        lease_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        now = int(time.time())
        lease_until = now + max(1, lease_seconds)
        with self.connect() as conn:
            self.expire_stale_tasks_conn(conn, now)
            assert_agent_exists(conn, agent_id)
            cursor_filter = ""
            params: list[Any] = [agent_id, now]
            if after_cursor:
                created_at, event_id = parse_agent_event_cursor(after_cursor)
                cursor_filter = "AND (created_at > ? OR (created_at = ? AND event_id > ?))"
                params.extend([created_at, created_at, event_id])
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT * FROM agent_events
                WHERE agent_id = ?
                  AND acked_at IS NULL
                  AND (
                    delivery_state IN ('pending', 'failed')
                    OR (delivery_state = 'inflight' AND COALESCE(inflight_until, 0) <= ?)
                  )
                  {cursor_filter}
                ORDER BY created_at, event_id
                LIMIT ?
                """,
                params,
            ).fetchall()
            event_ids = [row["event_id"] for row in rows]
            if not event_ids:
                return []
            placeholders = ", ".join("?" for _ in event_ids)
            conn.execute(
                f"""
                UPDATE agent_events
                SET delivery_state = 'inflight',
                    delivery_attempts = delivery_attempts + 1,
                    inflight_until = ?,
                    last_error = NULL
                WHERE agent_id = ?
                  AND event_id IN ({placeholders})
                  AND acked_at IS NULL
                """,
                (lease_until, agent_id, *event_ids),
            )
            refreshed = conn.execute(
                f"""
                SELECT * FROM agent_events
                WHERE agent_id = ?
                  AND event_id IN ({placeholders})
                ORDER BY created_at, event_id
                """,
                (agent_id, *event_ids),
            ).fetchall()
            return [decode_payload(row) for row in refreshed]

    def ack_agent_event(
        self,
        agent_id: str,
        event_id: str,
        expected_task_id: str | None = None,
        acked_at: int | None = None,
        delivery_state: str = "done",
        error: str | None = None,
    ) -> dict[str, Any] | None:
        now = acked_at or int(time.time())
        if delivery_state not in {"done", "failed"}:
            raise ValueError("delivery_state must be done or failed")
        with self.connect() as conn:
            assert_agent_exists(conn, agent_id)
            row = conn.execute(
                "SELECT * FROM agent_events WHERE agent_id = ? AND event_id = ?",
                (agent_id, event_id),
            ).fetchone()
            if not row:
                return None
            if expected_task_id and expected_task_id != row["task_id"]:
                raise ValueError("taskId does not match event")
            task = self.get_task_conn(conn, row["task_id"])
            if task and task.get("protocol_version") == PROTOCOL_V04 and row["can_transition_task"]:
                raise ConflictError(
                    "Protocol v0.4 current-message delivery requires the versioned Message ACK operation"
                )
            if row["acked_at"] is None and delivery_state == "done":
                conn.execute(
                    """
                    UPDATE agent_events
                    SET delivery_state = 'done',
                        acked_at = ?,
                        done_at = ?,
                        failed_at = NULL,
                        inflight_until = NULL,
                        last_error = NULL
                    WHERE agent_id = ? AND event_id = ?
                    """,
                    (now, now, agent_id, event_id),
                )
            elif row["acked_at"] is None and delivery_state == "failed":
                conn.execute(
                    """
                    UPDATE agent_events
                    SET delivery_state = 'failed',
                        failed_at = ?,
                        inflight_until = NULL,
                        last_error = ?
                    WHERE agent_id = ? AND event_id = ?
                    """,
                    (now, error or "delivery failed", agent_id, event_id),
                )
            row = conn.execute(
                "SELECT * FROM agent_events WHERE agent_id = ? AND event_id = ?",
                (agent_id, event_id),
            ).fetchone()
            return decode_payload(row)

    def upsert_thread_binding(
        self,
        task_id: str,
        agent_id: str,
        thread_id: str,
        thread_role: str = "agent_inbox",
        project_path: str | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self.connect() as conn:
            return self.upsert_thread_binding_conn(
                conn,
                task_id,
                agent_id,
                thread_id,
                thread_role,
                project_path,
                now,
            )

    def upsert_thread_binding_conn(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        agent_id: str,
        thread_id: str,
        thread_role: str = "agent_inbox",
        project_path: str | None = None,
        created_or_updated_at: int | None = None,
    ) -> dict[str, Any]:
        now = created_or_updated_at or int(time.time())
        assert_agent_exists(conn, agent_id)
        if not self.get_task_conn(conn, task_id):
            raise ValueError(f"unknown task: {task_id}")
        existing = conn.execute(
            """
            SELECT created_at FROM task_thread_bindings
            WHERE task_id = ? AND agent_id = ? AND thread_role = ?
            """,
            (task_id, agent_id, thread_role),
        ).fetchone()
        created_at = int(existing["created_at"]) if existing else now
        conn.execute(
            """
            INSERT OR REPLACE INTO task_thread_bindings (
                task_id, agent_id, thread_role, thread_id, project_path, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, agent_id, thread_role, thread_id, project_path, created_at, now),
        )
        row = conn.execute(
            """
            SELECT * FROM task_thread_bindings
            WHERE task_id = ? AND agent_id = ? AND thread_role = ?
            """,
            (task_id, agent_id, thread_role),
        ).fetchone()
        return dict(row)

    def list_thread_bindings(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if not self.get_task_conn(conn, task_id):
                return []
            rows = conn.execute(
                """
                SELECT * FROM task_thread_bindings
                WHERE task_id = ?
                ORDER BY agent_id, thread_role
                """,
                (task_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def add_event_conn(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: int | None = None,
    ) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(event_sequence), 0) + 1 AS next_sequence FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        event_sequence = int(row["next_sequence"] if row else 1)
        conn.execute(
            """
            INSERT INTO task_events (event_id, task_id, event_type, payload_json, event_sequence, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"evt_{uuid.uuid4().hex}",
                task_id,
                event_type,
                json.dumps(payload),
                event_sequence,
                created_at or int(time.time()),
            ),
        )


def required(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is None:
        raise ValueError(f"missing required field: {key}")
    return value


def request_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_alias(payload: dict[str, Any], snake_key: str, camel_key: str, default: Any = None) -> Any:
    if snake_key in payload:
        return payload.get(snake_key)
    if camel_key in payload:
        return payload.get(camel_key)
    return default


def normalize_task_create(payload: dict[str, Any]) -> dict[str, Any]:
    requester_agent_id = read_alias(payload, "requester_agent_id", "requesterAgentId", payload.get("from"))
    target_agent_id = read_alias(payload, "target_agent_id", "targetAgentId", payload.get("to"))
    if not requester_agent_id:
        raise ValueError("missing required field: requester_agent_id")
    if not target_agent_id:
        raise ValueError("missing required field: target_agent_id")

    message = required(payload, "message")
    if not isinstance(message, dict):
        raise ValueError("message must be an object")
    message_actor = read_alias(message, "actor_agent_id", "actorAgentId", requester_agent_id)
    if message_actor != requester_agent_id:
        raise ValueError("task create message actor_agent_id must match requester_agent_id")
    intent = message.get("intent") or role_to_intent(message.get("role"))
    parts = message.get("parts") or []
    if not isinstance(parts, list):
        raise ValueError("message.parts must be an array")

    done_criteria_payload = payload.get("doneCriteria") or payload.get("done_criteria") or ""
    done_criteria_storage = (
        json.dumps(done_criteria_payload, sort_keys=True)
        if isinstance(done_criteria_payload, dict)
        else str(done_criteria_payload)
    )
    thread_binding = payload.get("thread_binding") if isinstance(payload.get("thread_binding"), dict) else {}
    requester_thread_id = (
        payload.get("requesterThreadId")
        or payload.get("requester_thread_id")
        or (
            thread_binding.get("thread_id")
            if thread_binding.get("agent_id") == requester_agent_id
            and thread_binding.get("thread_role") == "requester_origin"
            else None
        )
    )

    completion_owner_agent_id = (
        payload.get("completionOwnerAgentId")
        or payload.get("completion_owner_agent_id")
        or requester_agent_id
    )
    if requester_agent_id != target_agent_id and completion_owner_agent_id == target_agent_id:
        completion_owner_agent_id = requester_agent_id

    return {
        "protocol_version": payload.get("protocol_version") or PROTOCOL_VERSION,
        "idempotency_key": payload.get("idempotency_key"),
        "task_id": payload.get("taskId") or payload.get("task_id") or f"task_{uuid.uuid4().hex}",
        "context_id": payload.get("contextId") or payload.get("context_id") or f"ctx_{uuid.uuid4().hex}",
        "requester_agent_id": requester_agent_id,
        "target_agent_id": target_agent_id,
        "message": {
            "message_id": message.get("messageId") or message.get("message_id") or f"msg_{uuid.uuid4().hex}",
            "actor_agent_id": message_actor,
            "intent": intent,
            "legacy_role": message.get("role") or "user",
            "parts": parts,
        },
        "subject": payload.get("subject") or "AgentRelay task",
        "requester_thread_id": requester_thread_id,
        "ttl": payload.get("ttl") or payload.get("expires_at") or payload.get("expiresAt"),
        "ttl_seconds": payload.get("ttl_seconds") or payload.get("ttlSeconds"),
        "max_turns": int(payload.get("maxTurns") or payload.get("max_turns") or 12),
        "done_criteria": done_criteria_storage,
        "done_criteria_payload": done_criteria_payload,
        "completion_owner_agent_id": completion_owner_agent_id,
        "pending_on_agent_id": (
            payload.get("pendingOnAgentId")
            or payload.get("pending_on_agent_id")
            or target_agent_id
        ),
        "pending_on_human_id": payload.get("pendingOnHumanId") or payload.get("pending_on_human_id"),
        "next_action": payload.get("nextAction") or payload.get("next_action"),
        "parent_task_id": payload.get("parentTaskId") or payload.get("parent_task_id"),
    }


def normalize_artifact_submit(payload: dict[str, Any]) -> dict[str, Any]:
    artifact = required(payload, "artifact")
    if not isinstance(artifact, dict):
        raise ValueError("artifact must be an object")
    actor_agent_id = (
        read_alias(payload, "actor_agent_id", "actorAgentId")
        or read_alias(artifact, "actor_agent_id", "actorAgentId")
        or payload.get("from")
    )
    if not actor_agent_id:
        raise ValueError("missing required field: actor_agent_id")
    to_agent_id = read_alias(payload, "target_agent_id", "targetAgentId", payload.get("to"))
    parts = artifact.get("parts") or []
    if not isinstance(parts, list):
        raise ValueError("artifact.parts must be an array")
    intent = artifact.get("intent") or payload.get("intent") or "work_result"
    return {
        "protocol_version": payload.get("protocol_version") or PROTOCOL_VERSION,
        "idempotency_key": payload.get("idempotency_key"),
        "actor_agent_id": actor_agent_id,
        "to_agent_id": to_agent_id,
        "next_status": payload.get("nextStatus") or payload.get("next_status"),
        "pending_on_agent_id": payload.get("pendingOnAgentId") or payload.get("pending_on_agent_id"),
        "pending_on_human_id": payload.get("pendingOnHumanId") or payload.get("pending_on_human_id"),
        "next_action": payload.get("nextAction") or payload.get("next_action"),
        "response_to_goal_version": payload.get("response_to_goal_version")
        or payload.get("responseToGoalVersion")
        or artifact.get("response_to_goal_version")
        or artifact.get("responseToGoalVersion"),
        "artifact": {
            "artifact_id": artifact.get("artifactId") or artifact.get("artifact_id") or f"art_{uuid.uuid4().hex}",
            "intent": intent,
            "kind": artifact.get("kind") or "text",
            "parts": parts,
            "summary": artifact.get("summary"),
            "source_refs": artifact.get("source_refs") or [],
        },
    }


def normalize_task_amend(payload: dict[str, Any], *, now: int) -> dict[str, Any]:
    actor_agent_id = read_alias(payload, "actor_agent_id", "actorAgentId")
    if not actor_agent_id:
        raise ValueError("missing required field: actor_agent_id")
    expected_goal_version = parse_positive_int(
        payload.get("expected_goal_version") or payload.get("expectedGoalVersion"),
        "expected_goal_version",
    )
    new_done_criteria_payload = payload.get("new_done_criteria")
    if new_done_criteria_payload is None:
        new_done_criteria_payload = payload.get("newDoneCriteria")
    if new_done_criteria_payload is None:
        raise ValueError("missing required field: new_done_criteria")
    new_done_criteria_storage = (
        json.dumps(new_done_criteria_payload, sort_keys=True)
        if isinstance(new_done_criteria_payload, dict)
        else str(new_done_criteria_payload)
    )
    if not new_done_criteria_storage.strip():
        raise ValueError("new_done_criteria must not be empty")
    disposition = payload.get("previous_goal_disposition") or payload.get("previousGoalDisposition") or "clarified"
    if disposition not in PREVIOUS_GOAL_DISPOSITIONS:
        raise ValueError("previous_goal_disposition is not supported")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("missing required field: reason")
    human_authority = normalize_human_authority(required(payload, "human_authority"))
    new_max_turns = payload.get("new_max_turns") or payload.get("newMaxTurns")
    return {
        "protocol_version": payload.get("protocol_version") or PROTOCOL_VERSION,
        "idempotency_key": payload.get("idempotency_key"),
        "actor_agent_id": actor_agent_id,
        "expected_goal_version": expected_goal_version,
        "new_done_criteria": new_done_criteria_storage,
        "new_done_criteria_payload": new_done_criteria_payload,
        "new_max_turns": parse_positive_int(new_max_turns, "new_max_turns") if new_max_turns is not None else None,
        "ttl": normalize_task_ttl(
            payload.get("ttl") or payload.get("expires_at") or payload.get("expiresAt"),
            ttl_seconds=payload.get("ttl_seconds") or payload.get("ttlSeconds"),
            now=now,
        ),
        "previous_goal_disposition": disposition,
        "human_authority": human_authority,
        "reason": reason.strip(),
        "next_action": payload.get("next_action") or payload.get("nextAction"),
    }


def normalize_artifact_handoff(
    task: dict[str, Any],
    actor_agent_id: str,
    next_status: str | None,
    pending_on_agent_id: str | None,
) -> str | None:
    if (
        next_status == "delivery_pending"
        and pending_on_agent_id == actor_agent_id
        and actor_agent_id != task.get("requester_agent_id")
    ):
        return task.get("requester_agent_id")
    return pending_on_agent_id


def normalize_final_artifact(final_artifact: Any) -> dict[str, Any] | None:
    if final_artifact is None:
        return None
    if not isinstance(final_artifact, dict):
        raise ValueError("final_artifact must be an object")
    normalized = dict(final_artifact)
    normalized["source_refs"] = normalize_source_refs(
        final_artifact.get("source_refs", []),
        field="final_artifact.source_refs",
    )
    return normalized


def role_to_intent(role: Any) -> str:
    if role == "agent" or role == "ROLE_AGENT":
        return "agent_message"
    if role == "system" or role == "ROLE_SYSTEM":
        return "system_context"
    return "request"


def other_task_agent(task: dict[str, Any], actor_agent_id: str) -> str:
    if actor_agent_id == task.get("requester_agent_id"):
        return task["target_agent_id"]
    return task["requester_agent_id"]


def assert_agent_exists(conn: sqlite3.Connection, agent_id: str) -> None:
    row = conn.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown agent: {agent_id}")


def ensure_agent_conn(conn: sqlite3.Connection, agent_id: str, owner: str, created_at: int) -> None:
    role = normalize_agent_role(None, agent_id)
    execution_mode = normalize_execution_mode(None, role)
    conn.execute(
        """
        INSERT OR IGNORE INTO agents
            (agent_id, name, owner, description, agent_role, execution_mode, capabilities_json, policy_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            agent_id,
            f"{owner} Agent",
            owner,
            f"Personal coordinator agent for {owner}.",
            role,
            execution_mode,
            json.dumps(default_agent_capabilities(role)),
            json.dumps(default_agent_policy(role)),
            created_at,
        ),
    )


def ensure_healthcheck_agent_conn(conn: sqlite3.Connection, created_at: int) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO agents
            (agent_id, name, owner, description, agent_role, execution_mode, capabilities_json, policy_json, created_at)
        VALUES (?, 'AgentRelay Healthcheck', 'AgentRelay', ?, 'service_agent', 'autonomous', ?, ?, ?)
        """,
        (
            HEALTHCHECK_AGENT_ID,
            "Built-in synthetic actor for install loopback health checks. It has no login token and does not run a remote agent.",
            json.dumps(DEFAULT_SERVICE_CAPABILITIES),
            json.dumps(DEFAULT_SERVICE_POLICY),
            created_at,
        ),
    )


def install_healthcheck_task_id(requester_agent_id: str, idempotency_key: str | None) -> str:
    if not idempotency_key:
        return f"task_{uuid.uuid4().hex}"
    digest = hashlib.sha256(f"{requester_agent_id}:{idempotency_key}".encode("utf-8")).hexdigest()[:32]
    return f"task_hc_{digest}"


def normalize_agent_role(value: str | None, agent_id: str = "") -> str:
    if agent_id in {HEALTHCHECK_AGENT_ID, "project-hermes"}:
        return "service_agent"
    role = str(value or "personal_agent").strip()
    if role not in AGENT_ROLES:
        raise ValueError(f"invalid agent_role: {role}")
    return role


def normalize_execution_mode(value: str | None, agent_role: str) -> str:
    default = "autonomous" if agent_role == "service_agent" else "notify_only"
    execution_mode = str(value or default).strip()
    if execution_mode not in EXECUTION_MODES:
        raise ValueError(f"invalid execution_mode: {execution_mode}")
    return execution_mode


def default_agent_capabilities(agent_role: str) -> list[str]:
    if agent_role == "service_agent":
        return list(DEFAULT_SERVICE_CAPABILITIES)
    return list(DEFAULT_PERSONAL_CAPABILITIES)


def default_agent_policy(agent_role: str) -> dict[str, Any]:
    if agent_role == "service_agent":
        return dict(DEFAULT_SERVICE_POLICY)
    return dict(DEFAULT_PERSONAL_POLICY)


def parse_json_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return [str(item) for item in parsed if str(item).strip()]


def parse_json_object(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def install_healthcheck_ack_text(requester_agent_id: str, task_id: str) -> str:
    return "\n".join(
        [
            f"ACK from {HEALTHCHECK_AGENT_ID}",
            f"requester={requester_agent_id}",
            f"task={task_id}",
            "scope=agentrelay-install-loopback",
        ]
    )


def latest_agent_event_conn(conn: sqlite3.Connection, agent_id: str, task_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM agent_events
        WHERE agent_id = ? AND task_id = ?
        ORDER BY created_at DESC, event_id DESC
        LIMIT 1
        """,
        (agent_id, task_id),
    ).fetchone()
    return decode_payload(row) if row else None


def latest_artifact_source_ref_conn(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT artifact_id, from_agent_id, kind, created_at
        FROM artifacts
        WHERE task_id = ?
        ORDER BY created_at DESC, artifact_id DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "type": "tool_result",
        "label": f"Latest artifact from {row['from_agent_id']}",
        "summary": f"Task completion was evaluated against artifact {row['artifact_id']} ({row['kind']}).",
        "visibility": "redacted",
        "uri": f"agentrelay://tasks/{task_id}/artifacts/{row['artifact_id']}",
    }


def summarize_task(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    return {
        "taskId": data["task_id"],
        "contextId": data["context_id"],
        "subject": data["subject"],
        "status": data["status"],
        "requesterAgentId": data["requester_agent_id"],
        "targetAgentId": data["target_agent_id"],
        "pendingOnAgentId": data["pending_on_agent_id"],
        "pendingOnHumanId": data["pending_on_human_id"],
        "nextAction": data["next_action"],
        "goalVersion": data.get("goal_version"),
        "exchangeEpoch": data.get("exchange_epoch"),
        "turnCount": data.get("turn_count"),
        "maxTurns": data.get("max_turns"),
        "claimedBy": data["claimed_by"],
        "claimedAt": data["claimed_at"],
        "requesterThreadId": data["requester_thread_id"],
        "updatedAt": data["updated_at"],
        "createdAt": data["created_at"],
    }


def pending_event_payload(task: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "type": "task.pending",
        "taskId": task["task_id"],
        "contextId": task["context_id"],
        "status": task["status"],
        "agentId": task["pending_on_agent_id"],
        "pendingOnAgentId": task["pending_on_agent_id"],
        "goalVersion": task.get("goal_version"),
        "goal_version": task.get("goal_version"),
        "exchangeEpoch": task.get("exchange_epoch"),
        "exchange_epoch": task.get("exchange_epoch"),
        "updatedAt": task["updated_at"],
        "reason": reason,
        "payloadRef": {
            "method": "GET",
            "href": f"/agentrelay/tasks/{task['task_id']}",
        },
    }


def expired_task_event_payload(task: dict[str, Any], expired_at: int) -> dict[str, Any]:
    return {
        "type": "task.pending",
        "taskId": task["task_id"],
        "contextId": task["context_id"],
        "status": "expired",
        "agentId": task["requester_agent_id"],
        "pendingOnAgentId": task["requester_agent_id"],
        "updatedAt": task["updated_at"],
        "reason": "task.ttl_expired",
        "expiredAt": expired_at,
        "ttl": task.get("ttl"),
        "payloadRef": {
            "method": "GET",
            "href": f"/agentrelay/tasks/{task['task_id']}",
        },
    }


def failed_task_event_payload(
    task: dict[str, Any],
    failed_at: int,
    *,
    reason: str,
    previous_pending_on_agent_id: str | None,
    attempted_actor_agent_id: str | None = None,
    attempted_pending_on_agent_id: str | None = None,
    attempted_turn_count: int | None = None,
) -> dict[str, Any]:
    requester_agent_id = task["requester_agent_id"]
    return {
        "type": "task.pending",
        "taskId": task["task_id"],
        "contextId": task["context_id"],
        "status": "failed",
        "agentId": requester_agent_id,
        "pendingOnAgentId": requester_agent_id,
        "updatedAt": task["updated_at"],
        "reason": reason,
        "terminalReason": task.get("terminal_reason"),
        "terminal_reason": task.get("terminal_reason"),
        "failedAt": failed_at,
        "failed_at": failed_at,
        "previousPendingOnAgentId": previous_pending_on_agent_id,
        "previous_pending_on_agent_id": previous_pending_on_agent_id,
        "turnCount": task.get("turn_count"),
        "turn_count": task.get("turn_count"),
        "maxTurns": task.get("max_turns"),
        "max_turns": task.get("max_turns"),
        "attemptedActorAgentId": attempted_actor_agent_id,
        "attempted_actor_agent_id": attempted_actor_agent_id,
        "attemptedPendingOnAgentId": attempted_pending_on_agent_id,
        "attempted_pending_on_agent_id": attempted_pending_on_agent_id,
        "attemptedTurnCount": attempted_turn_count,
        "attempted_turn_count": attempted_turn_count,
        "payloadRef": {
            "method": "GET",
            "href": f"/agentrelay/tasks/{task['task_id']}",
        },
    }


def normalize_task_ttl(ttl: Any, *, ttl_seconds: Any = None, now: int) -> int:
    if ttl_seconds is not None:
        seconds = parse_positive_int(ttl_seconds, "ttl_seconds")
        return now + seconds
    if ttl is None or ttl == "":
        return now + DEFAULT_TASK_TTL_SECONDS
    if isinstance(ttl, (int, float)):
        value = int(ttl)
        if value <= 0:
            raise ValueError("ttl must be positive")
        return value if value > 1_000_000_000 else now + value
    if isinstance(ttl, str):
        stripped = ttl.strip()
        if not stripped:
            return now + DEFAULT_TASK_TTL_SECONDS
        if stripped.isdigit():
            value = int(stripped)
            return value if value > 1_000_000_000 else now + value
        try:
            normalized = stripped.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError as exc:
            raise ValueError("ttl must be epoch seconds, ttl seconds, or ISO datetime") from exc
    raise ValueError("ttl must be epoch seconds, ttl seconds, or ISO datetime")


def parse_positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def decode_parts(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["parts"] = json.loads(data.pop("parts_json"))
    return data


def decode_payload(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data.pop("_rowid", None)
    data["payload"] = json.loads(data.pop("payload_json"))
    if "created_at" in data and "event_id" in data:
        data["cursor"] = agent_event_cursor(data["created_at"], data["event_id"])
    return data


def agent_event_cursor(created_at: int, event_id: str) -> str:
    return f"{int(created_at)}:{event_id}"


def parse_agent_event_cursor(cursor: str) -> tuple[int, str]:
    try:
        created_at_text, event_id = cursor.split(":", 1)
        created_at = int(created_at_text)
    except (AttributeError, ValueError) as exc:
        raise ValueError("invalid event cursor") from exc
    if not event_id:
        raise ValueError("invalid event cursor")
    return created_at, event_id


def summarize_timeline(entries: list[dict[str, Any]]) -> dict[str, Any]:
    categories: dict[str, int] = {}
    for entry in entries:
        category = entry.get("category") or "event"
        categories[category] = categories.get(category, 0) + 1
    return {
        "total_entries": len(entries),
        "categories": categories,
        "last_event_type": entries[-1]["event_type"] if entries else None,
        "last_created_at": entries[-1]["created_at"] if entries else None,
    }
