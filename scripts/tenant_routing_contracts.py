#!/usr/bin/env python3
"""Writes parity-rs's tenant routing contracts (0.16.0; CONVENTIONS "Tenant routing API"):
- schemas/tenant-routing/tenant-routing-requests.v1.json   the commands on tenant.routing.*.requests.<zone>
- schemas/tenant-routing/tenant-routing-replies.v1.json    the answers on replies.<service>
- schemas/tenant-routing/tenant-routing-events.v1.json     tenant.routing.events.<zone>
- schemas/tenant-routing/tenant-routing-handover.v1.json   tenant.routing.handover.<zone> (per-tenant services)
- schemas/tenant-routing/topology.v1.json                  parity-rs's topology file (regions, zones, units)
- tenants/test-topology.v1.json                            the topology of the shared test tenants
- fixtures/tenant-routing.v1/{valid,invalid}/*.json         kind "routing_ops"
Words (the user's glossary, 2026-11-03): region -> zone (= cell) -> deployment unit (= bin). parity-rs runs
leader + standby per zone and both decides and executes (no separate executor service).
Run from anywhere: python3 -I scripts/tenant_routing_contracts.py. Output is deterministic.
"""
import json
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://parity.example/schemas/tenant-routing"

TENANT_ID = {"type": "string", "pattern": "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
             "description": "A DNS label (CONVENTIONS \"Identifiers and values\")."}
NAME = {"type": "string", "pattern": "^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$",
        "description": "A region, zone (= cell), deployment unit (= bin) or media track name (eu1, eu1a, bin-a, blue)."}
UUID7 = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
         "description": "UUIDv7, lowercase."}
CMD_ID = {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,200}$"}
REPLY_TO = {"type": "string", "pattern": "^replies\\.[a-z0-9][a-z0-9.-]{0,200}$", "maxLength": 209}
MS = {"type": "integer", "minimum": 0, "maximum": 9007199254740991}
VERSION = {"type": "integer", "minimum": 0, "maximum": 9007199254740991,
           "description": "parity-store's placement version the operator saw (1-based; 0: none yet)."}
RELEASE = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"}
REASON = {"type": "string", "minLength": 10, "maxLength": 500,
          "description": "Why: shown in the audit and on the saga. No personal data."}
TICKET = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,99}$"}
ACTOR = {"description": "Who asked: a person (vouched for by the calling service) or a system. Informational: "
                        "authorization is the topic.",
         "oneOf": [
             {"type": "object", "additionalProperties": False, "required": ["sub", "username"],
              "properties": {"sub": {"type": "string", "minLength": 1, "maxLength": 128},
                             "username": {"type": "string", "pattern": "^[A-Za-z0-9._@-]{1,64}$"},
                             "roles": {"type": "array", "maxItems": 16,
                                       "items": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_.-]{0,63}$"}}}},
             {"type": "object", "additionalProperties": False, "required": ["system"],
              "properties": {"system": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,63}$"}}}]}
PLACEMENT = {"type": "object", "additionalProperties": False,
             "required": ["home_zone", "zone", "cell", "deployment_unit"],
             "description": "parity-store's placement fields (CONVENTIONS \"Tenant maps\"). `cell` names the zone that "
                            "serves the tenant (zone = cell); records written before 2026-11-03 hold the region in "
                            "`zone`, `home_zone` and `failover_zone`. parity-rs writes zone = home_zone = cell.",
             "properties": {"home_zone": NAME, "failover_zone": NAME, "zone": NAME, "cell": NAME,
                            "deployment_unit": NAME, "media_track": NAME}}

SAGA_KINDS = ["placement", "placement_update", "unit_move", "zone_move", "rollout", "drain"]
SAGA_STATES = ["accepted", "assigning", "recording", "preparing", "fencing", "switching", "handing_over",
               "compensating", "copying", "ready_to_cut", "cutting_over", "draining", "finishing",
               "canary_ready", "ring_moving", "ring_baking", "promoting", "watching", "paused", "rolling_back",
               "drained", "blocked", "done", "aborted", "cancelled"]
OUTCOMES = ["done", "aborted", "cancelled", "compensated", "rolled_back", "stopped"]
UNIT_ROLES = ["live", "canary", "previous", "empty", "draining", "failed"]
HEALTH = ["ok", "degraded", "failing", "unknown"]
ERROR_CODES = ["bad_request", "forbidden", "not_found", "wrong_zone", "conflict", "unavailable", "internal"]
RULES = ["version_mismatch", "saga_active", "unit_busy", "unit_role", "unit_not_ready", "release_mismatch",
         "tenant_pinned", "residency", "unknown_zone", "unknown_unit", "same_placement", "parent_takes_traffic",
         "no_capacity", "needs_operator", "past_cutover", "wrong_state", "ring_moved", "zone_copy_unavailable",
         "rollout_active", "not_leader"]

READ, OPS, ONB, PEER = "requests", "ops", "onboarding", "peer"
ALL_READ = [READ, OPS, ONB]


def obj(required, props, **more):
    return {"type": "object", "additionalProperties": False, "required": required, "properties": props, **more}


def op_fields(saga=False, version=False):
    p = {"op_id": UUID7, "actor": ACTOR, "reason": REASON, "ticket": TICKET}
    req = ["op_id", "actor", "reason"]
    if saga:
        p["saga_id"] = UUID7
        req.append("saga_id")
    if version:
        p["expected_version"] = VERSION
        req.append("expected_version")
    return req, p


# cmd: (topics, required, properties, description)
def commands():
    c = {}
    c["ping"] = (ALL_READ, [], {}, "Liveness of the zone's leader.")
    c["get_route"] = (ALL_READ, ["tenant_id"], {"tenant_id": TENANT_ID}, "One tenant's route.")
    c["list_routes"] = (ALL_READ, [], {"after": TENANT_ID, "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                                       "zone": NAME, "deployment_unit": NAME,
                                       "tier": {"enum": ["small", "mid", "enterprise"]},
                                       "owned_only": {"type": "boolean"}}, "A page of routes, by tenant id.")
    c["get_topology"] = (ALL_READ, [], {}, "Regions, zones and units with their role, release, readiness and health.")
    c["get_saga"] = (ALL_READ, ["saga_id"], {"saga_id": UUID7}, "One saga.")
    c["list_sagas"] = (ALL_READ, [], {"state": {"enum": ["active", "finished"]}, "kind": {"enum": SAGA_KINDS},
                                      "tenant_id": TENANT_ID, "after": UUID7,
                                      "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "A page of sagas.")
    req, p = op_fields(saga=True)
    c["place_tenant"] = ([ONB, OPS], ["tenant_id"] + req, dict(p, tenant_id=TENANT_ID, hint=obj(["residency"], {
        "residency": {"type": "string", "pattern": "^[a-z]{2,8}$"}, "preferred_zone": NAME,
        "country": {"type": "string", "pattern": "^[A-Z]{2}$"}, "seats": {"type": "integer", "minimum": 1,
                                                                          "maximum": 1000000}}),
        target=obj(["zone"], {"zone": NAME, "deployment_unit": NAME, "media_track": NAME, "failover_zone": NAME})),
        "Places a new tenant: parity-rs decides from `hint` (onboarding topic) or takes an operator's `target` "
        "(ops topic). Exactly one of them. An already placed tenant answers its placement, `changed: false`.")
    req, p = op_fields(version=True)
    c["validate_move"] = ([OPS], ["tenant_id"] + req, dict(p, tenant_id=TENANT_ID, to_unit=NAME, to_zone=NAME,
                                                           media_track=NAME, force={"type": "boolean"}),
                          "The dry run of move_tenant (to_unit) or move_tenant_zone (to_zone): a plan, changes nothing.")
    req, p = op_fields(saga=True, version=True)
    c["move_tenant"] = ([OPS], ["tenant_id", "to_unit"] + req, dict(p, tenant_id=TENANT_ID, to_unit=NAME,
                                                                    media_track=NAME, force={"type": "boolean"}),
                        "Moves a tenant to another deployment unit of its zone (the unit-move saga).")
    c["move_tenant_zone"] = ([OPS], ["tenant_id", "to_zone"] + req, dict(
        p, tenant_id=TENANT_ID, to_zone=NAME, to_unit=NAME, cutover={"enum": ["on_confirm", "auto"]},
        residency_approval_ref=TICKET),
        "Moves a tenant to another zone: the home-zone switch (the zone-move saga). v1 answers conflict "
        "zone_copy_unavailable.")
    c["set_failover_zone"] = ([OPS], ["tenant_id", "failover_zone"] + req, dict(
        p, tenant_id=TENANT_ID, failover_zone={"anyOf": [NAME, {"type": "null"}]}), "Sets or clears the failover zone.")
    req, p = op_fields()
    for name, desc in [("confirm_cutover", "Lets a zone move waiting in ready_to_cut cut over."),
                       ("cancel_saga", "Cancels a saga: its compensation for the state it is in."),
                       ("stop_rollout", "Pauses a rollout: no new moves; moves in flight finish."),
                       ("resume_rollout", "Resumes a paused rollout."),
                       ("rollback_rollout", "Moves every tenant of the canary unit back to the live unit.")]:
        c[name] = ([OPS], ["saga_id"] + req, dict(p, saga_id=UUID7), desc)
    c["resolve_saga"] = ([OPS], ["saga_id", "action"] + req, dict(p, saga_id=UUID7, action={
        "enum": ["retry_record", "accept_store"]}), "Resolves a blocked saga.")
    c["advance_rollout"] = ([OPS], ["saga_id", "expected_ring"] + req, dict(p, saga_id=UUID7, expected_ring={
        "type": "integer", "minimum": 1, "maximum": 64,
        "description": "The ring baking now (1-based); refused ring_moved once it has moved on."}),
        "Starts the next ring, or promotes the canary after the last one.")
    c["pin_tenant"] = ([OPS], ["tenant_id", "until_ms"] + req, dict(p, tenant_id=TENANT_ID, until_ms=MS),
                       "Pins a tenant to its unit until until_ms: moves and rollouts skip it.")
    c["unpin_tenant"] = ([OPS], ["tenant_id"] + req, dict(p, tenant_id=TENANT_ID), "Removes a pin.")
    req, p = op_fields(saga=True)
    ring = obj(["name", "select"], {"name": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,31}$"},
                                    "select": {"anyOf": [{"enum": ["test", "internal", "early"]},
                                                         obj(["percent"], {"percent": {"type": "integer", "minimum": 1,
                                                                                       "maximum": 100}})]}})
    c["start_rollout"] = ([OPS], ["zone", "release", "unit", "plan", "advance"] + req, dict(
        p, zone=NAME, release=RELEASE, unit=NAME, advance={"const": "manual"},
        plan=obj(["rings"], {"rings": {"type": "array", "minItems": 1, "maxItems": 16, "items": ring},
                             "max_parallel": {"type": "integer", "minimum": 1, "maximum": 100}})),
        "Starts a rollout of the release an empty unit runs; rings advance by hand in v1.")
    c["drain_unit"] = ([OPS], ["zone", "unit"] + req, dict(p, zone=NAME, unit=NAME, to_unit=NAME,
                                                          allow_release_change={"type": "boolean"}),
                       "Moves every tenant off a unit; the unit becomes empty.")
    req, p = op_fields()
    c["release_unit"] = ([OPS], ["zone", "unit"] + req, dict(p, zone=NAME, unit=NAME),
                         "A failed unit with no tenants becomes empty.")
    return c


def requests_schema():
    defs, refs = {}, []
    for cmd, (topics, required, props, desc) in commands().items():
        key = "cmd_" + cmd
        defs[key] = {"description": desc, "x-topics": topics, **obj(
            ["schema_version", "cmd", "id", "reply_to"] + required,
            {"schema_version": {"const": 1}, "cmd": {"const": cmd}, "id": CMD_ID, "reply_to": REPLY_TO, **props})}
        if cmd == "place_tenant":
            defs[key]["oneOf"] = [{"required": ["hint"]}, {"required": ["target"]}]
        refs.append({"$ref": f"#/$defs/{key}"})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"{BASE}/requests/v1.json",
            "title": "parity-rs tenant routing requests (0.16.0)",
            "description": "Requests to a zone's parity-rs on tenant.routing.requests.<zone> (reads only), "
                           "tenant.routing.ops.requests.<zone> (parity-cc-tools only: every operation) and "
                           "tenant.routing.onboarding.requests.<zone> (admin-console only: place_tenant with a "
                           "hint). `x-topics` lists the topics a command is allowed on; on any other it is "
                           "answered forbidden and changes nothing (authorization is the topic). Every operation "
                           "carries op_id (one per confirmed operator action), actor and reason; a saga-starting "
                           "one its saga_id (the idempotency key). CONVENTIONS \"Tenant routing API\".",
            "x-reserved": ["peer_place", "peer_prepare_inbound", "peer_activate_inbound", "peer_release_inbound",
                           "peer_report"],
            "oneOf": refs, "$defs": defs}


ROUTE = obj(["tenant_id", "placement", "version"], {
    "tenant_id": TENANT_ID, "name": {"type": "string", "maxLength": 200},
    "tier": {"enum": ["small", "mid", "enterprise"]}, "parent": {"anyOf": [TENANT_ID, {"type": "null"}]},
    "placement": {"anyOf": [PLACEMENT, {"type": "null"}]}, "version": VERSION, "revision": MS, "owner_zone": NAME,
    "assignment": {"anyOf": [{"type": "null"}, obj(["unit"], {"unit": NAME, "media_track": NAME, "epoch": MS})]},
    "unit": {"anyOf": [{"type": "null"}, obj(["role"], {"role": {"enum": UNIT_ROLES}, "release": RELEASE,
                                                        "ready": {"type": "boolean"}, "health": {"enum": HEALTH}})]},
    "pin": {"anyOf": [{"type": "null"}, obj(["until_ms"], {"until_ms": MS, "reason": REASON})]},
    "active_saga": {"anyOf": [{"type": "null"}, obj(["saga_id", "kind", "state"], {
        "saga_id": UUID7, "kind": {"enum": SAGA_KINDS}, "state": {"enum": SAGA_STATES}})]}})
UNIT = obj(["unit", "role"], {"unit": NAME, "zone": NAME, "role": {"enum": UNIT_ROLES}, "release": RELEASE,
                              "ready": {"type": "boolean"}, "health": {"enum": HEALTH},
                              "replicas": {"type": "integer", "minimum": 0}, "tenant_count": {"type": "integer",
                                                                                               "minimum": 0},
                              "reason": {"type": "string", "maxLength": 200}})


def replies_schema():
    error = obj(["code", "message"], {"code": {"enum": ERROR_CODES}, "message": {"type": "string", "maxLength": 500},
                                      "rule": {"enum": RULES}, "owner_zone": NAME})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"{BASE}/replies/v1.json",
            "title": "parity-rs tenant routing answers (0.16.0)",
            "description": "Answers on the request's reply_to, keyed like the request. result's shape follows the "
                           "command (the $defs name them: route, routes, topology, saga, sagas, accepted, plan); "
                           "readers ignore fields they don't know.",
            "oneOf": [obj(["schema_version", "id", "ok", "result"], {
                "schema_version": {"const": 1}, "id": CMD_ID, "ok": {"const": True}, "result": {"type": "object"}}),
                obj(["schema_version", "id", "ok", "error"], {
                    "schema_version": {"const": 1}, "id": CMD_ID, "ok": {"const": False}, "error": error})],
            "$defs": {"route": ROUTE, "unit": UNIT,
                      "accepted": obj(["accepted"], {"accepted": {"const": True}, "saga_id": UUID7,
                                                     "changed": {"type": "boolean"},
                                                     "placement": {"anyOf": [PLACEMENT, {"type": "null"}]}})}}


EVENT_BASE = {"schema_version": {"const": 1}, "event_id": UUID7, "at_ms": MS, "zone": NAME, "tenant_id": TENANT_ID,
              "saga_id": UUID7, "parent_saga_id": UUID7, "op_id": UUID7}
SEQ = {"type": "integer", "minimum": 1, "maximum": 9007199254740991}


def event_types():
    return {
        "route.changed": (["tenant_id", "route", "cause"], {"route": ROUTE, "previous": PLACEMENT, "cause": {
            "enum": ["place", "move", "zone_move", "failover_change", "import"]}}, "tenant",
            "A tenant's placement changed (parity-store answered the write)."),
        "route.deleted": (["tenant_id", "route"], {"route": ROUTE}, "tenant",
                          "The tenant (and so its placement) was deleted in parity-store."),
        "saga.accepted": (["saga_id", "seq", "kind", "actor"], {"seq": SEQ, "kind": {"enum": SAGA_KINDS},
                                                               "actor": ACTOR, "reason": REASON,
                                                               "plan": {"type": "object"}}, "saga",
                          "A saga was accepted."),
        "saga.state_changed": (["saga_id", "seq", "kind", "from_state", "to_state"], {
            "seq": SEQ, "kind": {"enum": SAGA_KINDS}, "from_state": {"enum": SAGA_STATES},
            "to_state": {"enum": SAGA_STATES}, "step": {"type": "string", "maxLength": 64},
            "progress": obj(["done", "total"], {"done": {"type": "integer", "minimum": 0},
                                                "failed": {"type": "integer", "minimum": 0},
                                                "total": {"type": "integer", "minimum": 0}}),
            "detail": {"type": "string", "maxLength": 500}}, "saga", "A saga moved to another state."),
        "saga.finished": (["saga_id", "seq", "kind", "outcome"], {"seq": SEQ, "kind": {"enum": SAGA_KINDS},
                                                                  "outcome": {"enum": OUTCOMES},
                                                                  "after": PLACEMENT,
                                                                  "detail": {"type": "string", "maxLength": 500}},
                          "saga", "A saga ended."),
        "saga.blocked": (["saga_id", "seq", "kind", "reason"], {"seq": SEQ, "kind": {"enum": SAGA_KINDS},
                                                                "reason": {"type": "string", "maxLength": 500}},
                         "saga", "A saga needs an operator (resolve_saga)."),
        "unit.changed": (["unit"], {"unit": UNIT, "previous_role": {"enum": UNIT_ROLES}}, "unit",
                         "A unit's role, release, readiness or health changed."),
        "assignment.changed": (["tenant_id", "unit", "epoch"], {"unit": NAME, "media_track": NAME, "epoch": MS,
                                                                "previous_unit": NAME}, "tenant",
                               "A tenant's assignment in the zone's etcd changed; epoch is its revision."),
        "drift.detected": (["tenant_id", "store", "assignment"], {"store": NAME, "assignment": NAME}, "tenant",
                           "The assignment names another unit than parity-store's placement, with no saga to "
                           "explain it."),
    }


def events_schema():
    defs, refs = {}, []
    for t, (required, props, key, desc) in event_types().items():
        name = "ev_" + t.replace(".", "_")
        defs[name] = {"description": desc + f" Key: {key}" + (" (unit:<name>)." if key == "unit" else " id."),
                      "x-key": key, **obj(["schema_version", "event_id", "type", "at_ms", "zone"] + required,
                                          dict(EVENT_BASE, type={"const": t}, **props))}
        refs.append({"$ref": f"#/$defs/{name}"})
    defs["route"], defs["unit"] = ROUTE, UNIT
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"{BASE}/events/v1.json",
            "title": "parity-rs tenant routing events (0.16.0)",
            "description": "tenant.routing.events.<zone>, written in the zone leader's Kafka transactions (read with "
                           "read_committed). Keyed by tenant id (route.*, assignment.changed, drift.detected), saga "
                           "id (saga.*) or unit:<name> (unit.changed); `x-key` names it. Saga events carry seq "
                           "(1, 2, … per saga). Headers: schema_version, traceparent, tenant_id when the body has "
                           "one, op_id when the body has one.",
            "oneOf": refs, "$defs": defs}


def handover_schema():
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"{BASE}/handover/v1.json",
            "title": "Handover reports to parity-rs (0.16.0)",
            "description": "tenant.routing.handover.<zone>, written by a zone's per-tenant services (interaction-"
                           "engine, routing-engine, gateway, …) when they have handed over (released) or taken "
                           "over (adopted) a tenant's live work for an assignment epoch (ARCH §7.8). parity-rs "
                           "waits for `released` from each service its topology lists before a unit move is done; "
                           "a report for an older epoch is ignored. Keyed by tenant id; header tenant_id.",
            **obj(["schema_version", "event_id", "tenant_id", "unit", "epoch", "service", "instance", "state",
                   "at_ms"], {"schema_version": {"const": 1}, "event_id": UUID7, "tenant_id": TENANT_ID, "unit": NAME,
                              "epoch": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
                              "service": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,62}$"},
                              "instance": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"},
                              "state": {"enum": ["released", "adopted"]}, "at_ms": MS, "saga_id": UUID7})}


def topology_schema():
    profile = obj([], {"full": {"type": "integer", "minimum": 0, "maximum": 1000},
                       "minimum": {"type": "integer", "minimum": 0, "maximum": 1000},
                       "per_ring_tenants": {"type": "integer", "minimum": 1, "maximum": 100000}})
    unit = obj([], {"namespace": {"type": "string", "pattern": "^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"},
                    "release": RELEASE, "ready": {"type": "boolean"}, "role": {"enum": UNIT_ROLES},
                    "scale": profile},
               description="A deployment unit (= bin). release, ready and role are the static observer's values "
                           "(local-dev, tests) and the first start's roles; the Kubernetes observer reads release "
                           "and readiness from the workloads' labels.")
    zone = obj(["region", "open", "capacity_seats", "default_media_track", "media_tracks", "units"], {
        "region": NAME, "failover": NAME, "open": {"type": "boolean"},
        "state": {"enum": ["active", "degraded", "closed"]},
        "capacity_seats": {"type": "integer", "minimum": 1}, "fill_limit": {"type": "number", "exclusiveMinimum": 0,
                                                                            "maximum": 1},
        "default_media_track": NAME, "media_tracks": {"type": "array", "minItems": 1, "items": NAME},
        "handover_services": {"type": "array", "items": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,62}$"}},
        "units": {"type": "object", "minProperties": 1, "propertyNames": NAME, "additionalProperties": unit}},
        description="A zone (= cell): one complete stack inside a region.")
    region = obj(["residency", "default_zone"], {"residency": {"type": "string", "pattern": "^[a-z]{2,8}$"},
                                                 "default_zone": NAME, "dedicated_zones": {"type": "boolean"}})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"{BASE}/topology/v1.json",
            "title": "parity-rs topology (0.16.0)",
            "description": "The regions, zones (= cells) and deployment units (= bins) parity-rs places tenants "
                           "on. Rendered by parity-deploy from its inventory (YAML or JSON); parity-rs refuses to "
                           "start on an invalid file. Cross-field rules (contract-check, parity-rs): a region's "
                           "default_zone is one of its zones; failover names another zone of the same residency; "
                           "default_media_track is in media_tracks.",
            **obj(["schema_version", "regions", "zones"], {
                "schema_version": {"const": 1}, "description": {"type": "string"},
                "regions": {"type": "object", "minProperties": 1, "propertyNames": NAME,
                            "additionalProperties": region},
                "zones": {"type": "object", "minProperties": 1, "propertyNames": NAME,
                          "additionalProperties": zone}})}


TEST_TOPOLOGY = {
    "schema_version": 1,
    "description": "The topology of the shared test tenants (tenants/test-tenants.v1.json): region eu1 holds zone "
                   "eu1a with three deployment units; region eu2 holds zone eu2a, eu1a's failover zone. The test "
                   "tenants' records predate 2026-11-03: they hold the region in zone, home_zone and failover_zone "
                   "and the zone in cell.",
    "regions": {"eu1": {"residency": "eu", "default_zone": "eu1a"},
                "eu2": {"residency": "eu", "default_zone": "eu2a"}},
    "zones": {
        "eu1a": {"region": "eu1", "failover": "eu2a", "open": True, "state": "active", "capacity_seats": 20000,
                 "fill_limit": 0.8, "default_media_track": "blue", "media_tracks": ["blue", "green"],
                 "handover_services": [],
                 "units": {"bin-a": {"namespace": "bin-a", "release": "1.8.3", "ready": True, "role": "live",
                                     "scale": {"full": 3, "minimum": 1}},
                           "bin-b": {"namespace": "bin-b", "release": "1.8.3", "ready": True, "role": "previous",
                                     "scale": {"full": 3, "minimum": 1}},
                           "bin-c": {"namespace": "bin-c", "release": "1.9.0", "ready": True, "role": "empty",
                                     "scale": {"full": 3, "minimum": 1, "per_ring_tenants": 50}}}},
        "eu2a": {"region": "eu2", "failover": "eu1a", "open": True, "state": "active", "capacity_seats": 20000,
                 "fill_limit": 0.8, "default_media_track": "blue", "media_tracks": ["blue"],
                 "units": {"bin-a": {"namespace": "bin-a", "release": "1.8.3", "ready": True, "role": "live"}}}}}

TRACE = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
OP = "0193a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b"
SAGA = "0193a1b2-c3d4-7e5f-9a6b-7c8d9e0f1a2c"
SAGA2 = "0193a1b2-c3d4-7e5f-aa6b-7c8d9e0f1a2d"
EVT = "0193a1b2-c3d4-7e5f-ba6b-7c8d9e0f1a3"
T0 = 1793000000000
PERSON = {"sub": "5f0c2a9e-1b2c-4d3e-8f40-123456789abc", "username": "jdoe", "roles": ["ops"]}
REASON_TEXT = "Move acme to the previous release for a support case"
ACME_PLACEMENT = {"home_zone": "eu1a", "failover_zone": "eu2a", "zone": "eu1a", "cell": "eu1a",
                  "deployment_unit": "bin-a", "media_track": "blue"}
ARGS = {
    "ping": {}, "get_route": {"tenant_id": "acme"}, "list_routes": {"zone": "eu1a", "limit": 100},
    "get_topology": {}, "get_saga": {"saga_id": SAGA}, "list_sagas": {"state": "active", "limit": 50},
    "place_tenant": {"tenant_id": "acme", "hint": {"residency": "eu", "preferred_zone": "eu1a", "country": "GB",
                                                   "seats": 20}},
    "validate_move": {"tenant_id": "acme", "to_unit": "bin-b", "expected_version": 7},
    "move_tenant": {"tenant_id": "acme", "to_unit": "bin-b", "expected_version": 7},
    "move_tenant_zone": {"tenant_id": "acme", "to_zone": "eu2a", "cutover": "on_confirm", "expected_version": 7},
    "set_failover_zone": {"tenant_id": "acme", "failover_zone": "eu2a", "expected_version": 7},
    "confirm_cutover": {"saga_id": SAGA}, "cancel_saga": {"saga_id": SAGA},
    "resolve_saga": {"saga_id": SAGA, "action": "retry_record"},
    "stop_rollout": {"saga_id": SAGA}, "resume_rollout": {"saga_id": SAGA}, "rollback_rollout": {"saga_id": SAGA},
    "advance_rollout": {"saga_id": SAGA, "expected_ring": 1},
    "pin_tenant": {"tenant_id": "acme", "until_ms": T0 + 86400000}, "unpin_tenant": {"tenant_id": "acme"},
    "start_rollout": {"zone": "eu1a", "release": "1.9.0", "unit": "bin-c", "advance": "manual",
                      "plan": {"rings": [{"name": "test", "select": "test"}, {"name": "five", "select": {"percent": 5}}],
                               "max_parallel": 10}},
    "drain_unit": {"zone": "eu1a", "unit": "bin-b", "to_unit": "bin-a"},
    "release_unit": {"zone": "eu1a", "unit": "bin-c"},
}


def request(cmd_name, topic_kind=None, key=None, **override):
    cmd = cmd_name
    topics, required, props, _ = commands()[cmd]
    kind = topic_kind or topics[0]
    topic = {READ: "tenant.routing.requests.eu1a", OPS: "tenant.routing.ops.requests.eu1a",
             ONB: "tenant.routing.onboarding.requests.eu1a"}[kind]
    reply_to = "replies.admin-console" if kind == ONB else "replies.parity-cc-tools"
    value = {"schema_version": 1, "cmd": cmd, "id": f"cct-1:{cmd}-0001", "reply_to": reply_to, **ARGS[cmd]}
    if "op_id" in props:
        value.update(op_id=OP, actor=PERSON if kind != ONB else {"system": "parity-admin-console"},
                     reason=REASON_TEXT)
    if "saga_id" in required:
        value.setdefault("saga_id", SAGA)
    for k, v in override.items():
        if v is None:
            value.pop(k, None)
        else:
            value[k] = v
    headers = {"schema_version": "1", "traceparent": TRACE}
    if "tenant_id" in value:
        headers["tenant_id"] = value["tenant_id"]
    if "op_id" in value:
        headers["op_id"] = value["op_id"]
    return {"topic": topic, "key": key or value.get("tenant_id") or value.get("saga_id") or f"cct-1:{cmd}-0001",
            "headers": headers, "value": value}


ROUTE_ACME = {"tenant_id": "acme", "name": "Acme Ltd", "tier": "small", "parent": None, "placement": ACME_PLACEMENT,
              "version": 7, "revision": 1834, "owner_zone": "eu1a",
              "assignment": {"unit": "bin-a", "media_track": "blue", "epoch": 7702},
              "unit": {"role": "live", "release": "1.8.3", "ready": True, "health": "ok"}, "pin": None,
              "active_saga": None}


def event(etype, key=None, **body):
    v = {"schema_version": 1, "event_id": EVT + "a", "type": etype, "at_ms": T0, "zone": "eu1a", **body}
    headers = {"schema_version": "1", "traceparent": TRACE}
    if "tenant_id" in v:
        headers["tenant_id"] = v["tenant_id"]
    if "op_id" in v:
        headers["op_id"] = v["op_id"]
    k = key
    if k is None:
        k = {"tenant": v.get("tenant_id"), "saga": v.get("saga_id"),
             "unit": "unit:" + (v.get("unit", {}) or {}).get("unit", "") if isinstance(v.get("unit"), dict) else None
             }[event_types()[etype][2]]
    return {"topic": "tenant.routing.events.eu1a", "key": k, "headers": headers, "value": v}


def reply(result=None, error=None, topic="replies.parity-cc-tools"):
    v = {"schema_version": 1, "id": "cct-1:move_tenant-0001", "ok": error is None}
    if error is None:
        v["result"] = result
    else:
        v["error"] = error
    return {"topic": topic, "key": "acme", "headers": {"schema_version": "1", "traceparent": TRACE,
                                                       "tenant_id": "acme", "op_id": OP}, "value": v}


def handover(**override):
    v = {"schema_version": 1, "event_id": EVT + "b", "tenant_id": "acme", "unit": "bin-a", "epoch": 7710,
         "service": "parity-interaction-engine", "instance": "ie-bin-a-0", "state": "released", "at_ms": T0,
         "saga_id": SAGA}
    v.update(override)
    return {"topic": "tenant.routing.handover.eu1a", "key": "acme",
            "headers": {"schema_version": "1", "traceparent": TRACE, "tenant_id": "acme"}, "value": v}


def fixtures():
    valid, invalid = {}, {}
    for cmd in commands():
        valid[f"request-{cmd.replace('_', '-')}"] = (cmd + " on " + commands()[cmd][0][0], request(cmd), "apply")
    valid["request-place-tenant-target"] = ("place_tenant with an operator's target, on the ops topic", request(
        "place_tenant", OPS, hint=None, target={"zone": "eu1a", "deployment_unit": "bin-a"}), "apply")
    valid["request-read-on-ops-topic"] = ("A read on the ops topic is answered too", request("get_route", OPS),
                                          "apply")
    valid["request-system-actor"] = ("An automatic action: a system actor", request(
        "rollback_rollout", actor={"system": "auto-rollback"}), "apply")
    invalid["request-move-on-read-topic"] = ("move_tenant on the read topic: forbidden, nothing changes",
                                             request("move_tenant", READ), ["wrong_topic"], "reply_error:forbidden")
    invalid["request-move-on-onboarding-topic"] = ("admin-console's topic carries place_tenant only",
                                                   request("move_tenant", ONB), ["wrong_topic"],
                                                   "reply_error:forbidden")
    invalid["request-place-target-on-onboarding"] = ("An operator's target on the onboarding topic: forbidden",
                                                     request("place_tenant", ONB, hint=None,
                                                             target={"zone": "eu1a"}),
                                                     ["wrong_topic"], "reply_error:forbidden")
    invalid["request-place-hint-and-target"] = ("Both hint and target on the ops topic: bad_request", request(
        "place_tenant", OPS, target={"zone": "eu1a"}), ["schema"], "reply_error:bad_request")
    invalid["request-no-reason"] = ("An operation without a reason: bad_request", request("move_tenant", reason=None),
                                    ["schema"], "reply_error:bad_request")
    invalid["request-no-op-id"] = ("An operation without op_id: bad_request", request("drain_unit", op_id=None),
                                   ["schema"], "reply_error:bad_request")
    invalid["request-no-expected-version"] = ("A tenant operation without expected_version: bad_request",
                                              request("move_tenant", expected_version=None), ["schema"],
                                              "reply_error:bad_request")
    invalid["request-peer-reserved"] = ("A reserved peer command (built with the second zone): bad_request",
                                        request("ping", cmd="peer_report"), ["schema"], "reply_error:bad_request")
    invalid["request-no-id"] = ("No id: never answered", request("ping", id=None), ["schema", "bad_id"], "dlt")
    invalid["request-no-reply-to"] = ("No reply_to: never answered", request("get_route", reply_to=None),
                                      ["schema", "bad_reply_to"], "dlt")
    invalid["request-reply-to-events"] = ("A reply_to that isn't replies.*: never answered", request(
        "get_route", reply_to="tenant.routing.events.eu1a"), ["schema", "bad_reply_to"], "dlt")
    invalid["request-not-json"] = ("A body that isn't JSON: never answered",
                                   {"topic": "tenant.routing.ops.requests.eu1a", "key": "acme", "headers": {}},
                                   ["schema", "bad_id", "bad_reply_to"], "dlt")
    invalid["request-tenant-header-mismatch"] = ("The tenant_id header names another tenant: bad_request",
                                                 request("get_route") | {"headers": {"schema_version": "1",
                                                                                     "traceparent": TRACE,
                                                                                     "tenant_id": "globex"}},
                                                 ["header_body_mismatch"], "reply_error:bad_request")

    valid["reply-route"] = ("get_route's answer", reply({"route": ROUTE_ACME}), "apply")
    valid["reply-accepted"] = ("A saga accepted", reply({"accepted": True, "saga_id": SAGA}), "apply")
    valid["reply-placed"] = ("place_tenant's answer to admin-console", reply(
        {"accepted": True, "saga_id": SAGA, "changed": True, "placement": ACME_PLACEMENT},
        topic="replies.admin-console"), "apply")
    valid["reply-wrong-zone"] = ("Another zone owns the tenant", reply(error={
        "code": "wrong_zone", "message": "another zone owns this tenant", "owner_zone": "eu2a"}), "apply")
    valid["reply-zone-copy-unavailable"] = ("v1 refuses zone moves", reply(error={
        "code": "conflict", "message": "zone moves need the zone copier, not built yet",
        "rule": "zone_copy_unavailable"}), "apply")
    invalid["reply-no-id"] = ("An answer without id: dropped", reply({"pong": True}) | {"value": {
        "schema_version": 1, "ok": True, "result": {"pong": True}}}, ["schema"], "reject")

    ev = {}
    ev["route.changed"] = event("route.changed", tenant_id="acme", saga_id=SAGA, op_id=OP, route=ROUTE_ACME,
                                previous=dict(ACME_PLACEMENT, deployment_unit="bin-b"), cause="move")
    ev["route.deleted"] = event("route.deleted", tenant_id="acme", route=ROUTE_ACME)
    ev["saga.accepted"] = event("saga.accepted", tenant_id="acme", saga_id=SAGA, op_id=OP, seq=1, kind="unit_move",
                                actor=PERSON, reason=REASON_TEXT, plan={"to_unit": "bin-b"})
    ev["saga.state_changed"] = event("saga.state_changed", tenant_id="acme", saga_id=SAGA, op_id=OP, seq=3,
                                     kind="unit_move", from_state="fencing", to_state="switching", step="store")
    ev["saga.finished"] = event("saga.finished", tenant_id="acme", saga_id=SAGA, op_id=OP, seq=5, kind="unit_move",
                                outcome="compensated", detail="parity-store refused the write: not_found")
    ev["saga.blocked"] = event("saga.blocked", tenant_id="acme", saga_id=SAGA, op_id=OP, seq=4, kind="unit_move",
                               reason="another change touched the same fields")
    ev["unit.changed"] = event("unit.changed", saga_id=SAGA2, op_id=OP, unit={
        "unit": "bin-c", "zone": "eu1a", "role": "canary", "release": "1.9.0", "ready": True, "health": "ok",
        "replicas": 3, "tenant_count": 0}, previous_role="empty")
    ev["assignment.changed"] = event("assignment.changed", tenant_id="acme", saga_id=SAGA, op_id=OP, unit="bin-b",
                                     media_track="blue", epoch=7710, previous_unit="bin-a")
    ev["drift.detected"] = event("drift.detected", tenant_id="acme", store="bin-a", assignment="bin-b")
    for t, rec in ev.items():
        valid["event-" + t.replace(".", "-").replace("_", "-")] = (t, rec, "apply")
    e = event("saga.finished", tenant_id="acme", saga_id=SAGA, op_id=OP, seq=5, kind="unit_move", outcome="done")
    e["key"] = "acme"
    invalid["event-saga-keyed-by-tenant"] = ("A saga event keyed by the tenant, not the saga", e, ["key_mismatch"],
                                             "reject")
    e = event("route.deleted", tenant_id="acme", route=ROUTE_ACME)
    del e["headers"]["tenant_id"]
    invalid["event-missing-tenant-header"] = ("No tenant_id header on a tenant's event", e, ["missing_header"],
                                              "reject")
    e = event("route.deleted", tenant_id="acme", route=ROUTE_ACME)
    e["headers"]["tenant_id"] = "globex"
    invalid["event-header-body-mismatch"] = ("The tenant header names another tenant", e, ["header_body_mismatch"],
                                             "reject")
    e = event("saga.state_changed", tenant_id="acme", saga_id=SAGA, seq=0, kind="unit_move", from_state="fencing",
              to_state="switching")
    invalid["event-seq-zero"] = ("seq starts at 1", e, ["schema"], "reject")

    valid["handover-released"] = ("IE released acme's calls for epoch 7710", handover(), "apply")
    valid["handover-adopted"] = ("The new unit's IE adopted them", handover(state="adopted", unit="bin-b",
                                                                           instance="ie-bin-b-0"), "apply")
    h = handover()
    h["key"] = "globex"
    invalid["handover-key-mismatch"] = ("A report keyed by another tenant", h, ["key_mismatch"], "reject")
    invalid["handover-epoch-zero"] = ("No epoch 0", handover(epoch=0), ["schema"], "reject")
    return valid, invalid


def write(rel, doc):
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def main():
    write("schemas/tenant-routing/tenant-routing-requests.v1.json", requests_schema())
    write("schemas/tenant-routing/tenant-routing-replies.v1.json", replies_schema())
    write("schemas/tenant-routing/tenant-routing-events.v1.json", events_schema())
    write("schemas/tenant-routing/tenant-routing-handover.v1.json", handover_schema())
    write("schemas/tenant-routing/topology.v1.json", topology_schema())
    write("tenants/test-topology.v1.json", TEST_TOPOLOGY)
    out = ROOT / "fixtures" / "tenant-routing.v1"
    if out.exists():
        shutil.rmtree(out)
    valid, invalid = fixtures()
    for name, (description, record, reader) in valid.items():
        write(f"fixtures/tenant-routing.v1/valid/{name}.json", {"description": description, "kind": "routing_ops",
                                                                "writer_valid": True, "reader": reader,
                                                                "record": record})
    for name, (description, record, violates, reader) in invalid.items():
        write(f"fixtures/tenant-routing.v1/invalid/{name}.json", {"description": description, "kind": "routing_ops",
                                                                  "writer_valid": False, "violates": violates,
                                                                  "reader": reader, "record": record})
    print(f"wrote 5 schemas, the test topology, {len(valid)} valid and {len(invalid)} invalid routing_ops fixtures")


if __name__ == "__main__":
    main()
