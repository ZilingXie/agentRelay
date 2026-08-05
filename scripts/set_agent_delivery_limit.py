from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.delivery_control import DeliveryControl


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Persist an AgentRelay per-Agent delivery inflight limit."
    )
    parser.add_argument("agent_id")
    parser.add_argument("max_inflight", type=int)
    parser.add_argument(
        "--control-db",
        default=os.environ.get(
            "AGENTRELAY_DELIVERY_CONTROL_DB_PATH",
            "./data/agentrelay-delivery-control.sqlite3",
        ),
    )
    args = parser.parse_args()
    result = DeliveryControl(args.control_db, []).set_max_inflight(
        args.agent_id,
        args.max_inflight,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
