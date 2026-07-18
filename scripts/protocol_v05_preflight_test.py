from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.export_protocol_retirement import export_retirement_report
from scripts.init_v05_database import initialize_v05_database
from scripts.protocol_v05_preflight import run_preflight
from server.protocol_v05 import PROTOCOL_V05
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
        legacy.create_task_v04({
            "protocol_version": "agent-collab-v0.4",
            "idempotency_key": "preflight-active",
            "requester_agent_id": "zac-agent",
            "target_agent_id": "frank-agent",
            "done_criteria": "response",
            "message": {"parts": [{"kind": "text", "text": "active"}]},
        })
        export_retirement_report(str(legacy_path), str(report_path))
        registry_path.write_text(json.dumps({"agents": [
            agent("zac-agent"), agent("frank-agent")
        ]}), encoding="utf-8")
        initialize_v05_database(str(v05_path), agent_registry_path=str(registry_path))
        store = V05Store(str(v05_path))
        runtime_agents = []
        for agent_id in ("zac-agent", "frank-agent"):
            instance = f"listener-{agent_id}"
            readiness = store.register_listener(
                agent_id,
                listener_instance_id=instance,
                client_version="0.5.0",
                workspace_version="2",
                transport="websocket",
            )
            store.publish_readiness(
                agent_id,
                listener_instance_id=instance,
                readiness_epoch=readiness["readiness_epoch"],
                ready=True,
            )
            runtime_agents.append({
                **store.get_agent(agent_id),
                **store.get_readiness(agent_id),
                "readiness_protocol_version": PROTOCOL_V05,
                "protocol_capabilities": [PROTOCOL_V05],
                "readiness_fresh": True,
            })
        port = free_port()
        base_url = f"http://127.0.0.1:{port}/agentrelay/api"
        process = start_server(legacy_path, v05_path, port, mode="closed")
        try:
            wait_health(base_url)
            result = run_preflight(
                base_url=base_url,
                admin_token="test-admin-token",
                legacy_db=str(legacy_path),
                v05_db=str(v05_path),
                retirement_report=str(report_path),
            )
            assert result["ok"] is True
            assert len(result["enabled_agents"]) == 2
        finally:
            process.terminate()
            process.wait(timeout=5)

        port = free_port()
        base_url = f"http://127.0.0.1:{port}/agentrelay/api"
        process = start_server(legacy_path, v05_path, port, mode="v05")
        try:
            wait_health(base_url)
            opened = run_preflight(
                base_url=base_url,
                admin_token="test-admin-token",
                legacy_db=str(legacy_path),
                v05_db=str(v05_path),
                retirement_report=str(report_path),
                expected_mode="v05",
            )
            assert opened["ok"] is True

            store.create_task({
                "protocol_version": PROTOCOL_V05,
                "idempotency_key": "preflight-post-write-task",
                "requester_agent_id": "zac-agent",
                "target_agent_id": "frank-agent",
                "done_criteria": "response",
                "message": {"parts": [{"kind": "text", "text": "post-write"}]},
            })
            try:
                run_preflight(
                    base_url=base_url,
                    admin_token="test-admin-token",
                    legacy_db=str(legacy_path),
                    v05_db=str(v05_path),
                    retirement_report=str(report_path),
                    expected_mode="v05",
                )
            except ValueError as exc:
                assert "contains migrated collaboration/readiness rows" in str(exc)
            else:
                raise AssertionError("strict preflight accepted existing collaboration rows")

            post_write = run_preflight(
                base_url=base_url,
                admin_token="test-admin-token",
                legacy_db=str(legacy_path),
                v05_db=str(v05_path),
                retirement_report=str(report_path),
                expected_mode="v05",
                require_empty_collaboration=False,
            )
            assert post_write["ok"] is True
            assert post_write["database_boundary"]["v05_collaboration_counts"]["tasks"] == 1
        finally:
            process.terminate()
            process.wait(timeout=5)

        broken = [dict(item) for item in runtime_agents]
        broken[1]["readiness_fresh"] = False
        broken[1]["ready"] = False
        failed = run_preflight(
            base_url="https://relay.example/agentrelay/api",
            admin_token="test-token",
            legacy_db=str(legacy_path),
            v05_db=str(v05_path),
            retirement_report=str(report_path),
            require_empty_collaboration=False,
            get_json=lambda url, _headers: response_map(broken)[url],
        )
        assert failed["ok"] is False
        assert failed["enabled_agents"][1]["failures"] == ["not_ready", "stale_readiness"]
    print("protocol v0.5 maintenance preflight passed")


def agent(agent_id: str) -> dict:
    return {
        "agent_id": agent_id,
        "name": agent_id,
        "owner": agent_id,
        "enabled": True,
        "protocol_capabilities": [PROTOCOL_V05],
    }


def response_map(agents: list[dict]) -> dict[str, dict]:
    base = "https://relay.example/agentrelay/api"
    return {
        f"{base}/health": {"protocol": {"version": PROTOCOL_V05, "write_mode": "closed"}},
        f"{base}/protocols/current": {"version": PROTOCOL_V05, "write_mode": "closed"},
        f"{base}/admin/api/summary": {
            "protocol_version": PROTOCOL_V05,
            "invariant_violations": 0,
            "readiness": {"stale_enabled_agents": sum(not item["readiness_fresh"] for item in agents)},
        },
        f"{base}/admin/api/agents": {"agents": agents},
    }


def start_server(legacy_path: Path, v05_path: Path, port: int, *, mode: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "AGENTRELAY_HOST": "127.0.0.1",
        "AGENTRELAY_PORT": str(port),
        "AGENTRELAY_DB_PATH": str(legacy_path),
        "AGENTRELAY_V05_DB_PATH": str(v05_path),
        "AGENTRELAY_MUTATION_MODE": mode,
        "AGENTRELAY_TOKENS": "zac:zac-agent:test-agent-token",
        "AGENTRELAY_ADMIN_TOKEN": "test-admin-token",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "server.app"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_health(base_url: str) -> None:
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("closed-mode test Server did not become healthy")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
