from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.migrate_v05_to_v06 import migrate_v05_to_v06
from server.protocol_v05 import PROTOCOL_V05
from server.protocol_v06 import PROTOCOL_V06
from server.store_v05 import V05Store


AGENTS = {"zac-agent", "vivi-agent"}
BASE = 1_785_210_000


def seed(path: Path) -> tuple[V05Store, dict]:
    store = V05Store(str(path))
    for agent_id in sorted(AGENTS):
        store.upsert_agent(
            agent_id,
            name=agent_id,
            owner=agent_id,
            enabled=True,
            protocol_capabilities=[PROTOCOL_V05],
            now=BASE,
        )
    for agent_id in sorted(AGENTS):
        readiness = store.register_listener(
            agent_id,
            listener_instance_id=f"listener-{agent_id}-v05",
            client_version="0.5.1",
            workspace_version="2",
            transport="websocket",
            now=BASE,
        )
        store.publish_readiness(
            agent_id,
            listener_instance_id=readiness["listener_instance_id"],
            readiness_epoch=readiness["readiness_epoch"],
            ready=True,
            now=BASE,
        )
    created = store.create_task(
        {
            "protocol_version": PROTOCOL_V05,
            "idempotency_key": "migration-create",
            "requester_agent_id": "zac-agent",
            "target_agent_id": "vivi-agent",
            "done_criteria": "preserve the task",
            "task_expires_at": BASE + 3600,
            "message": {
                "subject": "Migration",
                "parts": [{"kind": "text", "text": "keep me"}],
            },
        },
        now=BASE,
    )
    return store, created


def migration_preserves_history_and_resets_readiness(root: Path) -> None:
    source = root / "source.sqlite3"
    destination = root / "destination.sqlite3"
    store, created = seed(source)
    result = migrate_v05_to_v06(str(source), str(destination), enabled_v06_agents=AGENTS)
    assert result["counts"]["tasks"] == 1
    with store.connect() as conn:
        source_counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("agents", "tasks", "messages", "agent_events", "task_audit_events", "idempotency_records")
        }
    with sqlite3.connect(destination) as conn:
        conn.row_factory = sqlite3.Row
        destination_counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in source_counts
        }
        assert destination_counts == source_counts
        assert conn.execute("SELECT COUNT(*) FROM agent_listener_readiness").fetchone()[0] == 0
        task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (created["task"]["task_id"],)).fetchone()
        assert task["protocol_version"] == PROTOCOL_V06
        for agent_id in AGENTS:
            capabilities = json.loads(
                conn.execute(
                    "SELECT protocol_capabilities_json FROM agents WHERE agent_id = ?", (agent_id,)
                ).fetchone()[0]
            )
            assert PROTOCOL_V05 in capabilities
            assert PROTOCOL_V06 in capabilities


def migration_parks_recoverable_events(root: Path) -> None:
    for status in ("queued", "inflight", "retry_wait"):
        source = root / f"source-{status}.sqlite3"
        destination = root / f"destination-{status}.sqlite3"
        store, _ = seed(source)
        with store.connect() as conn:
            conn.execute(
                "UPDATE agent_events SET outbox_status = ?, inflight_until = ?, next_retry_at = ?",
                (status, BASE + 60 if status == "inflight" else None, BASE + 60 if status == "retry_wait" else None),
            )
        migrate_v05_to_v06(str(source), str(destination), enabled_v06_agents=AGENTS)
        with sqlite3.connect(destination) as conn:
            conn.row_factory = sqlite3.Row
            event = conn.execute("SELECT * FROM agent_events").fetchone()
            assert event["outbox_status"] == "parked"
            assert event["recovery_attempts"] == 0
            assert event["inflight_via"] is None
            assert event["inflight_until"] is None
            assert event["next_retry_at"] is None
            assert event["parked_at"] == event["updated_at"]


def migration_requires_exact_enabled_agent_set(root: Path) -> None:
    source = root / "source-agent-set.sqlite3"
    seed(source)
    try:
        migrate_v05_to_v06(
            str(source),
            str(root / "destination-agent-set.sqlite3"),
            enabled_v06_agents={"zac-agent"},
        )
    except ValueError as error:
        assert "exactly match" in str(error)
    else:
        raise AssertionError("migration accepted an incomplete enabled Agent set")


def migration_refuses_existing_destination(root: Path) -> None:
    source = root / "source-existing.sqlite3"
    destination = root / "destination-existing.sqlite3"
    seed(source)
    destination.write_text("occupied", encoding="utf-8")
    try:
        migrate_v05_to_v06(str(source), str(destination), enabled_v06_agents=AGENTS)
    except ValueError as error:
        assert "refusing to overwrite" in str(error)
    else:
        raise AssertionError("migration overwrote an existing destination")


def migration_rejects_unreachable_terminal_delivery(root: Path) -> None:
    source = root / "source-terminal.sqlite3"
    store, created = seed(source)
    with store.connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'expired' WHERE task_id = ?",
            (created["task"]["task_id"],),
        )
    try:
        migrate_v05_to_v06(
            str(source),
            str(root / "destination-terminal.sqlite3"),
            enabled_v06_agents=AGENTS,
        )
    except ValueError as error:
        assert "terminal Tasks" in str(error)
    else:
        raise AssertionError("migration accepted an unreachable transition Event")


def main() -> None:
    tests = [
        migration_preserves_history_and_resets_readiness,
        migration_parks_recoverable_events,
        migration_requires_exact_enabled_agent_set,
        migration_refuses_existing_destination,
        migration_rejects_unreachable_terminal_delivery,
    ]
    for test in tests:
        with tempfile.TemporaryDirectory() as directory:
            test(Path(directory))
        print(f"PASS {test.__name__}")
    print(json.dumps({"passed": len(tests), "failed": 0}, sort_keys=True))


if __name__ == "__main__":
    main()
