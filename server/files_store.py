from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable
import uuid


DEFAULT_FILES_DB_PATH = "./data/agentrelay-files.sqlite3"
DEFAULT_BLOBS_DIR = "./data/blobs"
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_FILES_PER_MESSAGE = 8
DEFAULT_MAX_TOTAL_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_FILE_RETENTION_HOURS = 72
DEFAULT_FILE_ORPHAN_HOURS = 24
DEFAULT_FILE_GC_INTERVAL_SECONDS = 60 * 60

# Server-side blob paths are derived exclusively from server-generated ids;
# client-provided display names never take part in path construction.
TASK_ID_PATTERN = "^[A-Za-z0-9_.-]{1,128}$"
FILE_NAME_MAX_LENGTH = 255
FILE_MIME_MAX_LENGTH = 255

TaskStatusLookup = Callable[[set[str]], dict[str, dict[str, Any]]]


def max_file_bytes_from_env(default: int = DEFAULT_MAX_FILE_BYTES) -> int:
    raw = os.environ.get("AGENTRELAY_MAX_FILE_BYTES", "").strip()
    if not raw:
        return default
    value = int(raw)
    if value < 1:
        raise ValueError("AGENTRELAY_MAX_FILE_BYTES must be a positive integer")
    return value


def file_retention_hours_from_env(default: int = DEFAULT_FILE_RETENTION_HOURS) -> int:
    raw = os.environ.get("AGENTRELAY_FILE_RETENTION_HOURS", "").strip()
    if not raw:
        return default
    value = int(raw)
    if value < 1:
        raise ValueError("AGENTRELAY_FILE_RETENTION_HOURS must be a positive integer")
    return value


def file_orphan_hours_from_env(default: int = DEFAULT_FILE_ORPHAN_HOURS) -> int:
    raw = os.environ.get("AGENTRELAY_FILE_ORPHAN_HOURS", "").strip()
    if not raw:
        return default
    value = int(raw)
    if value < 1:
        raise ValueError("AGENTRELAY_FILE_ORPHAN_HOURS must be a positive integer")
    return value


def max_files_per_message_from_env(default: int = DEFAULT_MAX_FILES_PER_MESSAGE) -> int:
    return positive_int_from_env("AGENTRELAY_MAX_FILES_PER_MESSAGE", default)


def max_total_file_bytes_from_env(default: int = DEFAULT_MAX_TOTAL_FILE_BYTES) -> int:
    return positive_int_from_env("AGENTRELAY_MAX_TOTAL_FILE_BYTES", default)


def file_gc_interval_seconds_from_env(default: int = DEFAULT_FILE_GC_INTERVAL_SECONDS) -> int:
    return positive_int_from_env("AGENTRELAY_FILE_GC_INTERVAL_SECONDS", default)


def positive_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def sanitize_file_name(name: str) -> str:
    cleaned = "".join(char for char in name if ord(char) >= 32 and char != "\x7f")
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned[:FILE_NAME_MAX_LENGTH]


def sanitize_file_mime(mime_type: str) -> str:
    cleaned = " ".join(mime_type.split()).strip().lower()
    return cleaned[:FILE_MIME_MAX_LENGTH] or "application/octet-stream"


class FilesStore:
    """Task-scoped file metadata and blob storage for Protocol v0.6 attachments.

    Blob bytes live under ``blobs_dir/<task_id>/<file_id>``; metadata lives in a
    dedicated SQLite database so the protocol lane stores stay untouched. Rows are
    removed by ``run_maintenance`` once their Task has been terminal past the
    retention window, or when an upload was never referenced by any Message past
    the orphan window.
    """

    def __init__(
        self,
        db_path: str,
        *,
        blobs_dir: str,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_files_per_message: int = DEFAULT_MAX_FILES_PER_MESSAGE,
        max_total_file_bytes: int = DEFAULT_MAX_TOTAL_FILE_BYTES,
        retention_hours: int = DEFAULT_FILE_RETENTION_HOURS,
        orphan_hours: int = DEFAULT_FILE_ORPHAN_HOURS,
        task_status_lookup: TaskStatusLookup | None = None,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.blobs_dir = Path(blobs_dir)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = max_file_bytes
        self.max_files_per_message = max_files_per_message
        self.max_total_file_bytes = max_total_file_bytes
        self.retention_hours = retention_hours
        self.orphan_hours = orphan_hours
        self.task_status_lookup = task_status_lookup
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'files'"
            ).fetchone()
            legacy_unique = existing and re.search(
                r"UNIQUE\s*\(\s*task_id\s*,\s*sha256\s*\)",
                str(existing["sql"]),
                re.IGNORECASE,
            )
            if legacy_unique:
                conn.execute("ALTER TABLE files RENAME TO files_legacy_v1")
                conn.execute(
                    """CREATE TABLE files (
                        file_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        uploader_agent_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 1),
                        sha256 TEXT NOT NULL,
                        content_path TEXT NOT NULL,
                        referenced_at INTEGER,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        UNIQUE (task_id, uploader_agent_id, sha256, name, mime_type)
                    )"""
                )
                conn.execute(
                    """INSERT INTO files (
                        file_id, task_id, uploader_agent_id, name, mime_type,
                        size_bytes, sha256, content_path, referenced_at,
                        created_at, updated_at
                    )
                    SELECT file_id, task_id, uploader_agent_id, name, mime_type,
                           size_bytes, sha256, content_path, referenced_at,
                           created_at, updated_at
                    FROM files_legacy_v1
                    WHERE size_bytes >= 1
                    """
                )
                conn.execute("DROP TABLE files_legacy_v1")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS files (
                    file_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    uploader_agent_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 1),
                    sha256 TEXT NOT NULL,
                    content_path TEXT NOT NULL,
                    referenced_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE (task_id, uploader_agent_id, sha256, name, mime_type)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_files_task ON files (task_id, created_at, file_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_files_unreferenced ON files (referenced_at, created_at, file_id)"
            )

    def task_blobs_dir(self, task_id: str) -> Path:
        return self.blobs_dir / task_id

    def blob_path(self, task_id: str, file_id: str) -> Path:
        return self.task_blobs_dir(task_id) / file_id

    def new_temp_path(self) -> Path:
        temp_dir = self.blobs_dir / ".incoming"
        temp_dir.mkdir(parents=True, exist_ok=True)
        return temp_dir / f".tmp-{uuid.uuid4().hex}"

    def create_file(
        self,
        *,
        task_id: str,
        uploader_agent_id: str,
        name: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        temp_path: Path,
        now: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Persist an uploaded blob. Re-uploading identical content for the same
        Task returns the existing record (deduplicated) and drops the new bytes."""
        timestamp = int(time.time()) if now is None else int(now)
        existing = self.find_duplicate(
            task_id, uploader_agent_id, sha256, name, mime_type
        )
        if existing is not None:
            temp_path.unlink(missing_ok=True)
            return existing, True
        file_id = f"file_{uuid.uuid4().hex}"
        final_dir = self.task_blobs_dir(task_id)
        final_dir.mkdir(parents=True, exist_ok=True)
        final_path = final_dir / file_id
        os.replace(temp_path, final_path)
        try:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO files (
                        file_id, task_id, uploader_agent_id, name, mime_type,
                        size_bytes, sha256, content_path, referenced_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        file_id,
                        task_id,
                        uploader_agent_id,
                        name,
                        mime_type,
                        size_bytes,
                        sha256,
                        f"{task_id}/{file_id}",
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError:
            existing = self.find_duplicate(
                task_id, uploader_agent_id, sha256, name, mime_type
            )
            final_path.unlink(missing_ok=True)
            if existing is None:
                raise
            return existing, True
        return self.get_file(task_id, file_id) or {
            "file_id": file_id,
            "task_id": task_id,
            "uploader_agent_id": uploader_agent_id,
            "name": name,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "referenced_at": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }, False

    def find_duplicate(
        self,
        task_id: str,
        uploader_agent_id: str,
        sha256: str,
        name: str,
        mime_type: str,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM files
                WHERE task_id = ? AND uploader_agent_id = ? AND sha256 = ?
                  AND name = ? AND mime_type = ?
                """,
                (task_id, uploader_agent_id, sha256, name, mime_type),
            ).fetchone()
        return dict(row) if row else None

    def get_file(self, task_id: str, file_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE task_id = ? AND file_id = ?",
                (task_id, file_id),
            ).fetchone()
        return dict(row) if row else None

    def list_task_files(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM files WHERE task_id = ? ORDER BY created_at, file_id",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_referenced(self, file_ids: list[str], *, now: int | None = None) -> int:
        if not file_ids:
            return 0
        timestamp = int(time.time()) if now is None else int(now)
        placeholders = ", ".join("?" for _ in file_ids)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE files
                SET referenced_at = COALESCE(referenced_at, ?), updated_at = ?
                WHERE file_id IN ({placeholders})
                """,
                [timestamp, timestamp, *file_ids],
            )
            return cursor.rowcount

    def delete_rows(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        for row in rows:
            content_path = self.blobs_dir / str(row["content_path"])
            content_path.unlink(missing_ok=True)
        file_ids = [str(row["file_id"]) for row in rows]
        placeholders = ", ".join("?" for _ in file_ids)
        with self.connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM files WHERE file_id IN ({placeholders})",
                file_ids,
            )
            return cursor.rowcount

    def run_maintenance(self, *, now: int | None = None) -> dict[str, int]:
        """Delete orphaned uploads and files whose Task stayed terminal past the
        retention window. Called from the delivery coordinator tick."""
        timestamp = int(time.time()) if now is None else int(now)
        deleted_orphans = self._sweep_orphans(timestamp)
        deleted_expired = self._sweep_terminal_tasks(timestamp)
        deleted_untracked = self._sweep_untracked_blobs(timestamp)
        return {
            "deleted_orphan_files": deleted_orphans,
            "deleted_task_files": deleted_expired,
            "deleted_untracked_blobs": deleted_untracked,
        }

    def _sweep_orphans(self, now: int) -> int:
        deadline = now - self.orphan_hours * 3600
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM files WHERE referenced_at IS NULL AND created_at <= ?",
                (deadline,),
            ).fetchall()
        return self.delete_rows([dict(row) for row in rows])

    def _sweep_terminal_tasks(self, now: int) -> int:
        if self.task_status_lookup is None:
            return 0
        deadline = now - self.retention_hours * 3600
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM files WHERE referenced_at IS NOT NULL AND referenced_at <= ?",
                (deadline,),
            ).fetchall()
        if not rows:
            return 0
        by_task: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_task.setdefault(str(row["task_id"]), []).append(dict(row))
        try:
            statuses = self.task_status_lookup(set(by_task))
        except Exception:
            return 0
        doomed: list[dict[str, Any]] = []
        for task_id, task_rows in by_task.items():
            status = statuses.get(task_id)
            if not status or status.get("status") == "open":
                continue
            terminal_at = int(status.get("terminal_at") or 0)
            if terminal_at and terminal_at <= deadline:
                doomed.extend(task_rows)
        return self.delete_rows(doomed)

    def _sweep_untracked_blobs(self, now: int) -> int:
        deadline = now - self.orphan_hours * 3600
        with self.connect() as conn:
            tracked = {
                str(row["content_path"])
                for row in conn.execute("SELECT content_path FROM files").fetchall()
            }
        deleted = 0
        for task_dir in self.blobs_dir.iterdir():
            if not task_dir.is_dir() or task_dir.name == ".incoming":
                continue
            for path in task_dir.iterdir():
                if not path.is_file():
                    continue
                relative = f"{task_dir.name}/{path.name}"
                if relative in tracked or int(path.stat().st_mtime) > deadline:
                    continue
                path.unlink(missing_ok=True)
                deleted += 1
            try:
                task_dir.rmdir()
            except OSError:
                pass
        incoming = self.blobs_dir / ".incoming"
        if incoming.is_dir():
            for path in incoming.iterdir():
                if path.is_file() and int(path.stat().st_mtime) <= deadline:
                    path.unlink(missing_ok=True)
                    deleted += 1
        return deleted

    def admin_summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM files").fetchone()
            referenced = conn.execute(
                "SELECT COUNT(*) FROM files WHERE referenced_at IS NOT NULL"
            ).fetchone()[0]
        return {
            "files": int(total[0]),
            "file_bytes": int(total[1]),
            "referenced_files": int(referenced),
            "unreferenced_files": int(total[0]) - int(referenced),
            "max_file_bytes": self.max_file_bytes,
            "max_files_per_message": self.max_files_per_message,
            "max_total_file_bytes": self.max_total_file_bytes,
            "retention_hours": self.retention_hours,
            "orphan_hours": self.orphan_hours,
        }
