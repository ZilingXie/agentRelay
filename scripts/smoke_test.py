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

    completed = post_json(
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
    if completed["status"] != "completed":
        raise AssertionError("task was not completed")
    if completed["requester_thread_id"] != "zac-thread-abc":
        raise AssertionError("requester thread was not preserved")
    print("submitted artifact and completed task")

    events = get_json(f"{base_url}/agentrelay/tasks/{task_id}/events")["events"]
    event_types = [event["event_type"] for event in events]
    for expected in ["task.created", "task.claimed", "thread.created", "artifact.submitted", "task.completed"]:
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


def assert_ok(label: str, payload: dict) -> None:
    if not payload.get("ok"):
        raise AssertionError(f"{label} failed: {payload}")


if __name__ == "__main__":
    main()

