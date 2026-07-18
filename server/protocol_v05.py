from __future__ import annotations

from typing import Any


PROTOCOL_V05 = "agent-collab-v0.5"
TASK_STATES = {"open", "completed", "expired", "failed"}
MESSAGE_DELIVERY_STATES = {"pending", "delivered", "failed"}
OUTBOX_STATES = {"queued", "inflight", "acked", "retry_wait", "exhausted"}
TERMINAL_TASK_STATES = {"completed", "expired", "failed"}
RESERVED_TASK_STATES = {"cancelled", "archived"}

MAX_DELIVERY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = (60, 300, 600)
DELIVERY_ACK_LEASE_SECONDS = 60
LISTENER_READINESS_PUBLISH_INTERVAL_SECONDS = 60
LISTENER_READINESS_MAX_AGE_SECONDS = 300
MAX_VISIBILITY_BATCH_SIZE = 100

TASK_FAILURE_REASONS = {
    "delivery_retry_exhausted",
    "listener_persistence_failed",
    "relay_persistence_failed",
    "agent_reported_failure",
    "max_turns_exhausted",
    "internal_consistency_error",
}
DELIVERY_FAILURE_REASONS = {
    "delivery_retry_exhausted",
    "listener_persistence_failed",
}
OUTBOX_LAST_ERRORS = {
    "listener_unavailable",
    "socket_write_failed",
    "ack_lease_expired",
}
OUTBOX_EXHAUSTION_REASONS = {
    *DELIVERY_FAILURE_REASONS,
    "task_expired",
    "task_failed",
}


def is_protocol_v05(payload: dict[str, Any] | None) -> bool:
    return bool(payload and payload.get("protocol_version") == PROTOCOL_V05)


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


def validate_message_parts(parts: Any) -> list[dict[str, Any]]:
    if not isinstance(parts, list) or not parts:
        raise ValueError("parts must be a non-empty array")
    if not all(isinstance(part, dict) and part for part in parts):
        raise ValueError("parts entries must be non-empty objects")
    return parts


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
    if payload.get("protocol_version") != PROTOCOL_V05:
        raise ValueError(f"protocol_version must be {PROTOCOL_V05}")
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
    reject_unknown(message, {"message_id", "parts"})
    if "message_id" in message:
        require_string(message, "message_id")
    validate_message_parts(message.get("parts"))
    if "max_turns" in payload:
        require_positive_int(payload, "max_turns")
    if "task_expires_at" in payload:
        require_positive_int(payload, "task_expires_at")


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
        {"listener_instance_id", "client_version", "workspace_version", "transport"},
    )
    require_string(payload, "listener_instance_id")
    require_string(payload, "client_version")
    require_string(payload, "workspace_version")
    require_string(payload, "transport")


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
