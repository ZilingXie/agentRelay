from __future__ import annotations

import os
from typing import Any


DEFAULT_TRANSPORT_PONG_TIMEOUT_SECONDS = 90.0

TRANSPORT_DISCONNECT_REASONS = {
    "heartbeat_timeout",
    "client_close",
    "unexpected_data_frame",
    "superseded",
    "connection_lost",
}


def transport_pong_timeout_seconds() -> float:
    raw = os.environ.get("AGENTRELAY_WS_PONG_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TRANSPORT_PONG_TIMEOUT_SECONDS
    value = float(raw)
    if value <= 0:
        raise ValueError("AGENTRELAY_WS_PONG_TIMEOUT_SECONDS must be a positive number")
    return value


def transport_is_online(
    transport: dict[str, Any] | None,
    *,
    now: int,
    pong_timeout_seconds: float,
) -> bool:
    if not transport or transport.get("state") != "connected":
        return False
    connected_at = int(transport.get("connected_at") or 0)
    last_pong_at = transport.get("last_pong_at")
    reference = int(last_pong_at) if last_pong_at is not None else connected_at
    return now - reference <= pong_timeout_seconds


def combine_listener_status(
    *,
    readiness: dict[str, Any] | None,
    transport: dict[str, Any] | None,
    pending_event_count: int,
    now: int,
    pong_timeout_seconds: float,
    readiness_max_age_seconds: int = 300,
) -> dict[str, Any]:
    listener_ready = bool(readiness and readiness.get("ready"))
    readiness_fresh = bool(
        readiness
        and readiness.get("ready")
        and readiness.get("observed_at") is not None
        and int(readiness["observed_at"]) >= now - readiness_max_age_seconds
    )
    transport_online = transport_is_online(
        transport, now=now, pong_timeout_seconds=pong_timeout_seconds
    )
    can_receive_push = transport_online and listener_ready and readiness_fresh
    if not transport_online:
        if transport is None:
            status = "unknown" if readiness else "offline_idle"
        elif pending_event_count > 0:
            status = "offline_with_pending"
        else:
            status = "offline_idle"
    elif can_receive_push:
        status = "online_ready"
    else:
        status = "online_not_ready"
    return {
        "transport_online": transport_online,
        "listener_ready": listener_ready,
        "readiness_fresh": readiness_fresh,
        "can_receive_push": can_receive_push,
        "status": status,
    }
