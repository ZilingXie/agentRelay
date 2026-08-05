from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.protocol_v06 import PROTOCOL_V06
from server.store_v06 import V06Store


TABLES = (
    "agents",
    "agent_listener_readiness",
    "tasks",
    "messages",
    "agent_events",
    "task_audit_events",
    "idempotency_records",
)
PARKED_SOURCE_STATUSES = {"queued", "inflight", "retry_wait"}


def migrate_v05_to_v06(
    source_path: str,
    destination_path: str,
    *,
    enabled_v06_agents: set[str],
) -> dict[str, Any]:
    source = Path(source_path)
    destination = Path(destination_path)
    if not source.is_file():
        raise ValueError(f"v0.5 source database does not exist: {source}")
    if destination.exists():
        raise ValueError(f"refusing to overwrite destination database: {destination}")

    source_conn = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    source_conn.row_factory = sqlite3.Row
    source_conn.execute("PRAGMA foreign_keys = ON")
    source_conn.execute("BEGIN")
    try:
        _validate_source(source_conn, enabled_v06_agents)
        source_counts = _counts(source_conn)
        source_event_statuses = _group_counts(source_conn, "agent_events", "outbox_status")
        V06Store(str(destination))
        destination_conn = sqlite3.connect(destination)
        destination_conn.row_factory = sqlite3.Row
        destination_conn.execute("PRAGMA foreign_keys = ON")
        try:
            destination_conn.execute("BEGIN IMMEDIATE")
            _copy_agents(source_conn, destination_conn, enabled_v06_agents)
            _copy_tasks(source_conn, destination_conn)
            _copy_table(source_conn, destination_conn, "messages")
            _copy_events(source_conn, destination_conn)
            _copy_table(source_conn, destination_conn, "task_audit_events")
            _copy_table(source_conn, destination_conn, "idempotency_records")
            _validate_destination(
                destination_conn,
                source_counts=source_counts,
                source_event_statuses=source_event_statuses,
                enabled_v06_agents=enabled_v06_agents,
            )
            destination_conn.commit()
        finally:
            destination_conn.close()
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        source_conn.rollback()
        source_conn.close()

    return {
        "source": str(source.resolve()),
        "destination": str(destination.resolve()),
        "counts": {**source_counts, "agent_listener_readiness": 0},
        "enabled_v06_agents": sorted(enabled_v06_agents),
        "parked_events": sum(source_event_statuses.get(status, 0) for status in PARKED_SOURCE_STATUSES),
    }


def _validate_source(conn: sqlite3.Connection, enabled_v06_agents: set[str]) -> None:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    missing = sorted(set(TABLES) - tables)
    if missing:
        raise ValueError(f"source database is missing tables: {missing}")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("source database quick_check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("source database foreign_key_check failed")
    non_v05 = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE protocol_version != 'agent-collab-v0.5'"
    ).fetchone()[0]
    if non_v05:
        raise ValueError("source database contains non-v0.5 Tasks")
    unreachable = conn.execute(
        """
        SELECT COUNT(*) FROM agent_events e
        JOIN tasks t ON t.task_id = e.task_id
        WHERE t.status != 'open' AND e.can_transition_message = 1
          AND e.outbox_status IN ('queued', 'inflight', 'retry_wait')
        """
    ).fetchone()[0]
    if unreachable:
        raise ValueError("source contains recoverable transition Events for terminal Tasks")
    enabled_agents = {
        row[0] for row in conn.execute("SELECT agent_id FROM agents WHERE enabled = 1")
    }
    if enabled_agents != enabled_v06_agents:
        missing_agents = sorted(enabled_agents - enabled_v06_agents)
        unknown_agents = sorted(enabled_v06_agents - enabled_agents)
        raise ValueError(
            "enabled v0.6 Agent set must exactly match enabled source Agents; "
            f"missing={missing_agents}, unknown={unknown_agents}"
        )


def _copy_agents(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    enabled_v06_agents: set[str],
) -> None:
    columns = _columns(source, "agents")
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO agents ({', '.join(columns)}) VALUES ({placeholders})"
    for row in source.execute("SELECT * FROM agents ORDER BY agent_id"):
        values = dict(row)
        if values["agent_id"] in enabled_v06_agents:
            capabilities = json.loads(values["protocol_capabilities_json"])
            values["protocol_capabilities_json"] = json.dumps(
                sorted(set(capabilities) | {PROTOCOL_V06})
            )
        destination.execute(sql, tuple(values[column] for column in columns))


def _copy_tasks(source: sqlite3.Connection, destination: sqlite3.Connection) -> None:
    columns = _columns(source, "tasks")
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({placeholders})"
    rows = source.execute(
        "SELECT * FROM tasks ORDER BY CASE WHEN task_id = root_task_id THEN 0 ELSE 1 END, created_at, task_id"
    )
    for row in rows:
        values = dict(row)
        values["protocol_version"] = PROTOCOL_V06
        destination.execute(sql, tuple(values[column] for column in columns))


def _copy_events(source: sqlite3.Connection, destination: sqlite3.Connection) -> None:
    source_columns = _columns(source, "agent_events")
    destination_columns = _columns(destination, "agent_events")
    placeholders = ", ".join("?" for _ in destination_columns)
    sql = f"INSERT INTO agent_events ({', '.join(destination_columns)}) VALUES ({placeholders})"
    for row in source.execute("SELECT * FROM agent_events ORDER BY created_at, event_id"):
        values = {column: row[column] for column in source_columns}
        values.setdefault("inflight_started_at", None)
        values["recovery_attempts"] = 0
        values["inflight_via"] = None
        values["parked_at"] = (
            values["updated_at"]
            if values["outbox_status"] in PARKED_SOURCE_STATUSES
            else None
        )
        if values["outbox_status"] in PARKED_SOURCE_STATUSES:
            values["outbox_status"] = "parked"
            values["inflight_until"] = None
            values["next_retry_at"] = None
            values["exhausted_at"] = None
            values["exhaustion_reason"] = None
        values["payload_json"] = _rewrite_event_payload(values["payload_json"])
        destination.execute(sql, tuple(values[column] for column in destination_columns))


def _copy_table(source: sqlite3.Connection, destination: sqlite3.Connection, table: str) -> None:
    columns = _columns(source, table)
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    for row in source.execute(f"SELECT * FROM {table}"):
        destination.execute(sql, tuple(row[column] for column in columns))


def _validate_destination(
    conn: sqlite3.Connection,
    *,
    source_counts: dict[str, int],
    source_event_statuses: dict[str, int],
    enabled_v06_agents: set[str],
) -> None:
    destination_counts = _counts(conn)
    expected_counts = {**source_counts, "agent_listener_readiness": 0}
    if destination_counts != expected_counts:
        raise ValueError(
            f"destination row counts differ; expected={expected_counts}, actual={destination_counts}"
        )
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("destination foreign_key_check failed")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("destination quick_check failed")
    if conn.execute("SELECT COUNT(*) FROM tasks WHERE protocol_version != ?", (PROTOCOL_V06,)).fetchone()[0]:
        raise ValueError("destination contains non-v0.6 Tasks")
    parked = conn.execute(
        "SELECT COUNT(*) FROM agent_events WHERE outbox_status = 'parked'"
    ).fetchone()[0]
    expected_parked = sum(source_event_statuses.get(status, 0) for status in PARKED_SOURCE_STATUSES)
    if parked != expected_parked:
        raise ValueError(f"parked Event count differs; expected={expected_parked}, actual={parked}")
    for agent_id in enabled_v06_agents:
        row = conn.execute(
            "SELECT protocol_capabilities_json FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if row is None or PROTOCOL_V06 not in json.loads(row[0]):
            raise ValueError(f"enabled Agent is missing v0.6 capability: {agent_id}")


def _rewrite_event_payload(raw: str) -> str:
    payload = json.loads(raw)
    if isinstance(payload, dict) and "protocol_version" in payload:
        payload["protocol_version"] = PROTOCOL_V06
    return json.dumps(payload, sort_keys=True)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}


def _group_counts(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    return {row[0]: row[1] for row in conn.execute(f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}")}


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate a stable Protocol v0.5 snapshot to v0.6.")
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--enable-agent", action="append", default=[])
    args = parser.parse_args()
    result = migrate_v05_to_v06(
        args.source,
        args.destination,
        enabled_v06_agents=set(args.enable_agent),
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
