from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


BASE_URL = "http://127.0.0.1:8787"


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else BASE_URL
    assert_ok("health", get_json(f"{base_url}/agentrelay/health"))
    task = post_json(
        f"{base_url}/agentrelay/tasks",
        {
            "contextId": "ctx_meeting_frank_smoke",
            "from": "zac-agent",
            "to": "frank-agent",
            "requesterThreadId": "zac-thread-abc",
            "subject": "Meeting availability",
            "doneCriteria": "Both Zac and Frank accept the same online meeting time.",
            "completionOwnerAgentId": "zac-agent",
            "message": {
                "role": "user",
                "parts": [
                    {
                        "kind": "text",
                        "text": "Zac wants a 30-minute online meeting with Frank. Please ask Frank when he is available.",
                    }
                ],
            },
            "humanBoundary": {
                "requiresHuman": True,
                "reason": "Frank must approve sharing availability.",
            },
        },
    )["task"]
    task_id = task["task_id"]
    if task["done_criteria"] != "Both Zac and Frank accept the same online meeting time.":
        raise AssertionError("done criteria was not stored")
    if task["completion_owner_agent_id"] != "zac-agent":
        raise AssertionError("completion owner was not stored")
    if task["pending_on_agent_id"] != "frank-agent":
        raise AssertionError("initial pending owner should be frank-agent")
    print(f"created {task_id}")

    claimed = get_json(f"{base_url}/agentrelay/workers/frank-agent/claim")["task"]
    if not claimed or claimed["task_id"] != task_id:
        raise AssertionError("frank-agent did not claim the created task")
    print(f"claimed {task_id}")

    with_thread = post_json(
        f"{base_url}/agentrelay/workers/frank-agent/tasks/{task_id}/thread",
        {"threadId": "frank-thread-123"},
    )["task"]
    if with_thread["target_thread_id"] != "frank-thread-123":
        raise AssertionError("target thread was not recorded")
    print("recorded frank thread")

    after_artifact = post_json(
        f"{base_url}/agentrelay/tasks/{task_id}/artifacts",
        {
            "from": "frank-agent",
            "to": "zac-agent",
            "artifact": {
                "kind": "meeting_availability",
                "parts": [
                    {
                        "kind": "text",
                        "text": "Frank is available Tuesday 10:00-11:00 or Thursday 15:00-16:00 China time.",
                    }
                ],
            },
        },
    )["task"]
    if after_artifact["status"] != "delivery_pending":
        raise AssertionError("artifact should not complete the task")
    if after_artifact["pending_on_agent_id"] != "zac-agent":
        raise AssertionError("artifact should transfer ownership back to zac-agent")
    if after_artifact["requester_thread_id"] != "zac-thread-abc":
        raise AssertionError("requester thread was not preserved")
    print("submitted artifact and transferred ownership to zac-agent")

    zac_claimed = get_json(f"{base_url}/agentrelay/workers/zac-agent/claim")["task"]
    if not zac_claimed or zac_claimed["task_id"] != task_id:
        raise AssertionError("zac-agent did not claim the returned task")
    print("zac-agent claimed returned task")

    delivered = post_json(
        f"{base_url}/agentrelay/tasks/{task_id}/deliveries",
        {
            "deliveredByAgentId": "zac-agent",
            "threadId": "zac-thread-abc",
            "deliveryStatus": "delivered",
            "pendingOnHumanId": "zac",
            "nextAction": "Ask Zac whether Frank's proposed time works.",
        },
    )["task"]
    if delivered["delivery_status"] != "delivered":
        raise AssertionError("delivery status was not recorded")
    if delivered["delivered_to_thread_id"] != "zac-thread-abc":
        raise AssertionError("delivery thread was not recorded")
    if delivered["status"] != "waiting_human":
        raise AssertionError("successful delivery should wait for human confirmation")
    if delivered["pending_on_human_id"] != "zac":
        raise AssertionError("successful delivery should wait on Zac")
    print("delivered reply to requester thread")

    expect_http_error(
        "non-owner close rejected",
        400,
        "POST",
        f"{base_url}/agentrelay/tasks/{task_id}/close",
        {
            "closedByAgentId": "frank-agent",
            "terminalReason": "Frank should not be able to close Zac-owned completion.",
        },
    )
    print("non-owner close rejected")

    completed = post_json(
        f"{base_url}/agentrelay/tasks/{task_id}/close",
        {
            "closedByAgentId": "zac-agent",
            "terminalReason": "Requester confirmed the proposed meeting time.",
        },
    )["task"]
    if completed["status"] != "completed":
        raise AssertionError("task was not completed")
    if completed["terminal_reason"] != "Requester confirmed the proposed meeting time.":
        raise AssertionError("terminal reason was not recorded")
    print("requester closed task")

    events = get_json(f"{base_url}/agentrelay/tasks/{task_id}/events")["events"]
    event_types = [event["event_type"] for event in events]
    for expected in [
        "task.created",
        "task.claimed",
        "thread.created",
        "artifact.submitted",
        "ownership.transferred",
        "reply.delivered",
        "task.completed",
    ]:
        if expected not in event_types:
            raise AssertionError(f"missing event: {expected}")
    print("events ok")
    print(json.dumps({"taskId": task_id, "status": completed["status"]}, indent=2))


def get_json(url: str) -> dict:
    return request_json("GET", url)


def post_json(url: str, payload: dict) -> dict:
    return request_json("POST", url, payload)


def request_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"{method} {url} failed: {exc.code} {body}") from exc


def expect_http_error(
    label: str,
    expected_status: int,
    method: str,
    url: str,
    payload: dict | None = None,
) -> None:
    try:
        request_json(method, url, payload)
    except RuntimeError as exc:
        if f"failed: {expected_status}" not in str(exc):
            raise AssertionError(f"{label} returned unexpected error: {exc}") from exc
        return
    raise AssertionError(f"{label} unexpectedly succeeded")


def assert_ok(label: str, payload: dict) -> None:
    if not payload.get("ok"):
        raise AssertionError(f"{label} failed: {payload}")


if __name__ == "__main__":
    main()
