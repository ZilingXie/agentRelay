from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import sqlite3
import time
from typing import Callable, TypeVar


DEFAULT_MAX_INFLIGHT = 1
MAX_CONFIGURED_INFLIGHT = 100

T = TypeVar("T")


@dataclass(frozen=True)
class DeliveryLane:
    protocol_version: str
    db_path: Path


class DeliveryClaimContext:
    def __init__(
        self,
        control: "DeliveryControl",
        agent_id: str,
        max_inflight: int,
    ):
        self._control = control
        self.agent_id = agent_id
        self.max_inflight = max_inflight

    def inflight_count(self) -> int:
        return self._control.inflight_count(self.agent_id)

    def has_capacity(self) -> bool:
        return self.inflight_count() < self.max_inflight

    def has_capacity_for_lane(
        self,
        lane_db_path: str | Path,
        local_inflight_count: int,
    ) -> bool:
        other_inflight = self._control.inflight_count(
            self.agent_id,
            exclude_lane_path=lane_db_path,
        )
        return other_inflight + int(local_inflight_count) < self.max_inflight


class DeliveryControl:
    """Cross-process admission gate for all durable delivery lanes."""

    def __init__(self, db_path: str | Path, lanes: list[DeliveryLane]):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        unique_lanes: dict[Path, DeliveryLane] = {}
        for lane in lanes:
            resolved = lane.db_path.resolve()
            unique_lanes[resolved] = DeliveryLane(lane.protocol_version, resolved)
        self.lanes = tuple(unique_lanes.values())
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_delivery_limits (
                    agent_id TEXT PRIMARY KEY,
                    max_inflight INTEGER NOT NULL
                        CHECK (max_inflight BETWEEN 1 AND 100),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )

    def run_claim(
        self,
        agent_id: str,
        operation: Callable[[DeliveryClaimContext], T],
        *,
        now: int | None = None,
    ) -> T:
        timestamp = int(time.time()) if now is None else int(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            max_inflight = self._get_or_create_limit_conn(conn, agent_id, timestamp)
            return operation(DeliveryClaimContext(self, agent_id, max_inflight))

    def get_max_inflight(self, agent_id: str, *, now: int | None = None) -> int:
        timestamp = int(time.time()) if now is None else int(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._get_or_create_limit_conn(conn, agent_id, timestamp)

    def set_max_inflight(
        self,
        agent_id: str,
        max_inflight: int,
        *,
        now: int | None = None,
    ) -> dict[str, int | str]:
        if not 1 <= int(max_inflight) <= MAX_CONFIGURED_INFLIGHT:
            raise ValueError(
                f"max_inflight must be between 1 and {MAX_CONFIGURED_INFLIGHT}"
            )
        timestamp = int(time.time()) if now is None else int(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO agent_delivery_limits (
                    agent_id, max_inflight, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    max_inflight = excluded.max_inflight,
                    updated_at = excluded.updated_at
                """,
                (agent_id, int(max_inflight), timestamp, timestamp),
            )
        return {"agent_id": agent_id, "max_inflight": int(max_inflight)}

    def configured_limits(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT agent_id, max_inflight FROM agent_delivery_limits ORDER BY agent_id"
            ).fetchall()
        return {str(row["agent_id"]): int(row["max_inflight"]) for row in rows}

    def metrics_summary(self) -> dict[str, object]:
        limits = self.configured_limits()
        agents: dict[str, dict[str, object]] = {}
        total_ack_latencies: list[int] = []
        total_recovery_latencies: list[int] = []

        for lane in self.lanes:
            if not lane.db_path.exists():
                continue
            uri = lane.db_path.as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                if "agents" in tables:
                    for row in conn.execute("SELECT agent_id FROM agents"):
                        self._ensure_agent_metrics(agents, str(row["agent_id"]), limits)
                if "agent_events" not in tables:
                    continue
                columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(agent_events)")
                }
                inflight_started = (
                    "inflight_started_at" if "inflight_started_at" in columns else "NULL"
                )
                parked_at = "parked_at" if "parked_at" in columns else "NULL"
                recovery_attempts = (
                    "recovery_attempts" if "recovery_attempts" in columns else "0"
                )
                rows = conn.execute(
                    f"""
                    SELECT agent_id, outbox_status, acked_at,
                           {inflight_started} AS inflight_started_at,
                           {parked_at} AS parked_at,
                           {recovery_attempts} AS recovery_attempts
                    FROM agent_events
                    """
                ).fetchall()
                for row in rows:
                    agent_id = str(row["agent_id"])
                    metrics = self._ensure_agent_metrics(agents, agent_id, limits)
                    by_protocol = metrics["by_protocol"]
                    protocol_counts = by_protocol.setdefault(
                        lane.protocol_version,
                        {"queued": 0, "inflight": 0, "parked": 0},
                    )
                    status = str(row["outbox_status"])
                    if status in {"queued", "inflight", "parked"}:
                        metrics[status] = int(metrics[status]) + 1
                        protocol_counts[status] += 1

                    acked_at = row["acked_at"]
                    started_at = row["inflight_started_at"]
                    if acked_at is not None and started_at is not None:
                        latency = max(0, int(acked_at) - int(started_at))
                        metrics["_ack_latencies"].append(latency)
                        total_ack_latencies.append(latency)
                    parked = row["parked_at"]
                    if (
                        acked_at is not None
                        and parked is not None
                        and int(row["recovery_attempts"] or 0) > 0
                    ):
                        latency = max(0, int(acked_at) - int(parked))
                        metrics["_recovery_latencies"].append(latency)
                        total_recovery_latencies.append(latency)

        for agent_id in limits:
            self._ensure_agent_metrics(agents, agent_id, limits)

        values = []
        for agent_id in sorted(agents):
            metrics = agents[agent_id]
            ack_latencies = metrics.pop("_ack_latencies")
            recovery_latencies = metrics.pop("_recovery_latencies")
            metrics["ack_latency_seconds"] = _latency_stats(ack_latencies)
            metrics["recovery_latency_seconds"] = _latency_stats(recovery_latencies)
            values.append(metrics)

        return {
            "totals": {
                "queued": sum(int(item["queued"]) for item in values),
                "inflight": sum(int(item["inflight"]) for item in values),
                "parked": sum(int(item["parked"]) for item in values),
                "ack_latency_seconds": _latency_stats(total_ack_latencies),
                "recovery_latency_seconds": _latency_stats(total_recovery_latencies),
            },
            "agents": values,
        }

    def inflight_count(
        self,
        agent_id: str,
        *,
        exclude_lane_path: str | Path | None = None,
    ) -> int:
        excluded = Path(exclude_lane_path).resolve() if exclude_lane_path else None
        total = 0
        for lane in self.lanes:
            if excluded is not None and lane.db_path == excluded:
                continue
            if not lane.db_path.exists():
                continue
            uri = f"file:{lane.db_path}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=30) as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_events'"
                ).fetchone()
                if not table:
                    continue
                total += int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM agent_events
                        WHERE agent_id = ? AND outbox_status = 'inflight'
                        """,
                        (agent_id,),
                    ).fetchone()[0]
                )
        return total

    @staticmethod
    def _ensure_agent_metrics(
        agents: dict[str, dict[str, object]],
        agent_id: str,
        limits: dict[str, int],
    ) -> dict[str, object]:
        return agents.setdefault(
            agent_id,
            {
                "agent_id": agent_id,
                "max_inflight": limits.get(agent_id, DEFAULT_MAX_INFLIGHT),
                "queued": 0,
                "inflight": 0,
                "parked": 0,
                "by_protocol": {},
                "_ack_latencies": [],
                "_recovery_latencies": [],
            },
        )

    @staticmethod
    def _get_or_create_limit_conn(
        conn: sqlite3.Connection,
        agent_id: str,
        now: int,
    ) -> int:
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_delivery_limits (
                agent_id, max_inflight, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (agent_id, DEFAULT_MAX_INFLIGHT, now, now),
        )
        row = conn.execute(
            "SELECT max_inflight FROM agent_delivery_limits WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"delivery limit missing after initialization: {agent_id}")
        return int(row["max_inflight"])


def default_delivery_control(
    lane_db_path: str | Path,
    protocol_version: str,
) -> DeliveryControl:
    lane_path = Path(lane_db_path)
    control_path = lane_path.with_name(f"{lane_path.name}.delivery-control.sqlite3")
    return DeliveryControl(control_path, [DeliveryLane(protocol_version, lane_path)])


def resolve_delivery_control_path(
    configured_path: str,
    lanes: list[DeliveryLane],
) -> Path:
    if configured_path.strip():
        return Path(configured_path.strip())
    if not lanes:
        raise ValueError("at least one delivery lane is required")
    return lanes[0].db_path.parent / "agentrelay-delivery-control.sqlite3"


def _latency_stats(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)

    def percentile(fraction: float) -> int:
        index = max(0, math.ceil(len(ordered) * fraction) - 1)
        return int(ordered[index])

    return {
        "count": len(ordered),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "max": int(ordered[-1]),
    }
