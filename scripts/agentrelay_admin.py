from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.store import Store


DEFAULT_DB_PATH = Path("data/agentrelay.sqlite3")


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentRelay admin/debug CLI for local SQLite inspection.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="AgentRelay SQLite DB path")
    parser.add_argument("--format", choices=["json", "table"], default="table", help="Output format")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("summary", help="Show high-level counts for agents, tasks, and events")
    subparsers.add_parser("agents", help="List known agents")

    tasks_parser = subparsers.add_parser("tasks", help="List tasks with optional filters")
    tasks_parser.add_argument("--agent-id", help="Requester, target, pending, completion owner, or claimed agent id")
    tasks_parser.add_argument("--status", help="Filter by task status")
    tasks_parser.add_argument("--limit", type=positive_int, default=50)

    task_parser = subparsers.add_parser("task", help="Show one full task")
    task_parser.add_argument("task_id")

    timeline_parser = subparsers.add_parser("timeline", help="Show one task timeline")
    timeline_parser.add_argument("task_id")

    events_parser = subparsers.add_parser("events", help="List durable agent events")
    events_parser.add_argument("--agent-id", help="Filter by agent id")
    events_parser.add_argument("--state", choices=["pending", "inflight", "done", "failed"], help="Filter by delivery state")
    events_parser.add_argument("--include-acked", action="store_true", help="Include done/acked events")
    events_parser.add_argument("--limit", type=positive_int, default=50)

    pending_parser = subparsers.add_parser("pending", help="List task work pending on an agent")
    pending_parser.add_argument("agent_id")
    pending_parser.add_argument("--limit", type=positive_int, default=50)

    args = parser.parse_args()
    store = Store(args.db_path)
    payload = dispatch(store, args)
    print_output(payload, args.format)


def dispatch(store: Store, args: argparse.Namespace) -> Any:
    if args.command == "summary":
        return summary(store)
    if args.command == "agents":
        return {"agents": store.list_agents()}
    if args.command == "tasks":
        return {"tasks": list_tasks(store, args.agent_id, args.status, args.limit)}
    if args.command == "task":
        task = store.get_task(args.task_id)
        if not task:
            raise SystemExit(f"task not found: {args.task_id}")
        return {"task": task}
    if args.command == "timeline":
        timeline = store.get_timeline(args.task_id)
        if timeline is None:
            raise SystemExit(f"task not found: {args.task_id}")
        return {"timeline": timeline}
    if args.command == "events":
        return {"events": list_events(store, args.agent_id, args.state, args.include_acked, args.limit)}
    if args.command == "pending":
        return {"tasks": store.list_pending_tasks(args.agent_id, limit=args.limit)}
    raise SystemExit(f"unknown command: {args.command}")


def summary(store: Store) -> dict[str, Any]:
    with store.connect() as conn:
        return {
            "agents": scalar(conn, "SELECT COUNT(*) FROM agents"),
            "tasks": {
                "total": scalar(conn, "SELECT COUNT(*) FROM tasks"),
                "by_status": grouped_counts(conn, "SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY status"),
                "pending_by_agent": grouped_counts(
                    conn,
                    """
                    SELECT COALESCE(pending_on_agent_id, 'none'), COUNT(*)
                    FROM tasks
                    GROUP BY COALESCE(pending_on_agent_id, 'none')
                    ORDER BY 1
                    """,
                ),
            },
            "agent_events": {
                "total": scalar(conn, "SELECT COUNT(*) FROM agent_events"),
                "by_delivery_state": grouped_counts(
                    conn,
                    """
                    SELECT delivery_state, COUNT(*)
                    FROM agent_events
                    GROUP BY delivery_state
                    ORDER BY delivery_state
                    """,
                ),
            },
        }


def list_tasks(store: Store, agent_id: str | None, status: str | None, limit: int) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if agent_id:
        where.append(
            """
            (
              requester_agent_id = ?
              OR target_agent_id = ?
              OR completion_owner_agent_id = ?
              OR pending_on_agent_id = ?
              OR claimed_by = ?
            )
            """
        )
        params.extend([agent_id, agent_id, agent_id, agent_id, agent_id])
    if status:
        where.append("status = ?")
        params.append(status)
    sql = """
        SELECT task_id, context_id, subject, status, requester_agent_id, target_agent_id,
               completion_owner_agent_id, pending_on_agent_id, next_action,
               turn_count, max_turns, updated_at, created_at
        FROM tasks
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, created_at DESC, task_id LIMIT ?"
    params.append(limit)
    with store.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def list_events(
    store: Store,
    agent_id: str | None,
    state: str | None,
    include_acked: bool,
    limit: int,
) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    if state:
        where.append("delivery_state = ?")
        params.append(state)
    if not include_acked:
        where.append("acked_at IS NULL")
    sql = """
        SELECT event_id, agent_id, event_type, task_id, delivery_state,
               delivery_attempts, inflight_until, acked_at, created_at, last_error
        FROM agent_events
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, event_id DESC LIMIT ?"
    params.append(limit)
    with store.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def scalar(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def grouped_counts(conn: sqlite3.Connection, sql: str) -> dict[str, int]:
    return {str(key): int(count) for key, count in conn.execute(sql).fetchall()}


def print_output(payload: Any, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print_table_payload(payload)


def print_table_payload(payload: Any) -> None:
    if isinstance(payload, dict) and len(payload) == 1:
        key, value = next(iter(payload.items()))
        print(f"{key}:")
        print_table_payload(value)
        return
    if isinstance(payload, list):
        print_table(payload)
        return
    print(json.dumps(payload, indent=2, sort_keys=True))


def print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("(none)")
        return
    columns = list(rows[0].keys())
    widths = {
        column: min(
            max(len(column), *(len(shorten(row.get(column))) for row in rows)),
            42,
        )
        for column in columns
    }
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    print(header)
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(shorten(row.get(column)).ljust(widths[column]) for column in columns))


def shorten(value: Any, limit: int = 42) -> str:
    if value is None:
        text = ""
    else:
        text = str(value)
    text = text.replace("\n", " ")
    if len(text) > limit:
        return text[: limit - 1] + "..."
    return text


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


if __name__ == "__main__":
    main()
