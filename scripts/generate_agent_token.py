from __future__ import annotations

import argparse
import json
import secrets


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an AgentRelay Phase 1 auth identity.")
    parser.add_argument("--username", required=True, help="Human/user name, for example zac")
    parser.add_argument("--agent-id", required=True, help="Agent id, for example zac-agent")
    args = parser.parse_args()
    token = secrets.token_urlsafe(32)
    identity = {"username": args.username, "agent_id": args.agent_id, "token": token}
    print(json.dumps(identity, indent=2))
    print("\nAGENTRELAY_TOKENS entry:")
    print(f"{args.username}:{args.agent_id}:{token}")
    print("\nLocal .env values:")
    print(f"AGENTRELAY_AGENT_ID={args.agent_id}")
    print(f"AGENTRELAY_USERNAME={args.username}")
    print(f"AGENTRELAY_TOKEN={token}")


if __name__ == "__main__":
    main()
