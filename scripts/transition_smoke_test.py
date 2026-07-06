from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8797/agentrelay/api"
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
        db_path = f"{tmpdir}/agentrelay-transitions.sqlite3"
        env = os.environ.copy()
        env.update(
            {
                "AGENTRELAY_HOST": "127.0.0.1",
                "AGENTRELAY_PORT": "8797",
                "AGENTRELAY_DB_PATH": db_path,
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

            missing_next = create_task("transition-missing-next", max_turns=3)
            claim_task(missing_next)
            err = submit_artifact(
                missing_next,
                {
                    "protocol_version": "agent-collab-v0.3",
                    "idempotency_key": "missing-next-action",
                    "actor_agent_id": "frank-agent",
                    "intent": "provide_availability",
                    "artifact": artifact_body(),
                    "next_status": "delivery_pending",
                    "pending_on_agent_id": "zac-agent",
                },
                expected_status=400,
            )
            assert_error_contains(err, "next_action")

            terminal_artifact = create_task("transition-terminal-artifact", max_turns=3)
            claim_task(terminal_artifact)
            err = submit_artifact(
                terminal_artifact,
                {
                    "protocol_version": "agent-collab-v0.3",
                    "idempotency_key": "artifact-terminal-state",
                    "actor_agent_id": "frank-agent",
                    "intent": "provide_availability",
                    "artifact": artifact_body(),
                    "next_status": "completed",
                    "pending_on_agent_id": "zac-agent",
                    "next_action": "Zac should confirm.",
                },
                expected_status=400,
            )
            assert_error_contains(err, "terminal")

            self_loop = create_task("transition-self-loop-handoff", max_turns=3)
            claim_task(self_loop)
            after_self_loop_guard = submit_artifact(
                self_loop,
                {
                    "protocol_version": "agent-collab-v0.3",
                    "idempotency_key": "artifact-self-loop-handoff",
                    "actor_agent_id": "frank-agent",
                    "intent": "provide_availability",
                    "artifact": artifact_body(),
                    "next_status": "delivery_pending",
                    "pending_on_agent_id": "frank-agent",
                    "next_action": "Requester should evaluate this result.",
                },
            )["data"]["task"]
            if after_self_loop_guard["pending_on_agent_id"] != "zac-agent":
                raise AssertionError("artifact handoff should not keep delivery_pending on the actor agent")

            bad_authority = create_task("transition-bad-authority", max_turns=3)
            claim_task(bad_authority)
            submit_valid_artifact(bad_authority)
            err = close_task(
                bad_authority,
                {
                    "protocol_version": "agent-collab-v0.3",
                    "idempotency_key": "bad-human-authority",
                    "closed_by_agent_id": "zac-agent",
                    "completion_authority": {
                        "type": "human",
                        "owner_id": "zac",
                        "via_agent_id": "frank-agent",
                        "approval_ref": "wrong-via-agent",
                    },
                    "terminal_reason": "Bad authority should fail.",
                },
                expected_status=400,
            )
            assert_error_contains(err, "via")

            max_turns = create_task("transition-max-turns", max_turns=1)
            claim_task(max_turns)
            submit_valid_artifact(max_turns)
            get_json(f"{BASE_URL}/workers/zac-agent/claim", AGENT_A_HEADERS)
            err = submit_artifact(
                max_turns,
                {
                    "protocol_version": "agent-collab-v0.3",
                    "idempotency_key": "exceed-max-turns",
                    "actor_agent_id": "zac-agent",
                    "intent": "request_clarification",
                    "artifact": artifact_body("Need another time."),
                    "next_status": "delivery_pending",
                    "pending_on_agent_id": "frank-agent",
                    "next_action": "Frank should propose another time.",
                },
                headers=AGENT_A_HEADERS,
                expected_status=400,
            )
            assert_error_contains(err, "max_turns")
            failed_after_max_turns = get_json(
                f"{BASE_URL}/tasks/{max_turns}",
                AGENT_A_HEADERS,
            )["data"]["task"]
            if failed_after_max_turns["status"] != "failed":
                raise AssertionError("max_turns violation should terminalize the task as failed")
            if failed_after_max_turns["pending_on_agent_id"] is not None:
                raise AssertionError("max_turns violation should clear pending_on_agent_id")
            pending_after_max_turns = get_json(
                f"{BASE_URL}/workers/frank-agent/pending",
                AGENT_B_HEADERS,
            )["data"]["tasks"]
            if any(task_id_from_summary(task) == max_turns for task in pending_after_max_turns):
                raise AssertionError("max_turns failed task should not remain in worker pending queue")

            stale_max_turns = create_task("transition-stale-max-turns", max_turns=1)
            force_exhausted_non_owner_task(db_path, stale_max_turns)
            stale_pending = get_json(
                f"{BASE_URL}/workers/frank-agent/pending",
                AGENT_B_HEADERS,
            )["data"]["tasks"]
            if any(task_id_from_summary(task) == stale_max_turns for task in stale_pending):
                raise AssertionError("stale max_turns task should not be exposed as pending")
            stale_failed = get_json(
                f"{BASE_URL}/tasks/{stale_max_turns}",
                AGENT_A_HEADERS,
            )["data"]["task"]
            if stale_failed["status"] != "failed":
                raise AssertionError("stale max_turns task should be terminalized as failed")

            closed_task = create_task("transition-terminal-lock", max_turns=3)
            claim_task(closed_task)
            submit_valid_artifact(closed_task)
            close_task(
                closed_task,
                {
                    "protocol_version": "agent-collab-v0.3",
                    "idempotency_key": "valid-human-close",
                    "closed_by_agent_id": "zac-agent",
                    "completion_authority": {
                        "type": "human",
                        "owner_id": "zac",
                        "via_agent_id": "zac-agent",
                        "approval_ref": "zac-approved",
                    },
                    "terminal_reason": "Both owners accepted the time.",
                },
            )
            err = submit_artifact(
                closed_task,
                {
                    "protocol_version": "agent-collab-v0.3",
                    "idempotency_key": "artifact-after-close",
                    "actor_agent_id": "frank-agent",
                    "intent": "provide_availability",
                    "artifact": artifact_body(),
                    "next_status": "delivery_pending",
                    "pending_on_agent_id": "zac-agent",
                    "next_action": "Should not be accepted.",
                },
                expected_status=400,
            )
            assert_error_contains(err, "terminal")

            print(json.dumps({"ok": True, "checked": 6}, indent=2))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def create_task(suffix: str, max_turns: int) -> str:
    payload = {
        "protocol_version": "agent-collab-v0.3",
        "idempotency_key": f"create-{suffix}",
        "task_type": "meeting.schedule",
        "subject": f"Transition smoke {suffix}",
        "requester_agent_id": "zac-agent",
        "target_agent_id": "frank-agent",
        "done_criteria": "Both owners agree on one time.",
        "completion_owner_agent_id": "zac-agent",
        "pending_on_agent_id": "frank-agent",
        "next_action": "Frank should return availability.",
        "max_turns": max_turns,
        "message": {
            "actor_agent_id": "zac-agent",
            "intent": "request_availability",
            "parts": [{"kind": "text", "text": "Please propose a meeting time."}],
        },
    }
    return post_json(f"{BASE_URL}/tasks", payload, AGENT_A_HEADERS, expected_status=201)["data"]["task"]["task_id"]


def claim_task(task_id: str) -> None:
    claimed = get_json(f"{BASE_URL}/workers/frank-agent/claim", AGENT_B_HEADERS)["data"]["task"]
    if claimed["task_id"] != task_id:
        raise AssertionError(f"claimed wrong task: {claimed['task_id']} expected {task_id}")


def submit_valid_artifact(task_id: str) -> None:
    submit_artifact(
        task_id,
        {
            "protocol_version": "agent-collab-v0.3",
            "idempotency_key": f"artifact-{task_id}",
            "actor_agent_id": "frank-agent",
            "intent": "provide_availability",
            "artifact": artifact_body(),
            "next_status": "delivery_pending",
            "pending_on_agent_id": "zac-agent",
            "next_action": "Zac should confirm the proposed time.",
        },
    )


def force_exhausted_non_owner_task(db_path: str, task_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'claimed',
                pending_on_agent_id = 'frank-agent',
                turn_count = max_turns,
                claimed_by = 'frank-agent'
            WHERE task_id = ?
            """,
            (task_id,),
        )


def task_id_from_summary(task: dict) -> str | None:
    return task.get("task_id") or task.get("taskId")


def artifact_body(summary: str = "Frank can meet Monday 10:30.") -> dict:
    return {
        "kind": "availability_response",
        "summary": summary,
        "parts": [{"kind": "text", "text": summary}],
        "source_refs": [
            {
                "type": "owner_confirmation",
                "label": "Owner confirmed",
                "visibility": "redacted",
            }
        ],
    }


def submit_artifact(
    task_id: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    expected_status: int = 201,
) -> dict:
    return post_json(
        f"{BASE_URL}/tasks/{task_id}/artifacts",
        payload,
        headers or AGENT_B_HEADERS,
        expected_status=expected_status,
    )


def close_task(task_id: str, payload: dict, expected_status: int = 200) -> dict:
    return post_json(
        f"{BASE_URL}/tasks/{task_id}/close",
        payload,
        AGENT_A_HEADERS,
        expected_status=expected_status,
    )


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


def assert_error_contains(payload: dict, expected: str) -> None:
    if payload.get("ok") is not False:
        raise AssertionError(f"expected error envelope, got: {payload}")
    message = payload.get("error", {}).get("message", "")
    if expected not in message:
        raise AssertionError(f"expected {expected!r} in error message, got: {message!r}")


if __name__ == "__main__":
    main()
