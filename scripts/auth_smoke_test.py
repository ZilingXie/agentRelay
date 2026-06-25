from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


BASE_URL = "http://127.0.0.1:8790"
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
                "AGENTRELAY_PORT": "8790",
                "AGENTRELAY_DB_PATH": f"{tmpdir}/agentrelay-auth.sqlite3",
                "AGENTRELAY_TOKENS": "zac:zac-agent:zac-token,frank:frank-agent:frank-token",
            }
        )
        proc = subprocess.Popen(
            ["python3", "-m", "server.app"],
            cwd=os.getcwd(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            wait_for_health()
            assert_ok("health without auth", get_json(f"{BASE_URL}/agentrelay/api/health"))
            expect_http_error("agents without auth", 401, "GET", f"{BASE_URL}/agentrelay/api/agents")

            task = post_json(
                f"{BASE_URL}/agentrelay/api/tasks",
                {
                    "from": "zac-agent",
                    "to": "frank-agent",
                    "requesterThreadId": "zac-thread-auth-smoke",
                    "subject": "Auth smoke",
                    "message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]},
                },
                ZAC_HEADERS,
            )["task"]
            task_id = task["task_id"]
            expect_http_error(
                "zac token cannot claim frank queue",
                403,
                "GET",
                f"{BASE_URL}/agentrelay/api/workers/frank-agent/claim",
                headers=ZAC_HEADERS,
            )
            claimed = get_json(f"{BASE_URL}/agentrelay/api/workers/frank-agent/claim", FRANK_HEADERS)["task"]
            if claimed["task_id"] != task_id:
                raise AssertionError("frank token did not claim task")
            print(json.dumps({"ok": True, "taskId": task_id}, indent=2))
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
            assert_ok("health", get_json(f"{BASE_URL}/agentrelay/health"))
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    return request_json("GET", url, headers=headers)


def post_json(url: str, payload: dict, headers: dict[str, str] | None = None) -> dict:
    return request_json("POST", url, payload=payload, headers=headers)


def request_json(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
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
    headers: dict[str, str] | None = None,
) -> None:
    try:
        request_json(method, url, payload, headers)
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
