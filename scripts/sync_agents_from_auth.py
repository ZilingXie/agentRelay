from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.store import Store


DEFAULT_AUTH_FILE = Path("data/agentrelay-auth.json")
DEFAULT_DB_PATH = Path("data/agentrelay.sqlite3")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create missing agent registry rows from auth identities without changing tokens.")
    parser.add_argument("--auth-file", default=str(DEFAULT_AUTH_FILE))
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()

    identities = read_identities(Path(args.auth_file))
    store = Store(args.db_path)
    synced = []
    for identity in identities:
        username = required(identity, "username")
        agent_id = required(identity, "agent_id")
        owner = default_owner(username)
        agent = store.upsert_agent(
            agent_id=agent_id,
            owner=owner,
            name=f"{owner} Agent",
            description=f"Personal coordinator agent for {owner}.",
            agent_role="personal_agent",
            execution_mode="notify_only",
        )
        synced.append(agent)
    print(json.dumps({"ok": True, "synced": synced}, indent=2))


def read_identities(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON array")
    return raw


def required(value: dict[str, Any], key: str) -> str:
    result = str(value.get(key, "")).strip()
    if not result:
        raise ValueError(f"missing required identity field: {key}")
    return result


def default_owner(username: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", username.strip())
    if not words:
        raise ValueError("username must contain at least one letter or digit")
    return " ".join(word[:1].upper() + word[1:] for word in words)


if __name__ == "__main__":
    main()
