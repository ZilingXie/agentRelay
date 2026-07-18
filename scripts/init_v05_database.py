from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.store_v05 import V05Store


ALLOWED_AGENT_FIELDS = {
    "agent_id",
    "name",
    "owner",
    "enabled",
    "protocol_capabilities",
}


def initialize_v05_database(
    db_path: str,
    *,
    agent_registry_path: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    destination = Path(db_path)
    if destination.exists():
        raise ValueError(f"refusing to initialize existing database: {destination}")
    store = V05Store(str(destination))
    imported = 0
    if agent_registry_path:
        payload = json.loads(Path(agent_registry_path).read_text(encoding="utf-8"))
        agents = payload.get("agents") if isinstance(payload, dict) else None
        if not isinstance(agents, list):
            raise ValueError("agent registry must be an object with an agents array")
        for item in agents:
            if not isinstance(item, dict):
                raise ValueError("agent registry entries must be objects")
            unknown = sorted(set(item) - ALLOWED_AGENT_FIELDS)
            missing = sorted(ALLOWED_AGENT_FIELDS - set(item))
            if unknown or missing:
                raise ValueError(
                    f"invalid agent registry entry; missing={missing}, unknown={unknown}"
                )
            capabilities = item["protocol_capabilities"]
            if not isinstance(capabilities, list) or not all(
                isinstance(value, str) and value for value in capabilities
            ):
                raise ValueError("protocol_capabilities must be an array of strings")
            store.upsert_agent(
                str(item["agent_id"]),
                name=str(item["name"]),
                owner=str(item["owner"]),
                enabled=bool(item["enabled"]),
                protocol_capabilities=capabilities,
                now=now,
            )
            imported += 1
    with store.connect() as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("agents", "agent_listener_readiness", "tasks", "messages", "agent_events")
        }
    return {
        "database": str(destination.resolve()),
        "agents_imported": imported,
        "counts": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize a clean Protocol v0.5 database.")
    parser.add_argument("db_path")
    parser.add_argument("--agent-registry")
    args = parser.parse_args()
    result = initialize_v05_database(
        args.db_path,
        agent_registry_path=args.agent_registry,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
