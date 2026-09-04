#!/usr/bin/env node

import { readFileSync, readdirSync } from "node:fs";
import Ajv2020 from "ajv/dist/2020.js";

const schemaDir = new URL("../schemas/", import.meta.url);
const exampleDir = new URL("../examples/protocol-v06/", import.meta.url);
const ajv = new Ajv2020({ allErrors: true, strict: false });
const schemas = new Map();

for (const fileName of readdirSync(schemaDir)) {
  if (!fileName.endsWith(".schema.json")) continue;
  const schema = JSON.parse(readFileSync(new URL(fileName, schemaDir), "utf8"));
  schemas.set(fileName, schema);
  ajv.addSchema(schema);
}

const exampleSchemas = {
  "event-ack.json": "event-ack-v06.schema.json",
  "message-ack.json": "message-ack-v06.schema.json",
  "message-delivery-fail.json": "message-delivery-fail-v06.schema.json",
  "task-complete.json": "task-terminal-v06.schema.json",
  "task-create.json": "task-create-v06.schema.json",
  "task-message.json": "task-message-v06.schema.json",
};

for (const [exampleName, schemaName] of Object.entries(exampleSchemas)) {
  const value = JSON.parse(readFileSync(new URL(exampleName, exampleDir), "utf8"));
  const validate = ajv.getSchema(schemas.get(schemaName).$id);
  if (!validate(value)) {
    throw new Error(
      `${exampleName} failed ${schemaName}: ${ajv.errorsText(validate.errors)}`,
    );
  }
}

const v05Outbox = schemas.get("protocol-v05-common.schema.json").$defs.outbox;
const v06Outbox = schemas.get("protocol-v06-common.schema.json").$defs.outbox;
if (v05Outbox.properties.outbox_status.enum.includes("parked")) {
  throw new Error("Protocol v0.5 must not gain the v0.6 parked wire state");
}
if (!v06Outbox.properties.outbox_status.enum.includes("parked")) {
  throw new Error("Protocol v0.6 must expose parked in the outbox status enum");
}

const v06FailureReasons =
  schemas.get("task-terminal-v06.schema.json").properties.reason.enum;
for (const transportReason of [
  "delivery_retry_exhausted",
  "listener_persistence_failed",
]) {
  if (v06FailureReasons.includes(transportReason)) {
    throw new Error(`${transportReason} must not be a v0.6 Task failure reason`);
  }
}

for (const packet of [
  { kind: "result", status: "answered", summary: "Data returned.", data: { rows: 3 } },
  { kind: "result", status: "blocked", summary: "Access is missing.", blocker: { code: "access_denied" } },
  { kind: "result", status: "failed", summary: "Query failed.", error: { code: "query_failed" } },
]) {
  const validate = ajv.getSchema(schemas.get("result-packet-v06.schema.json").$id);
  if (!validate(packet)) {
    throw new Error(`Result Packet failed validation: ${ajv.errorsText(validate.errors)}`);
  }
}
const validateResult = ajv.getSchema(schemas.get("result-packet-v06.schema.json").$id);
if (validateResult({ kind: "result", status: "blocked", summary: "Missing blocker." })) {
  throw new Error("blocked Result Packet must require blocker");
}

console.log("protocol v0.6 schema conformance passed (6 examples, 3 Result Packets)");
