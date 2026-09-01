from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.app import make_v06_task_status_lookup
from server.files_store import FilesStore
from server.protocol_v06 import PROTOCOL_V06
from server.store_v06 import V06Store


REQUESTER = "zac-agent"
TARGET = "vivi-agent"
OUTSIDER = "mallory-agent"
PORT = 8809
BASE = f"http://127.0.0.1:{PORT}/agentrelay/api"
MAX_FILE_BYTES = 4096
HEADERS = {
    REQUESTER: {"Authorization": "Bearer requester-token"},
    TARGET: {"Authorization": "Bearer target-token"},
    OUTSIDER: {"Authorization": "Bearer outsider-token"},
}
CONTENT = b"agentrelay file attachment smoke payload\n" * 8
CONTENT_SHA = hashlib.sha256(CONTENT).hexdigest()
NOW = int(time.time())


def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        v06_db = root / "v06.sqlite3"
        files_db = root / "files.sqlite3"
        blobs_dir = root / "blobs"
        seed_agents(v06_db)
        process = start_server(root / "legacy.sqlite3", v06_db, files_db, blobs_dir)
        try:
            wait_health()
            run_flow(v06_db, files_db, blobs_dir)
        finally:
            stop_server(process)
    print("protocol v0.6 file attachment smoke test passed")


def seed_agents(db_path: Path) -> None:
    store = V06Store(str(db_path))
    for agent_id in (REQUESTER, TARGET, OUTSIDER):
        store.upsert_agent(
            agent_id,
            name=agent_id,
            owner=agent_id,
            enabled=True,
            protocol_capabilities=[PROTOCOL_V06],
        )


def run_flow(v06_db: Path, files_db: Path, blobs_dir: Path) -> None:
    listeners = {
        REQUESTER: register_and_ready(REQUESTER, "files-listener-zac"),
        TARGET: register_and_ready(TARGET, "files-listener-vivi"),
    }
    created = request(
        "POST",
        "/tasks",
        {
            "protocol_version": PROTOCOL_V06,
            "idempotency_key": "files-smoke-create",
            "requester_agent_id": REQUESTER,
            "target_agent_id": TARGET,
            "done_criteria": "target receives the attached file",
            "message": {"subject": "files", "parts": [{"kind": "text", "text": "see attachment"}]},
        },
        HEADERS[REQUESTER],
        201,
    )
    task_id = created["task"]["task_id"]

    # Initial messages cannot carry file parts (uploads are Task-scoped).
    error = request(
        "POST",
        "/tasks",
        {
            "protocol_version": PROTOCOL_V06,
            "idempotency_key": "files-smoke-create-with-file",
            "requester_agent_id": REQUESTER,
            "target_agent_id": TARGET,
            "done_criteria": "target receives the attached file",
            "message": {
                "subject": "files",
                "parts": [
                    {
                        "kind": "file",
                        "file_id": "file_0123456789abcdef0123456789abcdef",
                        "name": "x.bin",
                        "size_bytes": 4,
                        "sha256": CONTENT_SHA,
                    }
                ],
            },
        },
        HEADERS[REQUESTER],
        400,
    )
    assert error["code"] == "VALIDATION_ERROR"

    # The target ACKs the initial message, then takes its turn.
    deliver_current(task_id, listeners)
    reply(task_id, TARGET, "target-turn", [{"kind": "text", "text": "ready for the file"}])

    uploaded = upload(task_id, REQUESTER, CONTENT, "logs/report.txt", "text/plain", CONTENT_SHA, 201)
    file_id = uploaded["file"]["file_id"]
    assert uploaded["file"]["size_bytes"] == len(CONTENT)
    assert uploaded["file"]["sha256"] == CONTENT_SHA
    assert uploaded["file"]["name"] == "logs/report.txt"
    assert (blobs_dir / task_id / file_id).is_file()

    duplicate = upload(task_id, REQUESTER, CONTENT, "logs/report.txt", "text/plain", CONTENT_SHA, 201)
    assert duplicate["file"]["file_id"] == file_id
    assert duplicate["deduplicated"] is True

    bad_sha = hashlib.sha256(b"other").hexdigest()
    mismatch = upload(task_id, REQUESTER, CONTENT, "x.txt", "text/plain", bad_sha, 422)
    assert mismatch["code"] == "FILE_SHA256_MISMATCH"

    denied = upload(task_id, OUTSIDER, CONTENT, "x.txt", "text/plain", CONTENT_SHA, 403)
    assert denied["code"] == "TASK_PARTICIPANT_REQUIRED"
    assert upload("task_unknown", REQUESTER, CONTENT, "x.txt", "text/plain", CONTENT_SHA, 404)
    assert upload(task_id, REQUESTER, b"", "empty.txt", "text/plain", None, 422)
    oversized = upload(task_id, REQUESTER, b"x" * (MAX_FILE_BYTES + 1), "big.bin", None, None, 413)
    assert oversized["code"] == "FILE_TOO_LARGE"

    # The requester ACKs the target's reply, then replies carrying the file part.
    deliver_current(task_id, listeners)
    file_part = {
        "kind": "file",
        "file_id": file_id,
        "name": "report.txt",
        "mime_type": "text/plain",
        "size_bytes": len(CONTENT),
        "sha256": CONTENT_SHA,
    }
    sent = reply(
        task_id,
        REQUESTER,
        "requester-file-turn",
        [{"kind": "text", "text": "here is the report"}, file_part],
    )
    assert any(part.get("kind") == "file" for part in sent["messages"][-1]["parts"])

    listed = request("GET", f"/tasks/{task_id}/files", None, HEADERS[TARGET], 200)
    assert [item["file_id"] for item in listed["files"]] == [file_id]
    assert listed["files"][0]["referenced_at"] is not None

    downloaded = download(task_id, TARGET, file_id, 200)
    assert downloaded["body"] == CONTENT
    assert downloaded["sha256"] == CONTENT_SHA
    forbidden = download(task_id, OUTSIDER, file_id, 403)
    assert forbidden["code"] == "TASK_PARTICIPANT_REQUIRED"
    assert download(task_id, TARGET, "file_ffffffffffffffffffffffffffffffff", 404)

    # A file uploaded by the target cannot be referenced by the requester.
    target_upload = upload(task_id, TARGET, b"target-owned", "t.bin", None, None, 201)
    error = reply(
        task_id,
        REQUESTER,
        "wrong-uploader-turn",
        [
            {"kind": "text", "text": "borrowing"},
            {
                "kind": "file",
                "file_id": target_upload["file"]["file_id"],
                "name": "t.bin",
                "size_bytes": len(b"target-owned"),
                "sha256": target_upload["file"]["sha256"],
            },
        ],
        expected_status=400,
    )
    assert error["code"] == "VALIDATION_ERROR"

    # Unknown file ids are rejected too.
    error = reply(
        task_id,
        REQUESTER,
        "unknown-file-turn",
        [
            {"kind": "text", "text": "ghost file"},
            {
                "kind": "file",
                "file_id": "file_0123456789abcdef0123456789abcdef",
                "name": "ghost.bin",
                "size_bytes": 5,
                "sha256": CONTENT_SHA,
            },
        ],
        expected_status=400,
    )
    assert error["code"] == "VALIDATION_ERROR"

    # Completion requires delivered target evidence: ACK the file message, let
    # the target close the loop, then ACK the target's final reply.
    deliver_current(task_id, listeners)
    reply(task_id, TARGET, "target-final-turn", [{"kind": "text", "text": "report received"}])
    deliver_current(task_id, listeners)

    files_store = FilesStore(
        str(files_db),
        blobs_dir=str(blobs_dir),
        max_file_bytes=MAX_FILE_BYTES,
        retention_hours=72,
        orphan_hours=24,
        task_status_lookup=make_v06_task_status_lookup(V06Store(str(v06_db))),
    )

    # Unreferenced uploads are swept after the orphan window. Both the explicit
    # orphan and the never-referenced target upload (its reference was rejected)
    # are gone; the file attached to the accepted message survives.
    orphan = upload(task_id, TARGET, b"orphan-bytes", "orphan.bin", None, None, 201)
    orphan_path = blobs_dir / task_id / orphan["file"]["file_id"]
    target_upload_path = blobs_dir / task_id / target_upload["file"]["file_id"]
    assert orphan_path.is_file()
    counts = files_store.run_maintenance(now=NOW + 25 * 3600)
    assert counts["deleted_orphan_files"] == 2
    assert not orphan_path.exists()
    assert not target_upload_path.exists()

    # Referenced files survive until their Task stayed terminal past retention.
    detail = task_detail(task_id)
    V06Store(str(v06_db)).complete_task(
        task_id,
        {
            "actor_agent_id": REQUESTER,
            "message_id": detail["task"]["current_message_id"],
            "turn_sequence": detail["task"]["turn_sequence"],
            "expected_task_version": detail["task"]["task_version"],
            "idempotency_key": "files-smoke-complete",
            "completed_against_message_id": detail["task"]["current_message_id"],
        },
        now=NOW + 60,
    )
    counts = files_store.run_maintenance(now=NOW + 26 * 3600)
    assert counts["deleted_task_files"] == 0
    assert download(task_id, TARGET, file_id, 200)["body"] == CONTENT
    counts = files_store.run_maintenance(now=NOW + 73 * 3600)
    assert counts["deleted_task_files"] >= 1
    assert not (blobs_dir / task_id / file_id).exists()
    assert download(task_id, TARGET, file_id, 404)

    summary = request("GET", "/admin/api/summary", None, {"X-AgentRelay-Admin-Token": "admin-token"}, 200)
    assert summary["files"]["max_file_bytes"] == MAX_FILE_BYTES


def task_detail(task_id: str) -> dict:
    return request("GET", f"/tasks/{task_id}", None, HEADERS[REQUESTER], 200)


def register_and_ready(agent_id: str, instance_id: str) -> tuple[str, int]:
    registered = request(
        "POST",
        f"/workers/{agent_id}/readiness/register",
        {
            "listener_instance_id": instance_id,
            "client_version": "0.6.0",
            "workspace_version": "2",
            "transport": "websocket",
        },
        HEADERS[agent_id],
        201,
    )["readiness"]
    epoch = registered["readiness_epoch"]
    request(
        "POST",
        f"/workers/{agent_id}/readiness",
        {
            "listener_instance_id": instance_id,
            "readiness_epoch": epoch,
            "ready": True,
        },
        HEADERS[agent_id],
        200,
    )
    return instance_id, epoch


def deliver_current(task_id: str, listeners: dict[str, tuple[str, int]]) -> None:
    """Recover and ACK the task's current message for its receiving agent so the
    next turn is allowed (v0.6 requires a delivered current Message)."""
    detail = task_detail(task_id)
    task = detail["task"]
    receiver = task["to_agent_id"]
    instance_id, epoch = listeners[receiver]
    query = urllib.parse.urlencode(
        {"listener_instance_id": instance_id, "readiness_epoch": epoch}
    )
    events = request(
        "GET",
        f"/workers/{receiver}/events?{query}",
        None,
        HEADERS[receiver],
        200,
    )["events"]
    assert events, f"no recoverable event for {receiver}"
    event = events[0]
    assert event["task_id"] == task_id
    acked = request(
        "POST",
        f"/workers/{receiver}/messages/{task['current_message_id']}/ack",
        {
            "message_id": task["current_message_id"],
            "turn_sequence": task["turn_sequence"],
            "expected_task_version": task["task_version"],
            "idempotency_key": f"files-ack-{event['event_id']}",
            "task_id": task_id,
            "event_id": event["event_id"],
            "listener_instance_id": instance_id,
            "readiness_epoch": epoch,
        },
        HEADERS[receiver],
        200,
    )
    assert acked["task"]["status"] == "open"


def reply(
    task_id: str,
    actor: str,
    key: str,
    parts: list[dict],
    *,
    expected_status: int = 201,
) -> dict:
    detail = task_detail(task_id)
    task = detail["task"]
    return request(
        "POST",
        f"/tasks/{task_id}/messages",
        {
            "actor_agent_id": actor,
            "message_id": task["current_message_id"],
            "turn_sequence": task["turn_sequence"],
            "expected_task_version": task["task_version"],
            "idempotency_key": key,
            "parts": parts,
        },
        HEADERS[actor],
        expected_status,
    )


def upload(
    task_id: str,
    agent: str,
    body: bytes,
    name: str,
    mime_type: str | None,
    declared_sha: str | None,
    expected_status: int,
) -> dict:
    headers = {**HEADERS[agent], "Content-Type": mime_type or "application/octet-stream"}
    if name:
        headers["X-AgentRelay-File-Name"] = urllib.parse.quote(name)
    if declared_sha:
        headers["X-AgentRelay-File-Sha256"] = declared_sha
    return raw_request(
        "POST",
        f"/tasks/{task_id}/files",
        body if body else b"",
        headers,
        expected_status,
    )


def download(task_id: str, agent: str, file_id: str, expected_status: int) -> dict:
    req = urllib.request.Request(
        f"{BASE}/tasks/{task_id}/files/{file_id}",
        method="GET",
        headers=HEADERS[agent],
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            status = response.status
            result = {
                "body": response.read(),
                "sha256": response.headers.get("X-AgentRelay-File-Sha256", ""),
                "disposition": response.headers.get("Content-Disposition", ""),
            }
    except urllib.error.HTTPError as exc:
        status = exc.code
        result = json.loads(exc.read())
    if status != expected_status:
        raise AssertionError(
            f"GET /tasks/{task_id}/files/{file_id}: expected {expected_status}, got {status}: {result}"
        )
    return result


def request(
    method: str,
    path: str,
    payload: dict | None,
    headers: dict[str, str],
    expected_status: int,
) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    return raw_request(method, path, body or b"", {**headers, "Content-Type": "application/json"}, expected_status)


def raw_request(
    method: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
    expected_status: int,
) -> dict:
    data = body if method != "GET" else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            status = response.status
            result = maybe_json(response.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        result = maybe_json(exc.read())
    if status != expected_status:
        raise AssertionError(
            f"{method} {path}: expected {expected_status}, got {status}: {result}"
        )
    return result


def maybe_json(raw: bytes):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"body": raw}


def start_server(
    legacy_db: Path, v06_db: Path, files_db: Path, blobs_dir: Path
) -> subprocess.Popen:
    env = {
        **os.environ,
        "AGENTRELAY_HOST": "127.0.0.1",
        "AGENTRELAY_PORT": str(PORT),
        "AGENTRELAY_DB_PATH": str(legacy_db),
        "AGENTRELAY_V06_DB_PATH": str(v06_db),
        "AGENTRELAY_FILES_DB_PATH": str(files_db),
        "AGENTRELAY_BLOBS_DIR": str(blobs_dir),
        "AGENTRELAY_MAX_FILE_BYTES": str(MAX_FILE_BYTES),
        "AGENTRELAY_MUTATION_MODE": "v06",
        "AGENTRELAY_ADMIN_TOKEN": "admin-token",
        "AGENTRELAY_TOKENS": (
            "zac:zac-agent:requester-token,vivi:vivi-agent:target-token,"
            "mallory:mallory-agent:outsider-token"
        ),
    }
    return subprocess.Popen(
        ["python3", "-m", "server.app"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_health() -> None:
    for _ in range(60):
        try:
            raw_request("GET", "/health", b"", {}, 200)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("v0.6 files smoke server did not become healthy")


def stop_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


if __name__ == "__main__":
    main()
