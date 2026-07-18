from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.export_protocol_retirement import export_retirement_report
from scripts.init_v05_database import initialize_v05_database
from scripts.verify_v05_cutover import verify_v05_cutover
from server.store import Store
from server.store_v05 import V05Store


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        legacy_path = root / "legacy.sqlite3"
        v05_path = root / "v05.sqlite3"
        report_path = root / "retirement.json"
        registry_path = root / "agents.json"

        legacy = Store(str(legacy_path))
        active = legacy.create_task_v04(
            {
                "protocol_version": "agent-collab-v0.4",
                "idempotency_key": "cutover-active",
                "requester_agent_id": "zac-agent",
                "target_agent_id": "frank-agent",
                "done_criteria": "response",
                "message": {"parts": [{"kind": "text", "text": "active"}]},
            }
        )
        terminal = legacy.create_task_v04(
            {
                "protocol_version": "agent-collab-v0.4",
                "idempotency_key": "cutover-terminal",
                "requester_agent_id": "zac-agent",
                "target_agent_id": "frank-agent",
                "done_criteria": "response",
                "message": {"parts": [{"kind": "text", "text": "terminal"}]},
            }
        )
        terminal = legacy.fail_v04_task(
            terminal["task_id"],
            "relay",
            {
                "current_message_id": terminal["current_message_id"],
                "turn_sequence": terminal["turn_sequence"],
                "expected_status_version": terminal["status_version"],
                "idempotency_key": "cutover-terminal-fail",
                "reason": "relay_persistence_failed",
            },
        )
        assert terminal["status"] == "failed"

        exported = export_retirement_report(
            str(legacy_path), str(report_path), generated_at=20_000
        )
        assert exported["report"]["non_terminal_task_count"] == 1
        assert exported["report"]["tasks"][0]["task_id"] == active["task_id"]

        registry_path.write_text(
            json.dumps(
                {
                    "agents": [
                        {
                            "agent_id": "zac-agent",
                            "name": "Zac Agent",
                            "owner": "Zac",
                            "enabled": True,
                            "protocol_capabilities": ["agent-collab-v0.5"],
                        },
                        {
                            "agent_id": "frank-agent",
                            "name": "Frank Agent",
                            "owner": "Frank",
                            "enabled": True,
                            "protocol_capabilities": ["agent-collab-v0.5"],
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        initialized = initialize_v05_database(
            str(v05_path), agent_registry_path=str(registry_path), now=20_000
        )
        assert initialized["agents_imported"] == 2
        assert initialized["counts"]["agent_listener_readiness"] == 0
        result = verify_v05_cutover(str(legacy_path), str(v05_path), str(report_path))
        assert result["ok"] and result["v05_agents"] == 2
        store = V05Store(str(v05_path))
        for agent_id in ("zac-agent", "frank-agent"):
            readiness = store.register_listener(
                agent_id,
                listener_instance_id=f"listener-{agent_id}",
                client_version="0.5.0",
                workspace_version="2",
                transport="websocket",
                now=20_001,
            )
            store.publish_readiness(
                agent_id,
                listener_instance_id=f"listener-{agent_id}",
                readiness_epoch=readiness["readiness_epoch"],
                ready=True,
                now=20_001,
            )
        store.create_task(
            {
                "protocol_version": "agent-collab-v0.5",
                "idempotency_key": "cutover-post-write",
                "requester_agent_id": "zac-agent",
                "target_agent_id": "frank-agent",
                "done_criteria": "response",
                "message": {"parts": [{"kind": "text", "text": "post-write"}]},
            },
            now=20_001,
        )
        post_write = verify_v05_cutover(
            str(legacy_path),
            str(v05_path),
            str(report_path),
            allow_readiness=True,
            allow_existing_collaboration=True,
        )
        assert post_write["ok"] and post_write["v05_collaboration_counts"]["tasks"] == 1
    print("protocol v0.5 cutover tooling smoke passed")


if __name__ == "__main__":
    main()
