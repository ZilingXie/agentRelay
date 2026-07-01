from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from server.protocol_v03 import normalize_completion_authority, normalize_source_refs
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
    next_turn_count,
)


PROTOCOL_VERSION = "agent-collab-v0.2"
AGENT_EVENT_DELIVERY_STATES = {"pending", "inflight", "done", "failed"}


class ConflictError(Exception):
    """Raised when a task exists but cannot perform the requested state transition."""


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
            self.ensure_task_columns(conn)
            self.ensure_agent_event_columns(conn)
            self.ensure_seed_agents(conn)

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
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)

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
            ("zac-agent", "Zac Agent", "Zac", "Personal coordinator agent for Zac."),
            ("frank-agent", "Frank Agent", "Frank", "Personal coordinator agent for Frank."),
        ]
        for agent_id, name, owner, description in agents:
            conn.execute(
                """
                INSERT OR IGNORE INTO agents
                    (agent_id, name, owner, description, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (agent_id, name, owner, description, now),
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
    ) -> dict[str, Any]:
        now = int(time.time())
        agent_name = name or f"{owner} Agent"
        agent_description = description or f"Personal coordinator agent for {owner}."
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            created_at = int(existing["created_at"]) if existing else now
            conn.execute(
                """
                INSERT OR REPLACE INTO agents
                    (agent_id, name, owner, description, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (agent_id, agent_name, owner, agent_description, created_at),
            )
            row = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            return dict(row)

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
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
        ttl = normalized["ttl"]
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
                    "pendingOnAgentId": pending_on_agent_id,
                    "pending_on_agent_id": pending_on_agent_id,
                    "next_action": next_action,
                },
                now,
            )
            self.create_pending_agent_event_conn(conn, task_id, "task.created", now)
            return self.get_task_conn(conn, task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
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

    def get_events(self, task_id: str) -> list[dict[str, Any]] | None:
        with self.connect() as conn:
            if not self.get_task_conn(conn, task_id):
                return None
            rows = conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at, event_id",
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
        with self.connect() as conn:
            assert_agent_exists(conn, agent_id)
            rows = conn.execute(
                f"""
                SELECT * FROM tasks
                WHERE pending_on_agent_id = ?
                  AND (
                    status IN ({claimable_placeholders})
                    OR (status = 'claimed' AND claimed_by = ?)
                  )
                  AND (claimed_by IS NULL OR claimed_by = ?)
                ORDER BY updated_at, created_at, task_id
                LIMIT ?
                """,
                (agent_id, *sorted(CLAIMABLE_STATES), agent_id, agent_id, limit),
            ).fetchall()
            return [summarize_task(row) for row in rows]

    def claim_task(self, agent_id: str) -> dict[str, Any] | None:
        now = int(time.time())
        claimable_placeholders = ", ".join("?" for _ in CLAIMABLE_STATES)
        with self.connect() as conn:
            assert_agent_exists(conn, agent_id)
            row = conn.execute(
                f"""
                SELECT task_id FROM tasks
                WHERE pending_on_agent_id = ?
                  AND status IN ({claimable_placeholders})
                  AND (claimed_by IS NULL OR claimed_by = ?)
                ORDER BY created_at
                LIMIT 1
                """,
                (agent_id, *sorted(CLAIMABLE_STATES), agent_id),
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
            assert_agent_exists(conn, agent_id)
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
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
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
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
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            assert_update_status_allowed(task, status, payload)
            terminal_reason = payload.get("terminalReason")
            next_action = payload.get("nextAction")
            pending_on_agent_id = read_alias(payload, "pending_on_agent_id", "pendingOnAgentId")
            pending_on_human_id = payload.get("pendingOnHumanId")
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (status, now, task_id),
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
        with self.connect() as conn:
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            if not to_agent:
                to_agent = other_task_agent(task, actor_agent_id)
            if not pending_on_agent_id and actor_agent_id != task["completion_owner_agent_id"]:
                pending_on_agent_id = task["completion_owner_agent_id"]
            if not next_status:
                next_status = "delivery_pending" if pending_on_agent_id else "working"
            if not next_action and pending_on_agent_id:
                next_action = "Requester agent should evaluate the artifact against done_criteria."
            assert_artifact_allowed(task, actor_agent_id, next_status, pending_on_agent_id, next_action)
            turn_count = next_turn_count(task, pending_on_agent_id)
            assert_max_turns(task, turn_count)
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
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
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
        closed_by_agent_id = required(payload, "closedByAgentId")
        terminal_reason = payload.get("terminalReason") or "requester closed task"
        protocol_version = payload.get("protocol_version") or PROTOCOL_VERSION
        completion_authority = normalize_completion_authority(payload.get("completion_authority"))
        final_artifact = normalize_final_artifact(payload.get("final_artifact"))
        idempotency_key = payload.get("idempotency_key")
        with self.connect() as conn:
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            assert_close_allowed(task, payload)
            conn.execute(
                """
                UPDATE tasks
                SET status = 'completed',
                    pending_on_agent_id = NULL,
                    pending_on_human_id = NULL,
                    next_action = NULL,
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
                    "terminalReason": terminal_reason,
                    "terminal_reason": terminal_reason,
                    "final_artifact": final_artifact,
                },
                now,
            )
            return self.get_task_conn(conn, task_id)

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
        with self.connect() as conn:
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
        conn.execute(
            """
            INSERT INTO task_events (event_id, task_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                f"evt_{uuid.uuid4().hex}",
                task_id,
                event_type,
                json.dumps(payload),
                created_at or int(time.time()),
            ),
        )


def required(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is None:
        raise ValueError(f"missing required field: {key}")
    return value


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
        "ttl": payload.get("ttl"),
        "max_turns": int(payload.get("maxTurns") or payload.get("max_turns") or 12),
        "done_criteria": done_criteria_storage,
        "done_criteria_payload": done_criteria_payload,
        "completion_owner_agent_id": (
            payload.get("completionOwnerAgentId")
            or payload.get("completion_owner_agent_id")
            or requester_agent_id
        ),
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
        "artifact": {
            "artifact_id": artifact.get("artifactId") or artifact.get("artifact_id") or f"art_{uuid.uuid4().hex}",
            "intent": intent,
            "kind": artifact.get("kind") or "text",
            "parts": parts,
            "summary": artifact.get("summary"),
            "source_refs": artifact.get("source_refs") or [],
        },
    }


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
        "updatedAt": task["updated_at"],
        "reason": reason,
        "payloadRef": {
            "method": "GET",
            "href": f"/agentrelay/tasks/{task['task_id']}",
        },
    }


def decode_parts(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["parts"] = json.loads(data.pop("parts_json"))
    return data


def decode_payload(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
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
