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
BASE_URL = "http://127.0.0.1:8796/agentrelay/api"
AGENT_A_HEADERS = {
    "Authorization": "Bearer zac-token",
    "X-AgentRelay-Agent-Id": "zac-agent",
    "X-AgentRelay-Username": "zac",
    "X-AgentRelay-Envelope": "v0.3",
}
AGENT_B_HEADERS = {
    "Authorization": "Bearer frank-token",
    "X-AgentRelay-Agent-Id": "frank-agent",
    "X-AgentRelay-Username": "frank",
    "X-AgentRelay-Envelope": "v0.3",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_HOST": "127.0.0.1",
                "AGENTRELAY_PORT": "8796",
                "AGENTRELAY_DB_PATH": f"{tmpdir}/agentrelay-protocol-v03.sqlite3",
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
            invalid = post_json(
                f"{BASE_URL}/tasks",
                {
                    "protocol_version": "agent-collab-v0.3",
                    "idempotency_key": "invalid-v03-create",
                    "requester_agent_id": "zac-agent",
                    "target_agent_id": "frank-agent",
                    "message": {
                        "actor_agent_id": "zac-agent",
                        "intent": "request_availability",
                        "parts": [{"kind": "text", "text": "bad"}],
                    },
                },
                AGENT_A_HEADERS,
                expected_status=400,
            )
            assert_envelope_error(invalid, "VALIDATION_ERROR", "task_type")

            created_env = post_json(
                f"{BASE_URL}/tasks",
                {
                    "protocol_version": "agent-collab-v0.3",
                    "idempotency_key": "create-meeting-task-v03",
                    "task_type": "meeting.schedule",
                    "subject": "Find an online meeting time",
                    "requester_agent_id": "zac-agent",
                    "target_agent_id": "frank-agent",
                    "done_criteria": {
                        "type": "meeting_time_agreed",
                        "description": "Both owners agree on one 30-minute online meeting time.",
                        "required_outputs": ["start_time", "end_time", "timezone"],
                    },
                    "completion_owner_agent_id": "zac-agent",
                    "pending_on_agent_id": "frank-agent",
                    "next_action": "Frank agent should check availability and return candidate times.",
                    "max_turns": 6,
                    "message": {
                        "actor_agent_id": "zac-agent",
                        "intent": "request_availability",
                        "parts": [{"kind": "text", "text": "Please find a 30-minute slot next week."}],
                    },
                    "thread_binding": {
                        "agent_id": "zac-agent",
                        "thread_role": "requester_origin",
                        "thread_id": "zac-thread-v03",
                    },
                },
                AGENT_A_HEADERS,
                expected_status=201,
            )
            assert_success_envelope(created_env)
            task = created_env["data"]["task"]
            task_id = task["task_id"]
            if created_env["next_action"]["agent_id"] != "frank-agent":
                raise AssertionError("create envelope should point next_action at frank-agent")
            if task["requester_thread_id"] != "zac-thread-v03":
                raise AssertionError("v0.3 thread_binding did not populate requester_thread_id")

            frank_events = get_json(
                f"{BASE_URL}/workers/frank-agent/events",
                AGENT_B_HEADERS,
            )
            assert_success_envelope(frank_events)
            if frank_events["data"]["events"][0]["task_id"] != task_id:
                raise AssertionError("frank-agent did not receive a v0.3 event for the task")

            ack_env = post_json(
                f"{BASE_URL}/workers/frank-agent/events/{frank_events['data']['events'][0]['event_id']}/ack",
                {
                    "taskId": task_id,
                    "deliveryState": "done",
                    "threadId": "frank-thread-v03",
                },
                AGENT_B_HEADERS,
            )
            assert_success_envelope(ack_env)
            if ack_env["data"]["event"]["acked_at"] is None:
                raise AssertionError("v0.3 ack response did not ack event")

            claimed_env = post_json(
                f"{BASE_URL}/workers/frank-agent/tasks/{task_id}/claim",
                {},
                AGENT_B_HEADERS,
            )
            assert_success_envelope(claimed_env)
            if claimed_env["data"]["task"]["task_id"] != task_id:
                raise AssertionError("frank-agent did not precisely claim v0.3 task")

            artifact_env = post_json(
                f"{BASE_URL}/tasks/{task_id}/artifacts",
                {
                    "protocol_version": "agent-collab-v0.3",
                    "idempotency_key": "artifact-task-v03-availability",
                    "actor_agent_id": "frank-agent",
                    "intent": "provide_availability",
                    "artifact": {
                        "kind": "availability_response",
                        "summary": "Frank can meet Monday 10:30-11:00 Asia/Shanghai.",
                        "parts": [
                            {
                                "kind": "structured_availability",
                                "slots": [
                                    {
                                        "start_time": "2026-07-06T10:30:00+08:00",
                                        "end_time": "2026-07-06T11:00:00+08:00",
                                        "timezone": "Asia/Shanghai",
                                        "confidence": "confirmed",
                                    }
                                ],
                            }
                        ],
                        "source_refs": [
                            {
                                "type": "owner_confirmation",
                                "label": "Frank owner confirmed availability",
                                "summary": "Owner confirmed the primary slot.",
                                "visibility": "redacted",
                                "uri": "local://frank/private-thread/123",
                                "metadata": {"private_note": "do not relay"},
                            },
                            {
                                "type": "calendar_lookup",
                                "label": "Frank public work calendar",
                                "summary": "Calendar showed the slot open.",
                                "visibility": "public",
                                "uri": "calendar://frank/work",
                                "metadata": {"calendar_id": "work"},
                            },
                            {
                                "type": "message",
                                "label": "Private owner-agent exchange",
                                "summary": "Frank privately confirmed.",
                                "visibility": "private",
                                "uri": "local://frank/private-message/999",
                            }
                        ],
                    },
                    "next_status": "delivery_pending",
                    "pending_on_agent_id": "zac-agent",
                    "next_action": "Zac agent should ask its owner to accept or propose alternatives.",
                },
                AGENT_B_HEADERS,
                expected_status=201,
            )
            assert_success_envelope(artifact_env)
            if artifact_env["data"]["task"]["status"] != "delivery_pending":
                raise AssertionError("artifact should not close the task")
            if artifact_env["next_action"]["agent_id"] != "zac-agent":
                raise AssertionError("artifact envelope should point next_action at zac-agent")

            close_env = post_json(
                f"{BASE_URL}/tasks/{task_id}/close",
                {
                    "protocol_version": "agent-collab-v0.3",
                    "idempotency_key": "close-task-v03-human-approved",
                    "closed_by_agent_id": "zac-agent",
                    "completion_authority": {
                        "type": "human",
                        "owner_id": "zac",
                        "via_agent_id": "zac-agent",
                        "approval_ref": "zac-local-confirmation-456",
                        "summary": "Zac accepted the Monday 10:30 slot.",
                        "visibility": "redacted",
                        "source_refs": [
                            {
                                "type": "owner_confirmation",
                                "label": "Zac local confirmation",
                                "summary": "Zac accepted the proposed slot.",
                                "visibility": "redacted",
                                "uri": "local://zac/private-thread/456",
                            }
                        ],
                    },
                    "terminal_reason": "Both owners accepted the same online meeting time.",
                    "final_artifact": {
                        "kind": "meeting_confirmation",
                        "parts": [
                            {
                                "kind": "meeting_time",
                                "start_time": "2026-07-06T10:30:00+08:00",
                                "end_time": "2026-07-06T11:00:00+08:00",
                                "timezone": "Asia/Shanghai",
                            }
                        ],
                        "source_refs": [
                            {
                                "type": "owner_confirmation",
                                "label": "Both owners approved",
                                "summary": "Requester-side agent recorded final approval.",
                                "visibility": "redacted",
                            }
                        ],
                    },
                },
                AGENT_A_HEADERS,
            )
            assert_success_envelope(close_env)
            if close_env["data"]["task"]["status"] != "completed":
                raise AssertionError("completion owner did not close v0.3 task")
            if close_env["next_action"]["type"] != "none":
                raise AssertionError("closed task should have no next action")

            events = get_json(f"{BASE_URL}/tasks/{task_id}/events", AGENT_A_HEADERS)["data"]["events"]
            created_event = find_event(events, "task.created")
            artifact_event = find_event(events, "artifact.submitted")
            closed_event = find_event(events, "task.completed")
            if created_event["payload"].get("protocol_version") != "agent-collab-v0.3":
                raise AssertionError("task.created missing v0.3 protocol_version")
            if artifact_event["payload"].get("source_refs", [{}])[0].get("type") != "owner_confirmation":
                raise AssertionError("artifact.submitted missing source_refs")
            redacted_ref = artifact_event["payload"]["source_refs"][0]
            if redacted_ref.get("uri") or redacted_ref.get("metadata"):
                raise AssertionError("redacted source_ref should not expose uri or metadata")
            if not redacted_ref.get("redacted"):
                raise AssertionError("redacted source_ref should be marked redacted")
            public_ref = artifact_event["payload"]["source_refs"][1]
            if public_ref.get("uri") != "calendar://frank/work" or public_ref.get("metadata", {}).get("calendar_id") != "work":
                raise AssertionError("public source_ref should preserve uri and metadata")
            private_ref = artifact_event["payload"]["source_refs"][2]
            if private_ref.get("uri") or private_ref.get("metadata") or not private_ref.get("redacted"):
                raise AssertionError("private source_ref should hide uri and metadata")
            authority = closed_event["payload"].get("completion_authority") or {}
            if authority.get("type") != "human":
                raise AssertionError("task.completed missing human completion authority")
            if authority.get("source_refs", [{}])[0].get("uri"):
                raise AssertionError("completion authority source_refs should be redacted")
            final_artifact = closed_event["payload"].get("final_artifact") or {}
            if final_artifact.get("source_refs", [{}])[0].get("type") != "owner_confirmation":
                raise AssertionError("final artifact missing source refs")

            timeline = get_json(f"{BASE_URL}/tasks/{task_id}/timeline", AGENT_A_HEADERS)["data"]["timeline"]
            if timeline["summary"]["total_entries"] != len(events):
                raise AssertionError("timeline entry count should match task events")
            artifact_entry = find_timeline_entry(timeline["entries"], "artifact.submitted")
            if artifact_entry["category"] != "artifact":
                raise AssertionError("artifact timeline entry should use artifact category")
            if artifact_entry["source_refs"][0]["type"] != "owner_confirmation":
                raise AssertionError("timeline artifact entry missing source_refs")
            if artifact_entry["source_refs"][0].get("uri"):
                raise AssertionError("timeline should use sanitized source_refs")
            close_entry = find_timeline_entry(timeline["entries"], "task.completed")
            if close_entry["category"] != "completion":
                raise AssertionError("completed timeline entry should use completion category")
            if close_entry["completion_authority"]["type"] != "human":
                raise AssertionError("timeline close entry missing human completion authority")
            if close_entry["completion_authority"]["source_refs"][0].get("uri"):
                raise AssertionError("timeline completion authority should use sanitized source_refs")

            defaulted_env = post_json(
                f"{BASE_URL}/tasks",
                {
                    "idempotency_key": "create-defaults-to-v03",
                    "task_type": "meeting.schedule",
                    "subject": "Default protocol version smoke",
                    "requester_agent_id": "zac-agent",
                    "target_agent_id": "frank-agent",
                    "done_criteria": "Requester validates the default protocol version.",
                    "completion_owner_agent_id": "zac-agent",
                    "pending_on_agent_id": "frank-agent",
                    "next_action": "Frank should acknowledge.",
                    "message": {
                        "actor_agent_id": "zac-agent",
                        "intent": "request",
                        "parts": [{"kind": "text", "text": "Confirm default protocol version."}],
                    },
                },
                AGENT_A_HEADERS,
                expected_status=201,
            )
            assert_success_envelope(defaulted_env)
            defaulted_task_id = defaulted_env["data"]["task"]["task_id"]
            defaulted_close_env = post_json(
                f"{BASE_URL}/tasks/{defaulted_task_id}/close",
                {
                    "idempotency_key": "close-defaults-to-v03",
                    "closed_by_agent_id": "zac-agent",
                    "terminal_reason": "Default protocol version verified.",
                },
                AGENT_A_HEADERS,
            )
            assert_success_envelope(defaulted_close_env)
            defaulted_events = get_json(
                f"{BASE_URL}/tasks/{defaulted_task_id}/events",
                AGENT_A_HEADERS,
            )["data"]["events"]
            defaulted_created = find_event(defaulted_events, "task.created")
            defaulted_closed = find_event(defaulted_events, "task.completed")
            if defaulted_created["payload"].get("protocol_version") != "agent-collab-v0.3":
                raise AssertionError("default task.created should use v0.3 protocol_version")
            if defaulted_closed["payload"].get("protocol_version") != "agent-collab-v0.3":
                raise AssertionError("default task.completed should use v0.3 protocol_version")

            print(json.dumps({"ok": True, "taskId": task_id, "status": close_env["data"]["task"]["status"]}, indent=2))
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


def get_json(url: str, headers: dict[str, str] | None = None, expected_status: int = 200) -> dict:
    return request_json("GET", url, headers=headers, expected_status=expected_status)


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
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
            if response.status != expected_status:
                raise AssertionError(f"{method} {url} returned {response.status}, expected {expected_status}: {body}")
            return body
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        if exc.code != expected_status:
            raise AssertionError(f"{method} {url} returned {exc.code}, expected {expected_status}: {body}") from exc
        return body


def assert_success_envelope(payload: dict) -> None:
    if payload.get("ok") is not True or not isinstance(payload.get("data"), dict):
        raise AssertionError(f"expected success envelope, got: {payload}")
    if payload.get("meta", {}).get("envelope") != "v0.3":
        raise AssertionError("success envelope missing v0.3 meta")


def assert_envelope_error(payload: dict, code: str, field: str) -> None:
    if payload.get("ok") is not False:
        raise AssertionError(f"expected error envelope, got: {payload}")
    error = payload.get("error") or {}
    if error.get("code") != code:
        raise AssertionError(f"expected error code {code}, got: {error}")
    if error.get("detail", {}).get("field") != field:
        raise AssertionError(f"expected error field {field}, got: {error}")
    if not error.get("hint"):
        raise AssertionError("error envelope should include hint")


def find_event(events: list[dict], event_type: str) -> dict:
    for event in events:
        if event["event_type"] == event_type:
            return event
    raise AssertionError(f"missing event: {event_type}")


def find_timeline_entry(entries: list[dict], event_type: str) -> dict:
    for entry in entries:
        if entry["event_type"] == event_type:
            return entry
    raise AssertionError(f"missing timeline entry: {event_type}")


if __name__ == "__main__":
    main()
