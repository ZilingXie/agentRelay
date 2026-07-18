from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any


LEGACY_TERMINAL_STATES = {"completed", "expired", "failed", "cancelled"}


def export_retirement_report(
    legacy_db_path: str,
    output_path: str,
    *,
    generated_at: int | None = None,
) -> dict[str, Any]:
    source = Path(legacy_db_path).resolve()
    if not source.is_file():
        raise ValueError(f"legacy database not found: {source}")
    timestamp = int(time.time()) if generated_at is None else int(generated_at)
    uri = f"file:{source}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        required = {"task_id", "status", "requester_agent_id", "target_agent_id"}
        missing = sorted(required - columns)
        if missing:
            raise ValueError(f"legacy tasks table missing columns: {', '.join(missing)}")
        optional = [name for name in ("protocol_version", "current_message_id") if name in columns]
        select_columns = [*sorted(required), *optional]
        rows = conn.execute(
            f"SELECT {', '.join(select_columns)} FROM tasks ORDER BY created_at, task_id"
        ).fetchall()

    tasks = []
    for row in rows:
        if row["status"] in LEGACY_TERMINAL_STATES:
            continue
        tasks.append(
            {
                "task_id": row["task_id"],
                "protocol_version": row["protocol_version"] if "protocol_version" in row.keys() else None,
                "original_status": row["status"],
                "requester_agent_id": row["requester_agent_id"],
                "target_agent_id": row["target_agent_id"],
                "current_message_id": row["current_message_id"] if "current_message_id" in row.keys() else None,
                "operational_reason": "protocol_upgrade_required",
            }
        )
    report = {
        "report_version": 1,
        "generated_at": timestamp,
        "source_database": source.name,
        "non_terminal_task_count": len(tasks),
        "tasks": tasks,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, destination)
    return {
        "report": report,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "output_path": str(destination.resolve()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export non-terminal legacy AgentRelay Tasks.")
    parser.add_argument("legacy_db")
    parser.add_argument("output")
    args = parser.parse_args()
    result = export_retirement_report(args.legacy_db, args.output)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
