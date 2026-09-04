from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.store_v06 import V06Store


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upsert a governed static Agent profile in the Protocol v0.6 registry."
    )
    parser.add_argument("agent_id")
    parser.add_argument("profile", help="Path to an Agent Profile v0.6 JSON document.")
    parser.add_argument("--db-path", default="data/agentrelay-v06.sqlite3")
    args = parser.parse_args()

    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    result = V06Store(args.db_path).upsert_agent_profile(args.agent_id, profile)
    print(
        json.dumps(
            {
                "ok": True,
                "agent_id": result["agent_id"],
                "card_revision": result["card_revision"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
