from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


TERMINAL_STATES = {"completed", "failed", "cancelled", "expired", "rejected"}
CLAIMABLE_STATES = {
    "submitted",
    "input_required",
    "auth_required",
    "waiting_remote",
    "delivery_pending",
    "artifact_submitted",
}


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
        task_id = payload.get("taskId") or f"task_{uuid.uuid4().hex}"
        context_id = payload.get("contextId") or f"ctx_{uuid.uuid4().hex}"
        from_agent = required(payload, "from")
        to_agent = required(payload, "to")
        message = required(payload, "message")
        subject = payload.get("subject") or "AgentRelay task"
        parts = message.get("parts") or []
        requester_thread_id = payload.get("requesterThreadId")
        ttl = payload.get("ttl")
        max_turns = int(payload.get("maxTurns") or 12)
        done_criteria = payload.get("doneCriteria") or ""
        completion_owner_agent_id = payload.get("completionOwnerAgentId") or from_agent
        pending_on_agent_id = payload.get("pendingOnAgentId") or to_agent
        pending_on_human_id = payload.get("pendingOnHumanId")
        next_action = payload.get("nextAction")
        parent_task_id = payload.get("parentTaskId")

        with self.connect() as conn:
            assert_agent_exists(conn, from_agent)
            assert_agent_exists(conn, to_agent)
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
                    from_agent,
                    to_agent,
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
            message_id = message.get("messageId") or f"msg_{uuid.uuid4().hex}"
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
                    from_agent,
                    to_agent,
                    message.get("role") or "user",
                    json.dumps(parts),
                    now,
                ),
            )
            self.add_event_conn(
                conn,
                task_id,
                "task.created",
                {
                    "contextId": context_id,
                    "from": from_agent,
                    "to": to_agent,
                    "requesterThreadId": requester_thread_id,
                    "doneCriteria": done_criteria,
                    "completionOwnerAgentId": completion_owner_agent_id,
                    "pendingOnAgentId": pending_on_agent_id,
                },
                now,
            )
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

    def set_thread(self, agent_id: str, task_id: str, thread_id: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self.connect() as conn:
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            if task["target_agent_id"] != agent_id:
                raise ValueError("agent is not the task target")
            existing_thread_id = task.get("target_thread_id")
            event_type = "thread.reused" if existing_thread_id else "thread.created"
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
            if not self.get_task_conn(conn, task_id):
                return None
            terminal_reason = payload.get("terminalReason")
            next_action = payload.get("nextAction")
            pending_on_agent_id = payload.get("pendingOnAgentId")
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
            return self.get_task_conn(conn, task_id)

    def submit_artifact(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = int(time.time())
        from_agent = required(payload, "from")
        to_agent = required(payload, "to")
        artifact = required(payload, "artifact")
        parts = artifact.get("parts") or []
        artifact_id = artifact.get("artifactId") or f"art_{uuid.uuid4().hex}"
        next_status = payload.get("nextStatus")
        pending_on_agent_id = payload.get("pendingOnAgentId")
        pending_on_human_id = payload.get("pendingOnHumanId")
        next_action = payload.get("nextAction")
        with self.connect() as conn:
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            if not pending_on_agent_id and from_agent != task["completion_owner_agent_id"]:
                pending_on_agent_id = task["completion_owner_agent_id"]
            if not next_status:
                next_status = "delivery_pending" if pending_on_agent_id else "working"
            if not next_action and pending_on_agent_id:
                next_action = "Requester agent should evaluate the artifact against done_criteria."
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
                    from_agent,
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
                    "artifactId": artifact_id,
                    "from": from_agent,
                    "to": to_agent,
                    "nextStatus": next_status,
                    "pendingOnAgentId": pending_on_agent_id,
                    "pendingOnHumanId": pending_on_human_id,
                    "nextAction": next_action,
                },
                now,
            )
            self.add_event_conn(
                conn,
                task_id,
                "ownership.transferred",
                {
                    "from": from_agent,
                    "pendingOnAgentId": pending_on_agent_id,
                    "pendingOnHumanId": pending_on_human_id,
                },
                now,
            )
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
            if delivered_by_agent_id != task["completion_owner_agent_id"]:
                raise ValueError("only completion_owner_agent_id can mark requester delivery")
            if thread_id != task["requester_thread_id"]:
                raise ValueError("delivery threadId must match requester_thread_id")
            if delivery_status == "delivered":
                next_status = payload.get("nextStatus") or "waiting_human"
                pending_on_human_id = payload.get("pendingOnHumanId") or "zac"
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
            return self.get_task_conn(conn, task_id)

    def close_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = int(time.time())
        closed_by_agent_id = required(payload, "closedByAgentId")
        terminal_reason = payload.get("terminalReason") or "requester closed task"
        with self.connect() as conn:
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
            if closed_by_agent_id != task["completion_owner_agent_id"]:
                raise ValueError("only completion_owner_agent_id can close the task")
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
                {"closedByAgentId": closed_by_agent_id, "terminalReason": terminal_reason},
                now,
            )
            return self.get_task_conn(conn, task_id)

    def create_agent_event(
        self,
        agent_id: str,
        event_type: str,
        task_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self.connect() as conn:
            return self.create_agent_event_conn(conn, agent_id, event_type, task_id, payload)

    def create_agent_event_conn(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        event_type: str,
        task_id: str,
        payload: dict[str, Any],
        created_at: int | None = None,
    ) -> dict[str, Any]:
        assert_agent_exists(conn, agent_id)
        if not self.get_task_conn(conn, task_id):
            raise ValueError(f"unknown task: {task_id}")
        now = created_at or int(time.time())
        event_id = f"aevt_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO agent_events (
                event_id, agent_id, event_type, task_id, payload_json, acked_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?)
            """,
            (event_id, agent_id, event_type, task_id, json.dumps(payload), now),
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
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            assert_agent_exists(conn, agent_id)
            ack_filter = "" if include_acked else "AND acked_at IS NULL"
            rows = conn.execute(
                f"""
                SELECT * FROM agent_events
                WHERE agent_id = ? {ack_filter}
                ORDER BY created_at, event_id
                LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
            return [decode_payload(row) for row in rows]

    def ack_agent_event(
        self,
        agent_id: str,
        event_id: str,
        acked_at: int | None = None,
    ) -> dict[str, Any] | None:
        now = acked_at or int(time.time())
        with self.connect() as conn:
            assert_agent_exists(conn, agent_id)
            row = conn.execute(
                "SELECT * FROM agent_events WHERE agent_id = ? AND event_id = ?",
                (agent_id, event_id),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE agent_events SET acked_at = ? WHERE agent_id = ? AND event_id = ?",
                (now, agent_id, event_id),
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


def assert_agent_exists(conn: sqlite3.Connection, agent_id: str) -> None:
    row = conn.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown agent: {agent_id}")


def decode_parts(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["parts"] = json.loads(data.pop("parts_json"))
    return data


def decode_payload(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json"))
    return data
