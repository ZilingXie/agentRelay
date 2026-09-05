from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.protocol_v06 import COORDINATOR_GRANT_OPERATIONS, PROTOCOL_V06
from server.store_v06 import CoordinatorGrantPermissionError, V06Store


COORDINATOR = "project-hermes"
TARGET = "zac-agent"
OTHER = "vivi-agent"
AUTH = {
    COORDINATOR: {
        "Authorization": "Bearer hermes-test-token",
        "X-AgentRelay-Agent-Id": COORDINATOR,
    },
    TARGET: {
        "Authorization": "Bearer zac-test-token",
        "X-AgentRelay-Agent-Id": TARGET,
    },
    OTHER: {
        "Authorization": "Bearer vivi-test-token",
        "X-AgentRelay-Agent-Id": OTHER,
    },
}


def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        verify_reissue_after_task_deadline(root / "reissue.sqlite3")
        run_production_style(root / "production")
        run_compatibility_style(root / "compatibility")
    print("coordinator grant smoke passed (production compatibility=false, test compatibility=true)")


def verify_reissue_after_task_deadline(db_path: Path) -> None:
    seed_agents(db_path)
    store = V06Store(str(db_path))
    claims = grant_claims(1_800_000_000, "deadline-recovery", task_count=1)
    first = store.issue_coordinator_grant(claims, now=1_800_000_001)
    rotated = store.issue_coordinator_grant(
        claims, now=int(claims["task_expires_at"]) + 1
    )
    assert rotated["grant"]["grant_id"] == first["grant"]["grant_id"]
    assert rotated["grant"]["token_version"] == 2
    stale_new = grant_claims(1_800_000_000, "stale-new", task_count=1)
    try:
        store.issue_coordinator_grant(stale_new, now=int(stale_new["task_expires_at"]) + 1)
    except ValueError as exc:
        assert str(exc) == "task_expires_at must be in the future"
    else:
        raise AssertionError("new Coordinator Grant accepted an expired task deadline")


def run_production_style(root: Path) -> None:
    root.mkdir()
    db_path = root / "v06.sqlite3"
    seed_agents(db_path)
    port = free_port()
    process = start_server(root, db_path, port, compatibility=False)
    base = f"http://127.0.0.1:{port}/agentrelay/api"
    try:
        wait_health(base)
        now = int(time.time())
        claims = grant_claims(now, "round-issuance", task_count=2)
        issued = request(base, "POST", "/coordinator-grants", claims, AUTH[COORDINATOR], 201)
        grant = issued["grant"]
        token_one = issued["coordinator_grant_token"]
        assert token_one and token_one not in json.dumps(grant, sort_keys=True)
        assert grant["used_task_count"] == 0
        assert set(grant["operations"]) == set(COORDINATOR_GRANT_OPERATIONS)

        changed = {**claims, "task_count": 1}
        error = request(base, "POST", "/coordinator-grants", changed, AUTH[COORDINATOR], 409)
        assert error["code"] == "coordinator_grant_claim_mismatch"
        duplicate_round = {**claims, "issuance_key": "different-key-same-round"}
        error = request(
            base, "POST", "/coordinator-grants", duplicate_round, AUTH[COORDINATOR], 409
        )
        assert error["code"] == "coordinator_grant_claim_mismatch"

        rotated = request(base, "POST", "/coordinator-grants", claims, AUTH[COORDINATOR], 201)
        assert rotated["grant"]["grant_id"] == grant["grant_id"]
        assert rotated["grant"]["token_version"] == 2
        token = rotated["coordinator_grant_token"]
        assert token != token_one

        raw_payload = task_payload(claims, "wi-raw", "create-raw")
        missing = request(base, "POST", "/tasks", raw_payload, AUTH[COORDINATOR], 401)
        assert missing["code"] == "missing_coordinator_grant"

        invalid_headers = grant_headers(COORDINATOR, "not-a-grant")
        invalid = request(base, "POST", "/tasks", raw_payload, invalid_headers, 403)
        assert invalid["code"] == "invalid_coordinator_grant"

        old_headers = grant_headers(COORDINATOR, token_one)
        old = request(base, "POST", "/tasks", raw_payload, old_headers, 403)
        assert old["code"] == "invalid_coordinator_grant"

        non_coordinator = request(
            base,
            "POST",
            "/tasks",
            {**raw_payload, "requester_agent_id": OTHER},
            grant_headers(OTHER, token),
            403,
        )
        assert non_coordinator["code"] == "coordinator_identity_not_allowed"

        mismatch = task_payload(claims, "wi-mismatch", "create-mismatch")
        mismatch["target_agent_id"] = OTHER
        error = request(base, "POST", "/tasks", mismatch, grant_headers(COORDINATOR, token), 409)
        assert error["code"] == "coordinator_grant_claim_mismatch"

        first_payload = task_payload(claims, "wi-one", "create-one")
        first = request(
            base, "POST", "/tasks", first_payload, grant_headers(COORDINATOR, token), 201
        )
        replay = request(
            base, "POST", "/tasks", first_payload, grant_headers(COORDINATOR, token), 201
        )
        assert replay["task"]["task_id"] == first["task"]["task_id"]

        missing_read = request(
            base, "GET", f"/tasks/{first['task']['task_id']}", None, AUTH[COORDINATOR], 401
        )
        assert missing_read["code"] == "missing_coordinator_grant"
        missing_lineage = request(
            base,
            "GET",
            f"/tasks/{first['task']['task_id']}/lineage",
            None,
            AUTH[COORDINATOR],
            401,
        )
        assert missing_lineage["code"] == "missing_coordinator_grant"
        readback = request(
            base,
            "GET",
            f"/tasks/{first['task']['task_id']}",
            None,
            grant_headers(COORDINATOR, token),
            200,
        )
        assert readback["task"]["task_id"] == first["task"]["task_id"]
        lineage = request(
            base,
            "GET",
            f"/tasks/{first['task']['task_id']}/lineage",
            None,
            grant_headers(COORDINATOR, token),
            200,
        )
        assert [item["task_id"] for item in lineage["tasks"]] == [
            first["task"]["task_id"]
        ]
        try:
            V06Store(str(db_path)).resolve_coordinator_grant_task(
                grant["grant_id"],
                token,
                OTHER,
                idempotency_key="create-one",
            )
        except CoordinatorGrantPermissionError as exc:
            assert exc.code == "coordinator_grant_identity_mismatch"
        else:
            raise AssertionError("wrong coordinator identity unexpectedly used a grant")

        other_claims = {
            **grant_claims(now, "other-round", task_count=1),
            "investigation_id": "inv-other-case",
            "round_id": "round-other",
            "approved_plan_digest": "sha256:" + "b" * 64,
        }
        other_grant = request(
            base, "POST", "/coordinator-grants", other_claims, AUTH[COORDINATOR], 201
        )
        not_owned = request(
            base,
            "GET",
            f"/tasks/{first['task']['task_id']}",
            None,
            grant_headers(COORDINATOR, other_grant["coordinator_grant_token"]),
            409,
        )
        assert not_owned["code"] == "coordinator_grant_task_not_owned"

        missing_resolve = request(
            base,
            "POST",
            f"/coordinator-grants/{grant['grant_id']}/tasks/resolve",
            {"idempotency_key": "create-one", "work_item_id": "wi-one"},
            AUTH[COORDINATOR],
            401,
        )
        assert missing_resolve["code"] == "missing_coordinator_grant"
        resolved = request(
            base,
            "POST",
            f"/coordinator-grants/{grant['grant_id']}/tasks/resolve",
            {"idempotency_key": "create-one", "work_item_id": "wi-one"},
            grant_headers(COORDINATOR, token),
            200,
        )
        assert resolved["mapping"]["task_id"] == first["task"]["task_id"]

        batch_payload = {"task_ids": [first["task"]["task_id"]]}
        batch_missing = request(
            base, "POST", "/task-visibility/batch", batch_payload, AUTH[COORDINATOR], 401
        )
        assert batch_missing["code"] == "missing_coordinator_grant"
        batch = request(
            base,
            "POST",
            "/task-visibility/batch",
            batch_payload,
            grant_headers(COORDINATOR, token),
            200,
        )
        assert batch["items"][0]["task"]["task_id"] == first["task"]["task_id"]

        forbidden = request(
            base,
            "POST",
            f"/tasks/{first['task']['task_id']}/followups",
            first_payload,
            grant_headers(COORDINATOR, token),
            403,
        )
        assert forbidden["code"] == "coordinator_grant_operation_forbidden"

        second_payload = task_payload(claims, "wi-two", "create-two")
        second = request(
            base, "POST", "/tasks", second_payload, grant_headers(COORDINATOR, token), 201
        )
        assert second["task"]["task_id"] != first["task"]["task_id"]
        quota = request(
            base,
            "POST",
            "/tasks",
            task_payload(claims, "wi-three", "create-three"),
            grant_headers(COORDINATOR, token),
            409,
        )
        assert quota["code"] == "coordinator_grant_quota_exhausted"

        prepare_target_response(db_path, first["task"]["task_id"])
        current = request(
            base,
            "GET",
            f"/tasks/{first['task']['task_id']}",
            None,
            grant_headers(COORDINATOR, token),
            200,
        )["task"]
        complete_payload = {
            "actor_agent_id": COORDINATOR,
            "message_id": current["current_message_id"],
            "turn_sequence": current["turn_sequence"],
            "expected_task_version": current["task_version"],
            "idempotency_key": "complete-one",
            "completed_against_message_id": current["current_message_id"],
        }
        no_complete_grant = request(
            base,
            "POST",
            f"/tasks/{current['task_id']}/complete",
            complete_payload,
            AUTH[COORDINATOR],
            401,
        )
        assert no_complete_grant["code"] == "missing_coordinator_grant"
        completed = request(
            base,
            "POST",
            f"/tasks/{current['task_id']}/complete",
            complete_payload,
            grant_headers(COORDINATOR, token),
            200,
        )
        assert completed["task"]["status"] == "completed"
        duplicate_completion = request(
            base,
            "POST",
            f"/tasks/{current['task_id']}/complete",
            complete_payload,
            grant_headers(COORDINATOR, token),
            200,
        )
        assert duplicate_completion["task"]["task_id"] == current["task_id"]
        assert duplicate_completion["task"]["status"] == "completed"

        expired_claims = grant_claims(now, "expired-grant", task_count=1)
        expired_grant = request(
            base, "POST", "/coordinator-grants", expired_claims, AUTH[COORDINATOR], 201
        )
        revoked_claims = grant_claims(now, "revoked-grant", task_count=1)
        revoked_grant = request(
            base, "POST", "/coordinator-grants", revoked_claims, AUTH[COORDINATOR], 201
        )
        with V06Store(str(db_path)).connect() as conn:
            conn.execute(
                "UPDATE coordinator_grants SET grant_expires_at = ? WHERE grant_id = ?",
                (now - 1, expired_grant["grant"]["grant_id"]),
            )
            conn.execute(
                "UPDATE coordinator_grants SET status = 'revoked' WHERE grant_id = ?",
                (revoked_grant["grant"]["grant_id"],),
            )
        expired_error = request(
            base,
            "POST",
            f"/coordinator-grants/{expired_grant['grant']['grant_id']}/tasks/resolve",
            {"idempotency_key": "none"},
            grant_headers(COORDINATOR, expired_grant["coordinator_grant_token"]),
            403,
        )
        assert expired_error["code"] == "coordinator_grant_expired"
        revoked_error = request(
            base,
            "POST",
            f"/coordinator-grants/{revoked_grant['grant']['grant_id']}/tasks/resolve",
            {"idempotency_key": "none"},
            grant_headers(COORDINATOR, revoked_grant["coordinator_grant_token"]),
            403,
        )
        assert revoked_error["code"] == "coordinator_grant_revoked"

        invalid_window = grant_claims(now, "invalid-window", task_count=1)
        invalid_window["grant_expires_at"] = invalid_window["task_expires_at"]
        window_error = request(
            base,
            "POST",
            "/coordinator-grants",
            invalid_window,
            AUTH[COORDINATOR],
            400,
        )
        assert window_error["code"] == "VALIDATION_ERROR"

        assert_token_not_persisted(
            db_path,
            token_one,
            token,
            other_grant["coordinator_grant_token"],
            expired_grant["coordinator_grant_token"],
            revoked_grant["coordinator_grant_token"],
        )
    finally:
        stop_server(process)


def run_compatibility_style(root: Path) -> None:
    root.mkdir()
    db_path = root / "v06.sqlite3"
    seed_agents(db_path)
    port = free_port()
    process = start_server(root, db_path, port, compatibility=True)
    base = f"http://127.0.0.1:{port}/agentrelay/api"
    try:
        wait_health(base)
        now = int(time.time())
        payload = task_payload(grant_claims(now, "compat", task_count=1), "wi-compat", "compat")
        created = request(base, "POST", "/tasks", payload, AUTH[COORDINATOR], 201)
        with V06Store(str(db_path)).connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM task_audit_events
                WHERE task_id = ? AND event_type = 'coordinator.compatibility_create'
                """,
                (created["task"]["task_id"],),
            ).fetchone()
        assert row and json.loads(row["payload_json"])["compatibility_mode"] is True
    finally:
        stop_server(process)


def seed_agents(db_path: Path) -> None:
    store = V06Store(str(db_path))
    for agent_id in (COORDINATOR, TARGET, OTHER):
        store.upsert_agent(
            agent_id,
            name=agent_id,
            owner="isolated-test",
            enabled=True,
            protocol_capabilities=[PROTOCOL_V06],
        )


def grant_claims(now: int, issuance_key: str, *, task_count: int) -> dict:
    return {
        "protocol_version": PROTOCOL_V06,
        "issuance_key": issuance_key,
        "coordinator_agent_id": COORDINATOR,
        "investigation_id": "inv-isolated",
        "round_id": f"round-{issuance_key}",
        "approved_plan_digest": "sha256:" + "a" * 64,
        "authority_ref": "authority-isolated",
        "target_agent_ids": [TARGET],
        "task_count": task_count,
        "task_expires_at": now + 600,
        "grant_expires_at": now + 900,
        "operations": sorted(COORDINATOR_GRANT_OPERATIONS),
    }


def task_payload(claims: dict, work_item_id: str, key: str) -> dict:
    return {
        "protocol_version": PROTOCOL_V06,
        "idempotency_key": key,
        "requester_agent_id": COORDINATOR,
        "target_agent_id": TARGET,
        "done_criteria": {"required": "structured investigation result"},
        "max_turns": 1,
        "task_expires_at": claims["task_expires_at"],
        "message": {
            "subject": work_item_id,
            "metadata": {
                "investigation_id": claims["investigation_id"],
                "round_id": claims["round_id"],
                "work_item_id": work_item_id,
                "approved_plan_digest": claims["approved_plan_digest"],
            },
            "parts": [{"kind": "text", "text": work_item_id}],
        },
    }


def prepare_target_response(db_path: Path, task_id: str) -> None:
    store = V06Store(str(db_path))
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        conn.execute(
            "UPDATE messages SET delivery_status = 'delivered', delivered_at = ?, updated_at = ? WHERE message_id = ?",
            (int(time.time()), int(time.time()), task["current_message_id"]),
        )
        conn.execute(
            "UPDATE agent_events SET outbox_status = 'acked', acked_at = ?, updated_at = ? WHERE task_id = ? AND message_id = ?",
            (int(time.time()), int(time.time()), task_id, task["current_message_id"]),
        )
        conn.execute(
            "UPDATE tasks SET task_version = 2, updated_at = ? WHERE task_id = ?",
            (int(time.time()), task_id),
        )
    response = store.submit_message(
        task_id,
        {
            "actor_agent_id": TARGET,
            "message_id": task["current_message_id"],
            "turn_sequence": 1,
            "expected_task_version": 2,
            "idempotency_key": "target-result",
            "parts": [{"kind": "result", "data": {"outcome": "answered"}}],
        },
    )
    current = response["task"]
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE messages SET delivery_status = 'delivered', delivered_at = ?, updated_at = ? WHERE message_id = ?",
            (int(time.time()), int(time.time()), current["current_message_id"]),
        )
        conn.execute(
            "UPDATE agent_events SET outbox_status = 'acked', acked_at = ?, updated_at = ? WHERE task_id = ? AND message_id = ?",
            (int(time.time()), int(time.time()), task_id, current["current_message_id"]),
        )
        conn.execute(
            "UPDATE tasks SET task_version = task_version + 1, updated_at = ? WHERE task_id = ?",
            (int(time.time()), task_id),
        )


def assert_token_not_persisted(db_path: Path, *tokens: str) -> None:
    with V06Store(str(db_path)).connect() as conn:
        rows = conn.execute(
            "SELECT token_hash, claims_digest FROM coordinator_grants"
        ).fetchall()
    serialized = json.dumps([dict(row) for row in rows], sort_keys=True)
    for token in tokens:
        assert token not in serialized
    assert all(len(row["token_hash"]) == 64 for row in rows)


def grant_headers(agent_id: str, token: str) -> dict[str, str]:
    return {**AUTH[agent_id], "X-AgentRelay-Coordinator-Grant": token}


def start_server(root: Path, db_path: Path, port: int, *, compatibility: bool) -> subprocess.Popen:
    env = {
        **os.environ,
        "AGENTRELAY_HOST": "127.0.0.1",
        "AGENTRELAY_PORT": str(port),
        "AGENTRELAY_DB_PATH": str(root / "legacy.sqlite3"),
        "AGENTRELAY_V06_DB_PATH": str(db_path),
        "AGENTRELAY_MUTATION_MODE": "v06",
        "AGENTRELAY_COORDINATOR_AGENT_IDS": COORDINATOR,
        "AGENTRELAY_COORDINATOR_DIRECT_CREATE_COMPATIBILITY": "1" if compatibility else "0",
        "AGENTRELAY_TOKENS": (
            "hermes:project-hermes:hermes-test-token,"
            "zac:zac-agent:zac-test-token,vivi:vivi-agent:vivi-test-token"
        ),
    }
    return subprocess.Popen(
        ["python3", "-m", "server.app"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_health(base: str) -> None:
    for _ in range(60):
        try:
            request(base, "GET", "/health", None, {}, 200)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("isolated v0.6 server did not become healthy")


def stop_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(
    base: str,
    method: str,
    path: str,
    payload: dict | None,
    headers: dict[str, str],
    expected_status: int,
) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            status = response.status
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        result = json.loads(exc.read())
    if status != expected_status:
        raise AssertionError(
            f"{method} {path}: expected {expected_status}, got {status}: {result}"
        )
    return result


if __name__ == "__main__":
    main()
