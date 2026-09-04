from __future__ import annotations

import json
import math
import re
from typing import Any


PROTOCOL_V06 = "agent-collab-v0.6"
COORDINATOR_GRANT_OPERATIONS = frozenset({"create", "read", "batch", "complete-own"})
COORDINATOR_GRANT_MAX_TASKS = 100
TASK_STATES = {"open", "completed", "expired", "failed"}
MESSAGE_DELIVERY_STATES = {"pending", "delivered", "failed"}
OUTBOX_STATES = {"queued", "inflight", "acked", "retry_wait", "parked", "exhausted"}
TERMINAL_TASK_STATES = {"completed", "expired", "failed"}
RESERVED_TASK_STATES = {"cancelled", "archived"}

MAX_DELIVERY_ATTEMPTS = 4
MAX_AGENT_UNACKED_EVENTS = 1000
RETRY_BACKOFF_SECONDS = (60, 300, 600)
DELIVERY_ACK_LEASE_SECONDS = 60
LISTENER_READINESS_PUBLISH_INTERVAL_SECONDS = 60
LISTENER_READINESS_MAX_AGE_SECONDS = 300
MAX_VISIBILITY_BATCH_SIZE = 100
MESSAGE_SUBJECT_MAX_LENGTH = 120
MESSAGE_METADATA_MAX_BYTES = 4096
MESSAGE_METADATA_MAX_DEPTH = 3
MESSAGE_METADATA_MAX_PROPERTIES = 16
MESSAGE_METADATA_MAX_ARRAY_ITEMS = 16
MESSAGE_METADATA_MAX_STRING_LENGTH = 1024
MESSAGE_METADATA_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
MESSAGE_METADATA_RESERVED_KEYS = {
    "actoragentid",
    "actorid",
    "requesteragentid",
    "requesterid",
    "targetagentid",
    "targetid",
    "agentid",
    "authorization",
    "auth",
    "approval",
    "confirmationref",
    "clientactionid",
    "idempotencykey",
    "messageid",
    "turnsequence",
    "expectedtaskversion",
    "operation",
    "method",
    "path",
    "route",
    "token",
    "credential",
    "credentials",
    "headers",
}

TASK_FAILURE_REASONS = {
    "relay_persistence_failed",
    "agent_reported_failure",
    "max_turns_exhausted",
    "internal_consistency_error",
}
DELIVERY_FAILURE_REASONS = set()
OUTBOX_LAST_ERRORS = {
    "listener_unavailable",
    "socket_write_failed",
    "ack_lease_expired",
}
OUTBOX_EXHAUSTION_REASONS = {
    "task_expired",
    "task_failed",
}


def is_protocol_v06(payload: dict[str, Any] | None) -> bool:
    return bool(payload and payload.get("protocol_version") == PROTOCOL_V06)


def require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required field: {key}")
    return value.strip()


def require_positive_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def reject_unknown(payload: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(unknown)}")


FILE_PART_FIELDS = {"kind", "file_id", "name", "mime_type", "size_bytes", "sha256"}
FILE_ID_PATTERN = re.compile(r"^file_[a-f0-9]{32}$")
FILE_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
FILE_NAME_MAX_LENGTH = 255
FILE_MIME_MAX_LENGTH = 255


def validate_file_part(part: dict[str, Any]) -> None:
    unknown = sorted(set(part) - FILE_PART_FIELDS)
    if unknown:
        raise ValueError(f"file part has unknown fields: {', '.join(unknown)}")
    file_id = part.get("file_id")
    if not isinstance(file_id, str) or not FILE_ID_PATTERN.fullmatch(file_id):
        raise ValueError("file part file_id must match ^file_[a-f0-9]{32}$")
    name = part.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > FILE_NAME_MAX_LENGTH:
        raise ValueError(
            f"file part name must be a non-empty string of at most {FILE_NAME_MAX_LENGTH} characters"
        )
    mime_type = part.get("mime_type")
    if mime_type is not None and (
        not isinstance(mime_type, str) or len(mime_type) > FILE_MIME_MAX_LENGTH
    ):
        raise ValueError(f"file part mime_type must be a string of at most {FILE_MIME_MAX_LENGTH} characters")
    size_bytes = part.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
        raise ValueError("file part size_bytes must be a positive integer")
    sha256 = part.get("sha256")
    if not isinstance(sha256, str) or not FILE_SHA256_PATTERN.fullmatch(sha256):
        raise ValueError("file part sha256 must match ^[a-f0-9]{64}$")


def validate_message_parts(parts: Any) -> list[dict[str, Any]]:
    if not isinstance(parts, list) or not parts:
        raise ValueError("parts must be a non-empty array")
    if not all(isinstance(part, dict) and part for part in parts):
        raise ValueError("parts entries must be non-empty objects")
    for part in parts:
        if part.get("kind") == "file":
            validate_file_part(part)
    return parts


def validate_message_subject(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("message.subject must be a non-empty string")
    subject = value.strip()
    if len(subject) > MESSAGE_SUBJECT_MAX_LENGTH:
        raise ValueError(f"message.subject must be at most {MESSAGE_SUBJECT_MAX_LENGTH} characters")
    return subject


def validate_message_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("message.metadata must be an object")
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MESSAGE_METADATA_MAX_BYTES:
        raise ValueError(f"message.metadata must be at most {MESSAGE_METADATA_MAX_BYTES} bytes")
    _validate_message_metadata_value(value, depth=0)
    return value


def _validate_message_metadata_value(value: Any, *, depth: int) -> None:
    if depth > MESSAGE_METADATA_MAX_DEPTH:
        raise ValueError(f"message.metadata depth must be at most {MESSAGE_METADATA_MAX_DEPTH}")
    if isinstance(value, dict):
        if len(value) > MESSAGE_METADATA_MAX_PROPERTIES:
            raise ValueError(
                f"message.metadata objects may contain at most {MESSAGE_METADATA_MAX_PROPERTIES} properties"
            )
        for key, child in value.items():
            if not isinstance(key, str) or not MESSAGE_METADATA_KEY.fullmatch(key):
                raise ValueError("message.metadata keys must match ^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
            normalized_key = re.sub(r"[_.-]", "", key).lower()
            if normalized_key in MESSAGE_METADATA_RESERVED_KEYS:
                raise ValueError(f"message.metadata key is reserved: {key}")
            _validate_message_metadata_value(child, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MESSAGE_METADATA_MAX_ARRAY_ITEMS:
            raise ValueError(
                f"message.metadata arrays may contain at most {MESSAGE_METADATA_MAX_ARRAY_ITEMS} items"
            )
        for child in value:
            _validate_message_metadata_value(child, depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value) > MESSAGE_METADATA_MAX_STRING_LENGTH:
            raise ValueError(
                f"message.metadata strings may contain at most {MESSAGE_METADATA_MAX_STRING_LENGTH} characters"
            )
        return
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    raise ValueError("message.metadata values must be finite JSON values")


def validate_task_create(payload: dict[str, Any]) -> None:
    reject_unknown(
        payload,
        {
            "protocol_version",
            "idempotency_key",
            "requester_agent_id",
            "target_agent_id",
            "done_criteria",
            "max_turns",
            "task_expires_at",
            "message",
        },
    )
    if payload.get("protocol_version") != PROTOCOL_V06:
        raise ValueError(f"protocol_version must be {PROTOCOL_V06}")
    requester = require_string(payload, "requester_agent_id")
    target = require_string(payload, "target_agent_id")
    if requester == target:
        raise ValueError("requester_agent_id and target_agent_id must differ")
    require_string(payload, "idempotency_key")
    done_criteria = payload.get("done_criteria")
    if not isinstance(done_criteria, (str, dict)) or not done_criteria:
        raise ValueError("done_criteria must be a non-empty string or object")
    message = payload.get("message")
    if not isinstance(message, dict):
        raise ValueError("message must be an object")
    reject_unknown(message, {"message_id", "subject", "parts", "metadata"})
    if "message_id" in message:
        require_string(message, "message_id")
    if "subject" in message:
        validate_message_subject(message["subject"])
    if "metadata" in message:
        validate_message_metadata(message["metadata"])
    validate_message_parts(message.get("parts"))
    if "max_turns" in payload:
        require_positive_int(payload, "max_turns")
    if "task_expires_at" in payload:
        require_positive_int(payload, "task_expires_at")


def validate_coordinator_grant_issue(payload: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(
        payload,
        {
            "protocol_version",
            "issuance_key",
            "coordinator_agent_id",
            "investigation_id",
            "round_id",
            "approved_plan_digest",
            "authority_ref",
            "target_agent_ids",
            "task_count",
            "task_expires_at",
            "grant_expires_at",
            "operations",
        },
    )
    if payload.get("protocol_version") != PROTOCOL_V06:
        raise ValueError(f"protocol_version must be {PROTOCOL_V06}")
    for key in (
        "issuance_key",
        "coordinator_agent_id",
        "investigation_id",
        "round_id",
        "approved_plan_digest",
        "authority_ref",
    ):
        require_string(payload, key)
    targets = payload.get("target_agent_ids")
    if not isinstance(targets, list) or not targets:
        raise ValueError("target_agent_ids must be a non-empty array")
    normalized_targets = []
    for target in targets:
        if not isinstance(target, str) or not target.strip():
            raise ValueError("target_agent_ids entries must be non-empty strings")
        normalized_targets.append(target.strip())
    if len(set(normalized_targets)) != len(normalized_targets):
        raise ValueError("target_agent_ids entries must be unique")
    task_count = require_positive_int(payload, "task_count")
    if task_count > COORDINATOR_GRANT_MAX_TASKS:
        raise ValueError(
            f"task_count must be at most {COORDINATOR_GRANT_MAX_TASKS}"
        )
    require_positive_int(payload, "task_expires_at")
    require_positive_int(payload, "grant_expires_at")
    operations = payload.get("operations")
    if (
        not isinstance(operations, list)
        or not all(isinstance(item, str) for item in operations)
        or set(operations) != COORDINATOR_GRANT_OPERATIONS
    ):
        raise ValueError(
            "operations must contain exactly create, read, batch, and complete-own"
        )
    if len(operations) != len(COORDINATOR_GRANT_OPERATIONS):
        raise ValueError("operations entries must be unique")
    return {
        **payload,
        "target_agent_ids": sorted(normalized_targets),
        "operations": sorted(COORDINATOR_GRANT_OPERATIONS),
    }


def validate_coordinator_grant_resolve(payload: dict[str, Any]) -> None:
    reject_unknown(payload, {"idempotency_key", "work_item_id"})
    require_string(payload, "idempotency_key")
    if "work_item_id" in payload:
        require_string(payload, "work_item_id")


def validate_mutation_context(payload: dict[str, Any]) -> None:
    require_string(payload, "message_id")
    require_positive_int(payload, "turn_sequence")
    require_positive_int(payload, "expected_task_version")
    require_string(payload, "idempotency_key")


def validate_message_submit(payload: dict[str, Any]) -> None:
    reject_unknown(
        payload,
        {
            "actor_agent_id",
            "message_id",
            "turn_sequence",
            "expected_task_version",
            "idempotency_key",
            "parts",
        },
    )
    validate_mutation_context(payload)
    require_string(payload, "actor_agent_id")
    validate_message_parts(payload.get("parts"))


def validate_ack(payload: dict[str, Any]) -> None:
    reject_unknown(
        payload,
        {
            "task_id",
            "event_id",
            "message_id",
            "turn_sequence",
            "expected_task_version",
            "idempotency_key",
            "listener_instance_id",
            "readiness_epoch",
        },
    )
    validate_mutation_context(payload)
    require_string(payload, "task_id")
    require_string(payload, "event_id")
    require_string(payload, "listener_instance_id")
    require_positive_int(payload, "readiness_epoch")


def validate_delivery_fail(payload: dict[str, Any]) -> None:
    reject_unknown(
        payload,
        {
            "task_id",
            "event_id",
            "message_id",
            "turn_sequence",
            "expected_task_version",
            "idempotency_key",
            "listener_instance_id",
            "readiness_epoch",
            "reason",
        },
    )
    validate_ack({key: value for key, value in payload.items() if key != "reason"})
    reason = require_string(payload, "reason")
    if reason != "listener_persistence_failed":
        raise ValueError("delivery-fail reason must be listener_persistence_failed")


def validate_complete(payload: dict[str, Any]) -> None:
    reject_unknown(
        payload,
        {
            "actor_agent_id",
            "message_id",
            "turn_sequence",
            "expected_task_version",
            "idempotency_key",
            "completed_against_message_id",
        },
    )
    validate_mutation_context(payload)
    require_string(payload, "actor_agent_id")
    require_string(payload, "completed_against_message_id")


def validate_fail(payload: dict[str, Any]) -> None:
    reject_unknown(
        payload,
        {
            "actor_agent_id",
            "message_id",
            "turn_sequence",
            "expected_task_version",
            "idempotency_key",
            "reason",
        },
    )
    validate_mutation_context(payload)
    require_string(payload, "actor_agent_id")
    reason = require_string(payload, "reason")
    if reason not in TASK_FAILURE_REASONS:
        raise ValueError(f"unsupported failed reason: {reason}")


def validate_visibility_batch(payload: dict[str, Any]) -> list[str]:
    reject_unknown(payload, {"task_ids"})
    task_ids = payload.get("task_ids")
    if not isinstance(task_ids, list) or not task_ids:
        raise ValueError("task_ids must be a non-empty array")
    if len(task_ids) > MAX_VISIBILITY_BATCH_SIZE:
        raise ValueError(f"task_ids may contain at most {MAX_VISIBILITY_BATCH_SIZE} entries")
    if not all(isinstance(task_id, str) and task_id.strip() for task_id in task_ids):
        raise ValueError("task_ids entries must be non-empty strings")
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("task_ids must be de-duplicated")
    return task_ids


def validate_readiness_register(payload: dict[str, Any]) -> None:
    reject_unknown(
        payload,
        {
            "listener_instance_id",
            "client_version",
            "workspace_version",
            "transport",
            "recover_if_stale",
        },
    )
    require_string(payload, "listener_instance_id")
    require_string(payload, "client_version")
    require_string(payload, "workspace_version")
    require_string(payload, "transport")
    if "recover_if_stale" in payload and not isinstance(payload["recover_if_stale"], bool):
        raise ValueError("recover_if_stale must be a boolean")


def validate_readiness_publish(payload: dict[str, Any]) -> None:
    reject_unknown(payload, {"listener_instance_id", "readiness_epoch", "ready"})
    require_string(payload, "listener_instance_id")
    require_positive_int(payload, "readiness_epoch")
    if not isinstance(payload.get("ready"), bool):
        raise ValueError("ready must be a boolean")


def validate_event_ack(payload: dict[str, Any]) -> None:
    reject_unknown(payload, {"idempotency_key", "listener_instance_id", "readiness_epoch"})
    require_string(payload, "idempotency_key")
    require_string(payload, "listener_instance_id")
    require_positive_int(payload, "readiness_epoch")
