from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.store import Store


DEFAULT_AUTH_FILE = Path("data/agentrelay-auth.json")
DEFAULT_ENV_DIR = Path("data/local-env")
DEFAULT_BASE_URL = "https://server.stellarix.space/agentrelay/api"
DEFAULT_DB_PATH = Path("data/agentrelay.sqlite3")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or replace an AgentRelay auth identity by username.")
    parser.add_argument("username", help="Human/user name, for example zac")
    parser.add_argument("--agent-id", help="Defaults to '<normalized-username>-agent'")
    parser.add_argument("--auth-file", default=str(DEFAULT_AUTH_FILE), help="Auth JSON path")
    parser.add_argument("--env-dir", default=str(DEFAULT_ENV_DIR), help="Directory for local .env copy files")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Relay base URL for local .env")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="AgentRelay SQLite DB path")
    parser.add_argument("--name", help="Agent display name. Defaults to '<Owner> Agent'")
    parser.add_argument("--owner", help="Agent owner display name. Defaults to title-cased username")
    parser.add_argument("--description", help="Agent card description")
    parser.add_argument("--no-agent", action="store_true", help="Do not create/update the agent registry row")
    parser.add_argument("--no-env-file", action="store_true", help="Do not write data/local-env/<username>.env")
    args = parser.parse_args()

    username = args.username
    agent_id = args.agent_id or default_agent_id(username)
    owner = args.owner or default_owner(username)
    token = secrets.token_urlsafe(32)
    identity = {"username": username, "agent_id": agent_id, "token": token}

    auth_file = Path(args.auth_file)
    identities = read_identities(auth_file)
    identities = [item for item in identities if item.get("username") != username]
    identities.append(identity)
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(json.dumps(identities, indent=2) + "\n")
    auth_file.chmod(0o600)

    agent = None
    if not args.no_agent:
        agent = Store(args.db_path).upsert_agent(
            agent_id=agent_id,
            owner=owner,
            name=args.name or f"{owner} Agent",
            description=args.description or f"Personal coordinator agent for {owner}.",
        )

    env_file = None
    if not args.no_env_file:
        env_dir = Path(args.env_dir)
        env_dir.mkdir(parents=True, exist_ok=True)
        env_file = env_dir / f"{default_agent_id(username).removesuffix('-agent')}.env"
        env_file.write_text(
            f"AGENTRELAY_BASE_URL={args.base_url}\n"
            f"AGENTRELAY_AGENT_ID={agent_id}\n"
            f"AGENTRELAY_USERNAME={username}\n"
            f"AGENTRELAY_TOKEN={token}\n"
        )
        env_file.chmod(0o600)

    print(json.dumps({"username": username, "agent_id": agent_id, "token": token}, indent=2))
    print(f"\nUpdated cloud auth file: {auth_file}")
    if agent:
        print(f"Created/updated agent registry row: {agent['agent_id']} ({agent['name']})")
    if env_file:
        print(f"Wrote local .env copy: {env_file}")
        print("\nCopy these values into the user's local agent-relay-mcp/.env:")
        print(env_file.read_text())
    print("Restart relay after creating identities:")
    print("sudo systemctl restart agentrelay")


def read_identities(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON array")
    return raw


def default_agent_id(username: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", username.strip().lower()).strip("-")
    if not normalized:
        raise ValueError("username must contain at least one letter or digit")
    if normalized.endswith("-agent"):
        return normalized
    return f"{normalized}-agent"


def default_owner(username: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", username.strip())
    if not words:
        raise ValueError("username must contain at least one letter or digit")
    return " ".join(word[:1].upper() + word[1:] for word in words)


if __name__ == "__main__":
    main()
