# parity-contracts

The shared contracts of the contact-center platform: gRPC protos, the JSON Schemas of the
Kafka messages, the conventions they follow, and golden fixtures that every side's tests run.
It is ARCHITECTURE.md §8's `proto/` plus the schemas (PLAN question 11 of
parity-interaction-engine). Designed in parity-interaction-engine's
`docs/adr/0001-contracts-and-skeleton.md` (revision 2).

**Status (2026-10-27):** 0.4.0 adds parity-store's tenant maps file and the shared test tenants;
0.3.0 parity-gateway's contracts. 0.2.0 adds `telephony.proto` (parity-telephony-controller's step 5);
0.1.0 was built for interaction-engine's step 0. A plain
directory: no git yet (the user's decision), so versions are in `VERSION` and
`CHANGELOG.md`.

## What's here

| Path | What |
|---|---|
| `proto/parity/routing/v1/routing.proto` | interaction-engine ↔ routing-engine (parity-poc) |
| `proto/parity/telephony/v1/telephony.proto` | interaction-engine ↔ parity-telephony-controller (0.2.0) |
| `schemas/events/interaction-events.v1.json` | `interaction.events`: 18 types, 13 reserved names |
| `schemas/commands/interaction-commands.v1.json` | `interaction.commands`: `originate`, `redirect`, `hangup`; 4 reserved |
| `schemas/commands/interaction-replies.v1.json` | IE's answers on `replies.*` |
| `schemas/fixtures/fixture.v1.json` | The fixture file format |
| `schemas/tenants/tenant-maps.v1.json` | parity-store's maps file: tenants, tiers, parents, placements, numbers, SIP domains (0.4.0) |
| `schemas/store/store-{requests,replies,events,state}.v1.json` | parity-store's Kafka API: commands, answers, change events, the compacted state topic (0.5.0); the cell map in Redis is in CONVENTIONS.md (0.6.0) |
| `schemas/billing/billing-{requests,replies,events,entitlements}.v1.json` | parity-billing's Kafka API: the web and console commands, answers, money events, the compacted entitlements (0.10.0) |
| `schemas/admin/provisioning-{requests,replies}.v1.json` | admin-console's provisioning topic: the tenant-id namespace and `provision_tenant` (0.10.0) |
| `tenants/test-entitlements.v1.json` | The shared test tenants' entitlements (0.10.0) |
| `schemas/logging/log-record.v1.json`, `log-catalogue.v1.json` | The one structured log line every service writes, and a service's event catalogue (0.13.0; CONVENTIONS "Logging") |
| `logging/events.common.v1.json`, `logging/examples/` | The events every service shares, and a starter catalogue per service to copy (0.13.0) |
| `tenants/test-tenants.v1.json` | **The shared test tenants** every service's tests can use: `acme`, `globex`, `initech` (+ `initech-de`, `initech-uk`), with numbers, SIP domains and agent ids (0.4.0) |
| `CONVENTIONS.md` | The rules: ids, topics, headers, keys, versions, `reply_to`, idempotency, ordering, errors, timeouts, the routing lifecycle, the verb fetch |
| `fixtures/` | 137 golden fixtures (Kafka records and gRPC scripts; `telephony.v1/deferred.json` lists what has no script yet) |
| `masking/cases.v1.json` | The cross-language masking cases (ie-obs and TC's `obs.Pii`) |
| `rust/contract-check/` | Checks the schemas and fixtures |
| `rust/parity-proto/` | Rust bindings: prost messages and tonic clients (protox, no `protoc`) |
| `java-check/` | Optional: the same schemas through networknt 3.0.8 (`CI_JAVA=1`) |
| `scripts/` | `ci.sh`, `tools.sh` (+ `tools.env`), `buf.sh`, `release.sh`, `telephony_scripts.py` (writes `fixtures/telephony.v1/`), `routing_scripts.py` (writes the executable `fixtures/routing.v1/` scripts), `gateway_contracts.py`, `tenant_maps_fixtures.py` (writes `fixtures/tenant-maps.v1/`), `store_contracts.py` (writes `schemas/store/` and `fixtures/store.v1/`), `logging_fixtures.py` (writes `logging/` and `fixtures/log-record.v1/`) |
| `.baseline/` | The last release: `buf.binpb`, its valid Kafka fixtures, telephony scripts, tenant maps fixtures and test tenants (the break gates) |

Reserved for later: `proto/parity/interaction/v1/control.proto` (step 11),
`proto/parity/routing/v1/agents.proto`, `schemas/verbs/verbs.v1.json` (step 10),
`schemas/config/*.v1.json` (step 10).

## Owners

| Contract | Owners |
|---|---|
| `routing.proto` | parity-poc (routing-engine) and interaction-engine |
| `telephony.proto`, `fixtures/telephony.v1/` | parity-telephony-controller and interaction-engine |
| `interaction.events` | interaction-engine, the only writer |
| `interaction.commands` | interaction-engine (reader) with parity-voice-api and parity-admin-console (writers) |
| `tenant-maps.v1.json`, `fixtures/tenant-maps.v1/` | parity-store (owner of the tenant id format) |
| `tenants/test-tenants.v1.json` | parity-store; read by every service's tests (additions only) |
| `schemas/billing/`, `fixtures/billing.v1/`, `tenants/test-entitlements.v1.json`, `masking/billing.v1.json` | parity-billing (writer of answers, events, entitlements); parity-frontend (web requests); admin-console (console requests); every entitlements reader |
| `schemas/admin/` | admin-console (answers provisioning); parity-billing (the only writer of requests) |
| `schemas/store/`, `fixtures/store.v1/` | parity-store (writer of answers, events, state); admin-console (admin requests); every local copy (state) |
| `schemas/logging/`, `logging/events.common.v1.json`, `fixtures/log-record.v1/` | Every service (writers); parity-deploy (Fluent Bit, Loki). A service's own `logging/events.<service>.v1.json` lives in its repository |

A change needs the owners' agreement, a changelog entry and a release.

## Checking

```
scripts/tools.sh     # once: the digest-pinned buf image, cargo-deny 0.20.2
scripts/ci.sh        # proto format, lint, breaking; descriptor set; the tool's fmt, clippy,
                     # tests, cargo-deny; every fixture; the JSON Schema break gate
CI_JAVA=1 scripts/ci.sh   # adds the networknt 3.0.8 stage (needs Maven)
scripts/release.sh 0.1.1  # cut a version (see CHANGELOG.md)
```

buf runs in a hardened container (`scripts/buf.sh`: no network, read-only, no capabilities,
`proto/` read-only).

## Consuming (before git)

- **Rust:** a path dependency on `../parity-contracts/rust/parity-proto` (both packages); a git
  tag later.
- **Java:** tests read `${parity.contracts.dir:../parity-contracts}`; parity-telephony-controller
  generates its grpc-java stubs from `proto/` (`io.github.ascopes:protobuf-maven-plugin`); a Maven
  artifact later.
  networknt must register `x-pii` and `x-reserved` as annotation keywords.
- **C++ (parity-poc):** a Bazel repository rule pointing here, with a `BUILD` file parity-poc
  owns (`proto_library`, `cc_proto_library`, gRPC's `cc_grpc_library`).

## Consumers: what each reads, and what it must change

Listed, not done (ADR 0001 §4.8, §11).

**parity-voice-api**
- Reads `initiated`, `ringing`, `answered`, `ended` with `cpaas: true`, `interaction_id` (a
  UUID), `tenant_id`, `at_ms`, `end_reason`: all unchanged. Already right: an unknown
  `end_reason` becomes `failed`, and a status only moves forward.
- Must: write the headers `tenant_id`, `interaction_id`, `schema_version`, `traceparent`
  (not `X-Tenant-ID`/`X-Interaction-ID`) and body `schema_version`, `interaction_id`; read
  the tenant header before decoding; skip an unknown major; dead-letter poison events; map
  `call_ended` → 409, `unavailable`/`internal` → 503 (today all three become 502); reuse the
  request `id` on a retry; accept a missing `from` and the `redirect` reason in the verb
  fetch; align `FakeInteractionEngine`; run these fixtures in its tests.

**parity-signal-detection**
- Reads `hold_started`, `hold_ended`, `transferred`, `ended`: unchanged. `silence_*` are
  reserved (no producer until parity-ms has voice activity). `sip_call` is gone: Call-IDs come
  from `leg_added`, which is authoritative.
- Must: stop writing `interaction.events`; take tenants from IE's events, never from SIP
  headers; read the tenant header before decoding; drop stale `(owner_epoch, seq)`.

**parity-admin-console**
- Its config topics already use the header `tenant_id`. Its rule "every Kafka request is
  keyed by tenant" (PLAN.md:252) needs an exception: commands on `interaction.commands` are
  keyed by `interaction_id`.

**parity-deploy**
- `SECURITY.md:182`: `interaction.events` is written by interaction-engine only, and read by
  voice-api, signal-detection and later the gateway. Rows for `interaction.commands`,
  IE's `replies.*` writes and the dead-letter topic (72 h, operators only).

**parity-telephony-controller:** serves `telephony.proto` (step 5: the server on a simulated node,
and `fake-telephony-controller` with its test-only control endpoint on 9702); its runners play
every `fixtures/telephony.v1/` script against both; its `obs.Pii` passes `masking/cases.v1.json`.

**interaction-engine (telephony):** IE step 2 builds the client on `rust/parity-proto`, tests it
against `fake-telephony-controller` with the same scripts, and takes the IE-side rules of TC ADR 0001
§4.8 (one mutating command in flight per session, ack every `delivery_seq`, ack `SessionEnded` after
the ENDED checkpoint, lost contact is not death) and the renames of its §9.

**Every service's tests (0.4.0):** may read `tenants/test-tenants.v1.json` (through
`${parity.contracts.dir}` or the Rust path dependency's directory) instead of hard-coding
tenants. Not done for any service yet. Known differences: parity-gateway's `AgentsFlowTest` logs
agent `agent-i1` into `initech`, which is a parent here (no traffic): the shared file has
`agent-i1` on `initech-de`. interaction-engine's demo numbers `+15550100100` and `+15550100200`
are acme's.

**parity-poc (routing-engine):** implement `routing.proto` with CONVENTIONS.md's lifecycle;
deferred by the user ("we will deal with the routing engine later").

**Logging, log-record.v1 and the xsid (0.13.0): what each service changes.** Listed, not done. Every
service: write `log-record.v1` (CONVENTIONS "Logging"): lowercase `level`; `msg` (not `message`);
`component` (not `target`); a catalogue `event` on every record; `seq` (+ `incarnation`); event
fields under `attrs`; `error.{code,kind,message,stack}` (the Java services' `cause` = an exception
class becomes `error.kind`; their `error`/`stack` strings move into `error`); `trace_id`/`span_id`
from the incoming `traceparent`; `xsid` (+ `tenant_id`) on every call-scoped record; copy
`logging/examples/events.<service>.v1.json` into the repository and run
`contract-check --logs` on the logs its tests capture.
- **interaction-engine** (ie-obs `JsonFormat`; **done** 2026-11-02, IE ie-integration-1: its
  catalogue `logging/events.parity-interaction-engine.v1.json`, its ci.sh checks the records its
  tests capture, the CDR's `parent_interaction_id` is migration 0003): the envelope as above; span fields become
  correlation fields or `attrs` instead of top-level keys; `code` from tracing metadata; IE mints the
  xsid of outbound, callback, campaign and consult calls (UUIDv7, already) and carries `parent_xsid`;
  a consult becomes a child interaction (today a CONSULT state of the customer's); the CDR gains
  `parent_interaction_id`; read `X-Parity-Interaction-Id` before `X-Interaction-ID`.
- **parity-telephony-controller, parity-gateway, parity-store, parity-billing, parity-frontend,
  parity-admin-console** (one shared `obs.JsonLogFormatter` design): the envelope as above;
  `node`/`bin` become correlation fields; key-value pairs and MDC go under `attrs` unless they are
  log-record fields; TC keeps passing `tenant_id` and `interaction_id` (the xsid) to parity-ms on every
  command that creates a call; the gateway and admin-console show the xsid ("Call ID") in call
  details.
- **parity-ms** (tracing-subscriber's default JSON: `timestamp`, `fields.message`, `threadName`):
  its own formatter in the fixed 1 KiB slot (fields shortened, never the line cut: a cut line isn't
  JSON); `leg.*` records carry `xsid` and `tenant_id` from the API's `interaction_id` and
  `tenant_id` (already sent on a call's creation); no per-packet records.
- **parity-cc-routing** (`src/ops/log.cc`: `ts_ms`, every value a string): `ts`, `version`,
  `instance`, `seq`, `event`; typed values (numbers stay numbers); `decision` on `agent.selected`
  (debug or sampled); its `RateLimit` writes `repeat_count`.
- **parity-deploy**: opensips mints a UUIDv7 (its `$uuid` is not one) and writes
  `X-Parity-Interaction-Id` next to `X-Interaction-ID`; Fluent Bit parses the line (today it ships it
  as a string inside `log`) and keeps `xsid`, `tenant_id`, `trace_id` as structured metadata, not
  labels.

