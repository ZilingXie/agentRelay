from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8804/agentrelay/api"
ZAC_HEADERS = {
    "Authorization": "Bearer zac-token",
    "X-AgentRelay-Agent-Id": "zac-agent",
    "X-AgentRelay-Username": "zac",
}
FRANK_HEADERS = {
    "Authorization": "Bearer frank-token",
    "X-AgentRelay-Agent-Id": "frank-agent",
    "X-AgentRelay-Username": "frank",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_HOST": "127.0.0.1",
                "AGENTRELAY_PORT": "8804",
                "AGENTRELAY_DB_PATH": f"{tmpdir}/agentrelay-install-healthcheck.sqlite3",
                "AGENTRELAY_TOKENS": "zac:zac-agent:zac-token,frank:frank-agent:frank-token",
            }
        )
        proc = subprocess.Popen(
            ["python3", "-m", "server.app"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            wait_for_health()
            expect_http_error(
                "install healthcheck without auth",
                401,
                "POST",
                f"{BASE_URL}/healthchecks/install",
                {},
            )
            expect_http_error(
                "install healthcheck requester spoof",
                403,
                "POST",
                f"{BASE_URL}/healthchecks/install",
                {"requester_agent_id": "frank-agent"},
                ZAC_HEADERS,
            )

            created = post_json(
                f"{BASE_URL}/healthchecks/install",
                {
                    "requester_agent_id": "zac-agent",
                    "requesterThreadId": "zac-install-health-thread",
                    "idempotency_key": "install-healthcheck-smoke",
                },
                ZAC_HEADERS,
                expected_status=201,
            )
            task = created["task"]
            task_id = task["task_id"]
            if task["requester_agent_id"] != "zac-agent":
                raise AssertionError("healthcheck task requester must come from auth")
            if task["target_agent_id"] != "agentrelay-healthcheck":
                raise AssertionError("healthcheck task target should be synthetic")
            if task["completion_owner_agent_id"] != "zac-agent":
                raise AssertionError("healthcheck completion owner should be requester")
            if task["pending_on_agent_id"] != "zac-agent":
                raise AssertionError("healthcheck should be pending on requester")
            artifacts = task["artifacts"]
            if len(artifacts) != 1:
                raise AssertionError("healthcheck should include one ACK artifact")
            artifact = artifacts[0]
            if artifact["from_agent_id"] != "agentrelay-healthcheck":
                raise AssertionError("ACK artifact should come from synthetic healthcheck actor")
            ack_text = "\n".join(part.get("text", "") for part in artifact["parts"])
            for expected in ("ACK from agentrelay-healthcheck", "requester=zac-agent", f"task={task_id}"):
                if expected not in ack_text:
                    raise AssertionError(f"ACK artifact missing {expected!r}: {ack_text}")

            events = get_json(f"{BASE_URL}/workers/zac-agent/events", ZAC_HEADERS)["events"]
            if len(events) != 1:
                raise AssertionError(f"expected one requester pending event, got {len(events)}")
            event = events[0]
            if event["event_type"] != "task.pending" or event["task_id"] != task_id:
                raise AssertionError(f"unexpected requester event: {event}")
            if event["payload"].get("reason") != "install.healthcheck":
                raise AssertionError(f"unexpected healthcheck event reason: {event}")

            agents = get_json(f"{BASE_URL}/agents", ZAC_HEADERS)["agents"]
            agent_ids = {agent["agent_id"] for agent in agents}
            if "agentrelay-healthcheck" not in agent_ids:
                raise AssertionError("synthetic healthcheck agent row was not created")

            print(json.dumps({"ok": True, "taskId": task_id, "eventId": event["event_id"]}, indent=2))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def wait_for_health() -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            payload = get_json(f"{BASE_URL}/health")
            if payload.get("ok"):
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    return request_json("GET", url, headers=headers)


def post_json(
    url: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
) -> dict:
    return request_json("POST", url, payload=payload, headers=headers, expected_status=expected_status)


def request_json(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    with urllib.request.urlopen(req, timeout=5) as response:
        if response.status != expected_status:
            raise AssertionError(f"{method} {url} returned {response.status}, expected {expected_status}")
        return json.loads(response.read().decode("utf-8"))


def expect_http_error(
    label: str,
    expected_status: int,
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    try:
        request_json(method, url, payload, headers)
    except urllib.error.HTTPError as exc:
        if exc.code != expected_status:
            body = exc.read().decode("utf-8")
            raise AssertionError(f"{label} returned {exc.code}, expected {expected_status}: {body}") from exc
        return
    raise AssertionError(f"{label} unexpectedly succeeded")


if __name__ == "__main__":
    main()
