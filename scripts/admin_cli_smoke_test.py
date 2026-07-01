from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.store import Store


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "agentrelay-admin.sqlite3"
        store = Store(str(db_path))
        task = store.create_task(
            {
                "from": "zac-agent",
                "to": "frank-agent",
                "requesterThreadId": "zac-thread-admin-smoke",
                "subject": "Admin CLI smoke",
                "doneCriteria": "Admin CLI can inspect the task.",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "Ask Frank for availability."}],
                },
            }
        )
        task_id = task["task_id"]

        summary = run_json(db_path, "summary")
        if summary["tasks"]["total"] != 1:
            raise AssertionError("summary should include one task")
        if summary["tasks"]["pending_by_agent"].get("frank-agent") != 1:
            raise AssertionError("summary should include frank-agent pending count")

        agents = run_json(db_path, "agents")
        agent_ids = {agent["agent_id"] for agent in agents["agents"]}
        if {"zac-agent", "frank-agent"} - agent_ids:
            raise AssertionError("agents command should list seed agents")

        tasks = run_json(db_path, "tasks", "--agent-id", "frank-agent")
        if [row["task_id"] for row in tasks["tasks"]] != [task_id]:
            raise AssertionError("tasks command should filter by related agent")

        pending = run_json(db_path, "pending", "frank-agent")
        if [row["taskId"] for row in pending["tasks"]] != [task_id]:
            raise AssertionError("pending command should list pending task")

        task_payload = run_json(db_path, "task", task_id)
        if task_payload["task"]["subject"] != "Admin CLI smoke":
            raise AssertionError("task command should return full task")

        timeline = run_json(db_path, "timeline", task_id)
        if timeline["timeline"]["entries"][0]["event_type"] != "task.created":
            raise AssertionError("timeline command should return task timeline")

        events = run_json(db_path, "events", "--agent-id", "frank-agent")
        if len(events["events"]) != 1 or events["events"][0]["task_id"] != task_id:
            raise AssertionError("events command should list agent events")

        table_output = run_text(db_path, "tasks", "--agent-id", "frank-agent")
        if "Admin CLI smoke" not in table_output:
            raise AssertionError("table output should include task subject")

        print(json.dumps({"ok": True, "taskId": task_id}, indent=2))


def run_json(db_path: Path, *args: str) -> dict:
    output = run_text(db_path, "--format", "json", *args)
    return json.loads(output)


def run_text(db_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["python3", "scripts/agentrelay_admin.py", "--db-path", str(db_path), *args],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout


if __name__ == "__main__":
    main()
