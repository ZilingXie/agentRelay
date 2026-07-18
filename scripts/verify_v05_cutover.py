from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.export_protocol_retirement import LEGACY_TERMINAL_STATES


def verify_v05_cutover(
    legacy_db_path: str,
    v05_db_path: str,
    retirement_report_path: str,
) -> dict[str, Any]:
    legacy = Path(legacy_db_path).resolve()
    v05 = Path(v05_db_path).resolve()
    report_path = Path(retirement_report_path).resolve()
    if not legacy.is_file() or not v05.is_file() or not report_path.is_file():
        raise ValueError("legacy database, v0.5 database, and retirement report must exist")

    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_ids = {item["task_id"] for item in report.get("tasks", [])}
    with sqlite3.connect(f"file:{legacy}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        legacy_rows = conn.execute("SELECT task_id, status FROM tasks").fetchall()
    expected_ids = {
        row["task_id"] for row in legacy_rows if row["status"] not in LEGACY_TERMINAL_STATES
    }
    if report_ids != expected_ids:
        raise ValueError("retirement report does not exactly match legacy non-terminal Tasks")

    with sqlite3.connect(v05) as conn:
        conn.row_factory = sqlite3.Row
        collaboration_counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "tasks",
                "messages",
                "agent_events",
                "task_audit_events",
                "idempotency_records",
                "agent_listener_readiness",
            )
        }
        non_empty = {table: count for table, count in collaboration_counts.items() if count}
        if non_empty:
            raise ValueError(f"v0.5 database contains migrated collaboration/readiness rows: {non_empty}")
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        forbidden_columns = {"delivery_status", "delivery_state", "status_version"} & task_columns
        if forbidden_columns:
            raise ValueError(f"v0.5 tasks table contains legacy truth fields: {sorted(forbidden_columns)}")
        trigger = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = 'prevent_task_hard_delete'"
        ).fetchone()
        if not trigger:
            raise ValueError("v0.5 hard-delete trigger is missing")
        agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    return {
        "ok": True,
        "legacy_non_terminal_tasks": len(expected_ids),
        "v05_agents": agent_count,
        "v05_collaboration_counts": collaboration_counts,
        "hard_delete_trigger": "prevent_task_hard_delete",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the Protocol v0.5 database cutover boundary.")
    parser.add_argument("legacy_db")
    parser.add_argument("v05_db")
    parser.add_argument("retirement_report")
    args = parser.parse_args()
    result = verify_v05_cutover(args.legacy_db, args.v05_db, args.retirement_report)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
