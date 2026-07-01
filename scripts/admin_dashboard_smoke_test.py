from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.store import Store

BASE_URL = "http://127.0.0.1:8797"
ADMIN_TOKEN = "admin-dashboard-token"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = f"{tmpdir}/agentrelay-admin-dashboard.sqlite3"
        store = Store(db_path)
        task = store.create_task(
            {
                "from": "zac-agent",
                "to": "frank-agent",
                "requesterThreadId": "zac-thread-dashboard-smoke",
                "subject": "Admin dashboard smoke",
                "doneCriteria": "Dashboard can inspect the task.",
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "Ask Frank for availability."}],
                },
            }
        )
        task_id = task["task_id"]
        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_HOST": "127.0.0.1",
                "AGENTRELAY_PORT": "8797",
                "AGENTRELAY_DB_PATH": db_path,
                "AGENTRELAY_ADMIN_TOKEN": ADMIN_TOKEN,
            }
        )
        proc = subprocess.Popen(
            ["python3", "-m", "server.app"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            wait_for_health()
            html = get_text(f"{BASE_URL}/agentrelay/dashboard/")
            if "AgentRelay Dashboard" not in html:
                raise AssertionError("dashboard HTML did not load")

            unauthorized = get_json(
                f"{BASE_URL}/agentrelay/admin/api/summary",
                expected_status=401,
                token=None,
            )
            if unauthorized.get("error") != "missing admin token":
                raise AssertionError("admin API should require admin token")

            summary = get_json(f"{BASE_URL}/agentrelay/admin/api/summary")
            if summary["tasks"]["total"] != 1 or summary["tasks"]["active"] != 1:
                raise AssertionError("summary did not include active test task")

            agents = get_json(f"{BASE_URL}/agentrelay/admin/api/agents")["agents"]
            agent_ids = {agent["agent_id"] for agent in agents}
            if {"zac-agent", "frank-agent"} - agent_ids:
                raise AssertionError("agents endpoint did not list seed agents")

            tasks = get_json(f"{BASE_URL}/agentrelay/admin/api/tasks?agent_id=frank-agent")["tasks"]
            if [row["task_id"] for row in tasks] != [task_id]:
                raise AssertionError("tasks endpoint did not filter by related agent")

            detail = get_json(f"{BASE_URL}/agentrelay/admin/api/tasks/{task_id}")
            if detail["task"]["subject"] != "Admin dashboard smoke":
                raise AssertionError("task detail did not return full task")
            if detail["timeline"]["entries"][0]["event_type"] != "task.created":
                raise AssertionError("task detail did not include timeline")
            if not detail["agent_events"]:
                raise AssertionError("task detail did not include agent events")

            events = get_json(f"{BASE_URL}/agentrelay/admin/api/events?agent_id=frank-agent")["events"]
            if [row["task_id"] for row in events] != [task_id]:
                raise AssertionError("events endpoint did not filter agent events")

            print(json.dumps({"ok": True, "taskId": task_id}, indent=2))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def wait_for_health() -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            payload = get_json(f"{BASE_URL}/health", token=None)
            if payload.get("ok"):
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def get_json(url: str, expected_status: int = 200, token: str | None = ADMIN_TOKEN) -> dict:
    req = urllib.request.Request(url, headers=headers(token))
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if response.status != expected_status:
                raise AssertionError(f"GET {url} returned {response.status}, expected {expected_status}: {payload}")
            return payload
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
        if exc.code != expected_status:
            raise AssertionError(f"GET {url} returned {exc.code}, expected {expected_status}: {payload}") from exc
        return payload


def get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        if response.status != 200:
            raise AssertionError(f"GET {url} returned {response.status}")
        return response.read().decode("utf-8")


def headers(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


if __name__ == "__main__":
    main()
