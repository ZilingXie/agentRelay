from __future__ import annotations

import argparse
import json
import re
import secrets


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an AgentRelay Phase 1 auth identity.")
    parser.add_argument("username", nargs="?", help="Human/user name, for example zac")
    parser.add_argument("--username", dest="username_flag", help="Human/user name, for example zac")
    parser.add_argument(
        "--agent-id",
        help="Agent id. Defaults to '<normalized-username>-agent', for example zac-agent.",
    )
    parser.add_argument("--json-only", action="store_true", help="Print only the JSON identity object")
    args = parser.parse_args()

    username = args.username_flag or args.username
    if not username:
        parser.error("username is required, for example: generate_agent_token.py zac")
    agent_id = args.agent_id or default_agent_id(username)
    token = secrets.token_urlsafe(32)
    identity = {"username": username, "agent_id": agent_id, "token": token}

    if args.json_only:
        print(json.dumps(identity, separators=(",", ":")))
        return

    print(json.dumps(identity, indent=2))
    print("\nAGENTRELAY_TOKENS entry:")
    print(f"{username}:{agent_id}:{token}")
    print("\nLocal .env values:")
    print(f"AGENTRELAY_AGENT_ID={agent_id}")
    print(f"AGENTRELAY_USERNAME={username}")
    print(f"AGENTRELAY_TOKEN={token}")


def default_agent_id(username: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", username.strip().lower()).strip("-")
    if not normalized:
        raise ValueError("username must contain at least one letter or digit")
    if normalized.endswith("-agent"):
        return normalized
    return f"{normalized}-agent"


if __name__ == "__main__":
    main()
