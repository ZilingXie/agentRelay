from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOCAL_BASE_URL = "http://127.0.0.1:8799/agentrelay/api"
PROTOCOL_VERSION = "agent-collab-v0.3"


class RunnerError(RuntimeError):
    pass


def main() -> None:
    args = parse_args()
    if args.base_url:
        config = config_from_args_or_env(args)
        result = run_conformance(config)
        print_result(result)
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_HOST": "127.0.0.1",
                "AGENTRELAY_PORT": "8799",
                "AGENTRELAY_DB_PATH": f"{tmpdir}/agentrelay-v03-conformance.sqlite3",
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
            config = ConformanceConfig(
                base_url=LOCAL_BASE_URL,
                agent_a=AgentIdentity("zac-agent", "zac", "zac-token"),
                agent_b=AgentIdentity("frank-agent", "frank", "frank-token"),
                timeout_seconds=args.timeout_seconds,
            )
            result = run_conformance(config)
            print_result(result)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


class AgentIdentity:
    def __init__(self, agent_id: str, username: str, token: str):
        self.agent_id = agent_id
        self.username = username
        self.token = token

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-AgentRelay-Agent-Id": self.agent_id,
            "X-AgentRelay-Username": self.username,
            "X-AgentRelay-Envelope": "v0.3",
        }


class ConformanceConfig:
    def __init__(
        self,
        *,
        base_url: str,
        agent_a: AgentIdentity,
        agent_b: AgentIdentity,
        timeout_seconds: int,
    ):
        self.base_url = base_url.rstrip("/")
        self.agent_a = agent_a
        self.agent_b = agent_b
        self.timeout_seconds = timeout_seconds


def run_conformance(config: ConformanceConfig) -> dict[str, Any]:
    wait_for_health(config)
    run_id = uuid.uuid4().hex[:12]

    created_env = post_json(
        config,
        "/tasks",
        meeting_task_create_payload(config, run_id),
        config.agent_a.headers(),
        expected_status=201,
    )
    created = unwrap_data(created_env)
    task = created["task"]
    task_id = task["task_id"]
    assert_equal(task["requester_agent_id"], config.agent_a.agent_id, "requester agent was not stored")
    assert_equal(task["target_agent_id"], config.agent_b.agent_id, "target agent was not stored")
    assert_equal(task["completion_owner_agent_id"], config.agent_a.agent_id, "completion owner must be requester")
    assert_equal(task["pending_on_agent_id"], config.agent_b.agent_id, "task should start pending on target")
    assert_equal(created_env["next_action"]["agent_id"], config.agent_b.agent_id, "create next_action agent mismatch")

    b_event = find_agent_event(config, config.agent_b, task_id)
    ack_agent_event(config, config.agent_b, b_event, task_id, f"{config.agent_b.agent_id}-conformance-thread")

    claimed_b = unwrap_data(
        post_json(
            config,
            f"/workers/{quote(config.agent_b.agent_id)}/tasks/{quote(task_id)}/claim",
            {},
            config.agent_b.headers(),
        )
    )["task"]
    assert_equal(claimed_b["task_id"], task_id, "target precise claim returned wrong task")
    assert_equal(claimed_b["status"], "claimed", "target precise claim did not mark claimed")

    artifact_env = post_json(
        config,
        f"/tasks/{quote(task_id)}/artifacts",
        meeting_artifact_payload(config, run_id),
        config.agent_b.headers(),
        expected_status=201,
    )
    artifact_task = unwrap_data(artifact_env)["task"]
    assert_equal(artifact_task["status"], "delivery_pending", "artifact should not complete the task")
    assert_equal(artifact_task["pending_on_agent_id"], config.agent_a.agent_id, "artifact should hand off to requester")
    assert_equal(artifact_env["next_action"]["agent_id"], config.agent_a.agent_id, "artifact next_action agent mismatch")

    a_event = find_agent_event(config, config.agent_a, task_id)
    ack_agent_event(config, config.agent_a, a_event, task_id, f"{config.agent_a.agent_id}-conformance-thread")

    claimed_a = unwrap_data(
        post_json(
            config,
            f"/workers/{quote(config.agent_a.agent_id)}/tasks/{quote(task_id)}/claim",
            {},
            config.agent_a.headers(),
        )
    )["task"]
    assert_equal(claimed_a["task_id"], task_id, "requester precise claim returned wrong task")

    close_env = post_json(
        config,
        f"/tasks/{quote(task_id)}/close",
        close_payload(config, run_id),
        config.agent_a.headers(),
    )
    closed_task = unwrap_data(close_env)["task"]
    assert_equal(closed_task["status"], "completed", "completion owner did not close the task")
    assert_equal(close_env["next_action"]["type"], "none", "closed task should not have a next action")

    events = unwrap_data(get_json(config, f"/tasks/{quote(task_id)}/events", config.agent_a.headers()))["events"]
    timeline = unwrap_data(get_json(config, f"/tasks/{quote(task_id)}/timeline", config.agent_a.headers()))["timeline"]
    assert_protocol_events(events)
    assert_timeline(timeline, events)

    return {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "task_id": task_id,
        "status": closed_task["status"],
        "agents": {
            "requester": config.agent_a.agent_id,
            "target": config.agent_b.agent_id,
        },
        "checked": [
            "health",
            "task.create.v0.3",
            "target.agent_event",
            "target.precise_claim",
            "artifact.submit.v0.3",
            "requester.agent_event",
            "requester.precise_claim",
            "task.close.v0.3",
            "task.events",
            "task.timeline",
        ],
        "event_types": [event["event_type"] for event in events],
        "timeline_entries": timeline["summary"]["total_entries"],
    }


def meeting_task_create_payload(config: ConformanceConfig, run_id: str) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "idempotency_key": f"conformance-create-{run_id}",
        "task_type": "conformance.meeting.schedule",
        "subject": f"AgentRelay v0.3 conformance {run_id}",
        "requester_agent_id": config.agent_a.agent_id,
        "target_agent_id": config.agent_b.agent_id,
        "done_criteria": {
            "type": "conformance_passed",
            "description": "Create, claim, artifact, handoff, close, and timeline checks all pass.",
            "required_outputs": ["artifact", "completion_authority", "timeline"],
        },
        "completion_owner_agent_id": config.agent_a.agent_id,
        "pending_on_agent_id": config.agent_b.agent_id,
        "next_action": "Target agent should submit a conformance artifact.",
        "max_turns": 6,
        "message": {
            "actor_agent_id": config.agent_a.agent_id,
            "intent": "request_conformance_artifact",
            "parts": [
                {
                    "kind": "text",
                    "text": "Please return a conformance artifact and hand the task back to requester.",
                }
            ],
        },
        "thread_binding": {
            "agent_id": config.agent_a.agent_id,
            "thread_role": "requester_origin",
            "thread_id": f"{config.agent_a.agent_id}-conformance-origin-{run_id}",
        },
    }


def meeting_artifact_payload(config: ConformanceConfig, run_id: str) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "idempotency_key": f"conformance-artifact-{run_id}",
        "actor_agent_id": config.agent_b.agent_id,
        "intent": "provide_conformance_artifact",
        "artifact": {
            "kind": "conformance_result",
            "summary": "Target agent completed the conformance artifact step.",
            "parts": [
                {
                    "kind": "conformance_result",
                    "result": "artifact_submitted",
                    "run_id": run_id,
                }
            ],
            "source_refs": [
                {
                    "type": "tool_result",
                    "label": "Conformance runner local assertion",
                    "summary": "The target-side runner submitted this artifact.",
                    "visibility": "redacted",
                    "uri": f"local://agentrelay/conformance/{run_id}",
                }
            ],
        },
        "next_status": "delivery_pending",
        "pending_on_agent_id": config.agent_a.agent_id,
        "next_action": "Requester agent should close after validating the conformance artifact.",
    }


def close_payload(config: ConformanceConfig, run_id: str) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "idempotency_key": f"conformance-close-{run_id}",
        "closed_by_agent_id": config.agent_a.agent_id,
        "completion_authority": {
            "type": "agent",
            "summary": "Requester agent verified the conformance artifact and timeline.",
            "visibility": "redacted",
        },
        "terminal_reason": "AgentRelay v0.3 conformance flow completed.",
        "final_artifact": {
            "kind": "conformance_summary",
            "parts": [
                {
                    "kind": "conformance_summary",
                    "run_id": run_id,
                    "status": "passed",
                }
            ],
        },
    }


def find_agent_event(config: ConformanceConfig, agent: AgentIdentity, task_id: str) -> dict[str, Any]:
    deadline = time.time() + config.timeout_seconds
    while time.time() < deadline:
        data = unwrap_data(
            get_json(
                config,
                f"/workers/{quote(agent.agent_id)}/events?include_acked=true&limit=50",
                agent.headers(),
            )
        )
        for event in data.get("events", []):
            if event.get("task_id") == task_id:
                assert_equal(event["event_type"], "task.pending", "agent event type mismatch")
                if event.get("payload", {}).get("subject"):
                    raise RunnerError("agent event push payload exposed task subject")
                if event.get("payload", {}).get("payloadRef", {}).get("method") != "GET":
                    raise RunnerError("agent event missing GET payloadRef")
                return event
        time.sleep(0.2)
    raise RunnerError(f"timed out waiting for task.pending event for {agent.agent_id}")


def ack_agent_event(
    config: ConformanceConfig,
    agent: AgentIdentity,
    event: dict[str, Any],
    task_id: str,
    thread_id: str,
) -> None:
    acked = unwrap_data(
        post_json(
            config,
            f"/workers/{quote(agent.agent_id)}/events/{quote(event['event_id'])}/ack",
            {
                "taskId": task_id,
                "deliveryState": "done",
                "threadId": thread_id,
            },
            agent.headers(),
        )
    )["event"]
    assert_equal(acked["delivery_state"], "done", "agent event ack did not mark done")
    if not acked.get("acked_at"):
        raise RunnerError("agent event ack did not set acked_at")


def assert_protocol_events(events: list[dict[str, Any]]) -> None:
    event_types = [event["event_type"] for event in events]
    for required in ("task.created", "task.claimed", "artifact.submitted", "ownership.transferred", "task.completed"):
        if required not in event_types:
            raise RunnerError(f"missing task event: {required}")
    for event_type in ("task.created", "artifact.submitted", "ownership.transferred", "task.completed"):
        event = find_event(events, event_type)
        if event.get("payload", {}).get("protocol_version") != PROTOCOL_VERSION:
            raise RunnerError(f"{event_type} missing {PROTOCOL_VERSION}")
    artifact_event = find_event(events, "artifact.submitted")
    source_ref = artifact_event["payload"].get("source_refs", [{}])[0]
    if source_ref.get("uri") or not source_ref.get("redacted"):
        raise RunnerError("artifact source_refs were not redacted in audit payload")
    close_event = find_event(events, "task.completed")
    authority = close_event["payload"].get("completion_authority") or {}
    assert_equal(authority.get("type"), "agent", "close event missing agent completion authority")


def assert_timeline(timeline: dict[str, Any], events: list[dict[str, Any]]) -> None:
    if timeline["summary"]["total_entries"] != len(events):
        raise RunnerError("timeline entry count does not match task event count")
    event_types = [entry["event_type"] for entry in timeline["entries"]]
    for required in ("task.created", "artifact.submitted", "ownership.transferred", "task.completed"):
        if required not in event_types:
            raise RunnerError(f"timeline missing event: {required}")
    completed = find_event(timeline["entries"], "task.completed")
    if completed["category"] != "completion":
        raise RunnerError("task.completed timeline entry should be completion category")


def find_event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    for event in events:
        if event["event_type"] == event_type:
            return event
    raise RunnerError(f"missing event: {event_type}")


def wait_for_health(config: ConformanceConfig) -> None:
    deadline = time.time() + config.timeout_seconds
    while time.time() < deadline:
        try:
            payload = get_json(config, "/health", {})
            if payload.get("ok"):
                return
        except Exception:
            time.sleep(0.2)
    raise RunnerError(f"relay health check failed at {config.base_url}/health")


def get_json(config: ConformanceConfig, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    return request_json(config, "GET", path, headers=headers)


def post_json(
    config: ConformanceConfig,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    expected_status: int = 200,
) -> dict[str, Any]:
    return request_json(config, "POST", path, payload=payload, headers=headers, expected_status=expected_status)


def request_json(
    config: ConformanceConfig,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(
        f"{config.base_url}{path}",
        data=data,
        method=method,
        headers=request_headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=config.timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
            if response.status != expected_status:
                raise RunnerError(
                    f"{method} {path} returned {response.status}, expected {expected_status}: {body}"
                )
            return body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RunnerError(f"{method} {path} failed {exc.code}: {body}") from exc


def unwrap_data(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("ok") is True and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise RunnerError(f"{message}: expected {expected!r}, got {actual!r}")


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def print_result(result: dict[str, Any]) -> None:
    print(json.dumps(result, indent=2, sort_keys=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run AgentRelay Protocol v0.3 conformance against a temporary local relay "
            "or a real relay with two disposable test agents."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENTRELAY_CONFORMANCE_BASE_URL"),
        help="Relay API base URL, for example https://server.stellarix.space/agentrelay/api. "
        "If omitted, a temporary local relay is started.",
    )
    parser.add_argument("--agent-a-id", default=os.environ.get("AGENTRELAY_CONFORMANCE_AGENT_A_ID"))
    parser.add_argument("--agent-a-username", default=os.environ.get("AGENTRELAY_CONFORMANCE_AGENT_A_USERNAME"))
    parser.add_argument("--agent-a-token", default=os.environ.get("AGENTRELAY_CONFORMANCE_AGENT_A_TOKEN"))
    parser.add_argument("--agent-b-id", default=os.environ.get("AGENTRELAY_CONFORMANCE_AGENT_B_ID"))
    parser.add_argument("--agent-b-username", default=os.environ.get("AGENTRELAY_CONFORMANCE_AGENT_B_USERNAME"))
    parser.add_argument("--agent-b-token", default=os.environ.get("AGENTRELAY_CONFORMANCE_AGENT_B_TOKEN"))
    parser.add_argument("--timeout-seconds", type=int, default=10)
    return parser.parse_args()


def config_from_args_or_env(args: argparse.Namespace) -> ConformanceConfig:
    missing = [
        name
        for name, value in {
            "agent-a-id": args.agent_a_id,
            "agent-a-username": args.agent_a_username,
            "agent-a-token": args.agent_a_token,
            "agent-b-id": args.agent_b_id,
            "agent-b-username": args.agent_b_username,
            "agent-b-token": args.agent_b_token,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit(
            "External conformance requires two disposable agent identities. "
            f"Missing: {', '.join(missing)}"
        )
    return ConformanceConfig(
        base_url=args.base_url,
        agent_a=AgentIdentity(args.agent_a_id, args.agent_a_username, args.agent_a_token),
        agent_b=AgentIdentity(args.agent_b_id, args.agent_b_username, args.agent_b_token),
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    try:
        main()
    except RunnerError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        raise SystemExit(1) from exc
