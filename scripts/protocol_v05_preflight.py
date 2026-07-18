from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.verify_v05_cutover import verify_v05_cutover
from server.protocol_v05 import PROTOCOL_V05


JsonGetter = Callable[[str, dict[str, str]], dict[str, Any]]


def run_preflight(
    *,
    base_url: str,
    admin_token: str,
    legacy_db: str,
    v05_db: str,
    retirement_report: str,
    expected_mode: str = "closed",
    require_empty_collaboration: bool = True,
    get_json: JsonGetter | None = None,
) -> dict[str, Any]:
    if expected_mode not in {"closed", "v05"}:
        raise ValueError("expected_mode must be closed or v05")
    if not admin_token:
        raise ValueError("admin token is required")
    fetch = get_json or fetch_json
    base = base_url.rstrip("/")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    health = fetch(f"{base}/health", {})
    current = fetch(f"{base}/protocols/current", {})
    summary = fetch(f"{base}/admin/api/summary", admin_headers)
    agents_payload = fetch(f"{base}/admin/api/agents", admin_headers)
    agents = agents_payload.get("agents")
    if not isinstance(agents, list):
        raise ValueError("admin agents response is missing agents")

    boundary = verify_v05_cutover(
        legacy_db,
        v05_db,
        retirement_report,
        allow_readiness=True,
        allow_existing_collaboration=not require_empty_collaboration,
    )
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    health_protocol = health.get("protocol") or {}
    check("health_protocol", health_protocol.get("version") == PROTOCOL_V05, health_protocol.get("version"))
    check("health_write_mode", health_protocol.get("write_mode") == expected_mode, health_protocol.get("write_mode"))
    check("current_protocol", current.get("version") == PROTOCOL_V05, current.get("version"))
    check("current_write_mode", current.get("write_mode") == expected_mode, current.get("write_mode"))
    check("summary_protocol", summary.get("protocol_version") == PROTOCOL_V05, summary.get("protocol_version"))
    check("invariant_violations", int(summary.get("invariant_violations", -1)) == 0, summary.get("invariant_violations"))
    stale = (summary.get("readiness") or {}).get("stale_enabled_agents")
    check("stale_enabled_agents", stale == 0, stale)

    collaboration_counts = boundary["v05_collaboration_counts"]
    collaboration_without_readiness = {
        key: value for key, value in collaboration_counts.items()
        if key != "agent_listener_readiness"
    }
    if require_empty_collaboration:
        check(
            "empty_v05_collaboration",
            all(value == 0 for value in collaboration_without_readiness.values()),
            collaboration_without_readiness,
        )

    enabled_results = []
    for agent in agents:
        if not agent.get("enabled"):
            continue
        failures = []
        capabilities = agent.get("protocol_capabilities") or []
        if PROTOCOL_V05 not in capabilities:
            failures.append("protocol_capability")
        if agent.get("readiness_protocol_version") != PROTOCOL_V05:
            failures.append("readiness_protocol")
        if agent.get("ready") is not True:
            failures.append("not_ready")
        if agent.get("readiness_fresh") is not True:
            failures.append("stale_readiness")
        if str(agent.get("workspace_version") or "") != "2":
            failures.append("workspace_version")
        if not agent.get("listener_instance_id"):
            failures.append("listener_instance")
        if not _positive_int(agent.get("readiness_epoch")):
            failures.append("readiness_epoch")
        if agent.get("transport") != "websocket":
            failures.append("transport")
        enabled_results.append({
            "agent_id": agent.get("agent_id"),
            "ok": not failures,
            "failures": failures,
        })
    check("enabled_agent_count", len(enabled_results) >= 2, len(enabled_results))
    check(
        "enabled_agent_readiness",
        bool(enabled_results) and all(item["ok"] for item in enabled_results),
        enabled_results,
    )
    return {
        "ok": all(item["ok"] for item in checks),
        "expected_mode": expected_mode,
        "checks": checks,
        "enabled_agents": enabled_results,
        "database_boundary": boundary,
    }


def fetch_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed with HTTP {exc.code}: {body[:200]}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"GET {url} did not return a JSON object")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the read-only Protocol v0.5 maintenance preflight.")
    parser.add_argument("--base-url", required=True, help="Relay API base, for example https://host/agentrelay/api")
    parser.add_argument("--legacy-db", required=True)
    parser.add_argument("--v05-db", required=True)
    parser.add_argument("--retirement-report", required=True)
    parser.add_argument("--expected-mode", choices=["closed", "v05"], default="closed")
    parser.add_argument("--allow-existing-collaboration", action="store_true")
    parser.add_argument("--admin-token-env", default="AGENTRELAY_ADMIN_TOKEN")
    args = parser.parse_args()
    token = os.environ.get(args.admin_token_env, "")
    result = run_preflight(
        base_url=args.base_url,
        admin_token=token,
        legacy_db=args.legacy_db,
        v05_db=args.v05_db,
        retirement_report=args.retirement_report,
        expected_mode=args.expected_mode,
        require_empty_collaboration=not args.allow_existing_collaboration,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["ok"]:
        raise SystemExit(1)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


if __name__ == "__main__":
    main()
