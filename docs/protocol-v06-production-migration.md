# Protocol v0.6 Production Migration

Protocol v0.6 uses a separate database and is a maintenance-window cutover.
Never point `V06Store` at the v0.5 database: the Task protocol constraint is
different and the migration must reset Listener epochs.

## Preconditions

1. Server and every enabled production Listener support v0.6.
2. Each enabled Agent has an operator-confirmed v0.6 deployment path.
3. Server source is clean and synchronized with the reviewed migration commit.
4. The v0.5 preflight passes and no unrelated incident is active.
5. Record the Server commit, image id, database digest, and Listener versions.

## Cutover

1. Stop Hermes dispatch and all collaboration mutation callers.
2. Set `AGENTRELAY_MUTATION_MODE=closed`, rebuild/restart API and WS, and verify
   public health reports `write_mode=closed`.
3. Stop API and WS so the v0.5 SQLite files form a stable snapshot. Preserve
   the database, WAL/SHM files if present, deployment environment, and current
   Docker image as the pre-cutover rollback set.
4. Run the migration against the stable snapshot. The destination must not
   already exist, and every enabled source Agent must be named explicitly:

   ```text
   python3 scripts/migrate_v05_to_v06.py \
     data/agentrelay-v05.snapshot.sqlite3 \
     data/agentrelay-v06.sqlite3 \
     --enable-agent zac-agent \
     --enable-agent vivi-agent \
     --enable-agent project-hermes
   ```

5. Keep API, WS, dispatchers, and interactive mutation callers stopped while
   the migration command completes its integrity, count, protocol, capability,
   Event-state, and readiness-empty checks. Do not print Task payloads.
6. Configure `AGENTRELAY_MUTATION_MODE=v06` and the migrated v0.6 database,
   then start API and WS while callers remain paused. Start all v0.6 Listeners
   immediately; old readiness epochs are intentionally not migrated.
7. Require fresh v0.6 readiness for every enabled Agent and verify public
   health plus the v0.6 manifest both report `write_mode=v06`. Only then resume
   interactive mutation callers.
8. Enable the Hermes dispatcher on `agent-collab-v0.6` after Listener
   readiness is fresh. Run one offline-create/recovery probe without sending a
   real WeCom notification, then run the normal two-Agent completion proof.

## Migration Semantics

- Task ids, lineage, Messages, audit history, and idempotency records are kept.
- Task `protocol_version` becomes `agent-collab-v0.6` so history remains
  available through the active store.
- Existing Listener readiness is discarded; every Listener gets a new epoch.
- `queued`, `inflight`, and `retry_wait` Events become `parked`, with leases and
  retry timestamps cleared. `acked` and historical `exhausted` Events remain
  terminal audit history.
- Only explicitly named enabled Agents receive the v0.6 capability.

## Rollback Boundary

Before the first v0.6 mutation, return callers to v0.5, restore the pre-cutover
environment/image/database set, and restart in `v05`. After any v0.6 Task,
Message, ACK/NACK, terminal, or follow-up mutation commits, do not restore the
old database. Set mutations to `closed` and repair forward from the v0.6 data.
