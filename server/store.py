from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


TERMINAL_STATES = {"completed", "failed", "cancelled", "expired", "rejected"}


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
                """
            )
            self.ensure_seed_agents(conn)

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

        with self.connect() as conn:
            assert_agent_exists(conn, from_agent)
            assert_agent_exists(conn, to_agent)
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, context_id, status, requester_agent_id, target_agent_id,
                    requester_thread_id, requester_thread_policy, target_thread_policy,
                    subject, ttl, max_turns, created_at, updated_at
                )
                VALUES (?, ?, 'submitted', ?, ?, ?, 'reuse-origin-thread',
                    'reuse-task-thread', ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    context_id,
                    from_agent,
                    to_agent,
                    requester_thread_id,
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
        task["messages"] = [decode_parts(row) for row in messages]
        task["artifacts"] = [decode_parts(row) for row in artifacts]
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
        with self.connect() as conn:
            assert_agent_exists(conn, agent_id)
            row = conn.execute(
                """
                SELECT task_id FROM tasks
                WHERE target_agent_id = ?
                  AND status IN ('submitted', 'input_required', 'auth_required')
                  AND (claimed_by IS NULL OR claimed_by = ?)
                ORDER BY created_at
                LIMIT 1
                """,
                (agent_id, agent_id),
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
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (status, now, task_id),
            )
            self.add_event_conn(conn, task_id, "task.status_updated", {"status": status, **payload}, now)
            return self.get_task_conn(conn, task_id)

    def submit_artifact(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = int(time.time())
        from_agent = required(payload, "from")
        to_agent = required(payload, "to")
        artifact = required(payload, "artifact")
        parts = artifact.get("parts") or []
        artifact_id = artifact.get("artifactId") or f"art_{uuid.uuid4().hex}"
        with self.connect() as conn:
            task = self.get_task_conn(conn, task_id)
            if not task:
                return None
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
                "UPDATE tasks SET status = 'completed', updated_at = ? WHERE task_id = ?",
                (now, task_id),
            )
            self.add_event_conn(
                conn,
                task_id,
                "artifact.submitted",
                {"artifactId": artifact_id, "from": from_agent, "to": to_agent},
                now,
            )
            self.add_event_conn(conn, task_id, "task.completed", {"artifactId": artifact_id}, now)
            return self.get_task_conn(conn, task_id)

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

