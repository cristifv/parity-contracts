#!/usr/bin/env python3
"""Writes parity-store's Kafka contracts (parity-store PLAN.md §2–3):
- schemas/store/store-requests.v1.json: commands on tenant.store.requests (read commands) and
  tenant.store.admin.requests (every command but the placement writes; "x-admin": true marks the
  writes) and, since 0.16.0, tenant.store.placement.requests (parity-rs only: set_placement and
  delete_placement, plus the reads);
- schemas/store/store-replies.v1.json: answers on replies.*, with the result shapes in $defs;
- schemas/store/store-events.v1.json: tenant.store.events;
- schemas/store/store-state.v1.json: tenant.store.state (compacted; a null value is a tombstone);
- fixtures/store.v1/: valid and invalid records (kind "store").
Run from anywhere: python3 -I scripts/store_contracts.py. Output is deterministic.
"""
import json
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
TRACE = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
T0 = 1792800000000

TENANT_ID = {"type": "string", "pattern": "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"}
NAME = {"type": "string", "minLength": 1, "maxLength": 200, "pattern": "^[^\u0000-\u001f\u007f]+$"}
TIER = {"enum": ["small", "mid", "enterprise"]}
LOCATION = {"type": "string", "pattern": "^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$"}
PHONE = {"type": "string", "pattern": "^\\+[1-9][0-9]{6,14}$", "x-pii": "phone"}
DOMAIN = {"type": "string", "maxLength": 253,
          "pattern": "^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"}
REQUEST_ID = {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,200}$"}
REPLY_TO = {"type": "string", "pattern": "^replies\\.[a-z0-9][a-z0-9.-]{0,200}$"}
UUID = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
MS = {"type": "integer", "minimum": 0}
VERSION = {"type": "integer", "minimum": 1}
EXPECTED = {"type": "integer", "minimum": 0,
            "description": "Optimistic locking: the current version must equal this (0: the tenant, or the placement, must not exist). Left out: unconditional"}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 500, "description": "Default 100"}
REVISION = {"type": "integer", "minimum": 1, "description": "The store-wide change counter: one value per committed change, in commit order"}

PLACEMENT_IN = {
    "type": "object", "additionalProperties": False,
    "required": ["home_zone", "zone", "cell", "deployment_unit"],
    "properties": {"home_zone": LOCATION, "failover_zone": LOCATION, "zone": LOCATION, "cell": LOCATION,
                   "deployment_unit": LOCATION, "media_track": LOCATION},
}
NULLABLE_LOCATION = {"anyOf": [LOCATION, {"type": "null"}]}
PLACEMENT_VIEW = {
    "type": "object", "additionalProperties": False,
    "required": ["home_zone", "failover_zone", "zone", "cell", "deployment_unit", "media_track", "version",
                 "updated_at_ms"],
    "properties": {"home_zone": LOCATION, "failover_zone": NULLABLE_LOCATION, "zone": LOCATION, "cell": LOCATION,
                   "deployment_unit": LOCATION, "media_track": NULLABLE_LOCATION, "version": VERSION,
                   "updated_at_ms": MS},
}
NULLABLE_TENANT = {"anyOf": [TENANT_ID, {"type": "null"}]}
NULLABLE_PLACEMENT = {"anyOf": [{"$ref": "#/$defs/placement"}, {"type": "null"}]}
TENANT_RECORD = {
    "type": "object", "additionalProperties": False,
    "required": ["tenant_id", "name", "tier", "parent", "version", "updated_at_ms"],
    "properties": {"tenant_id": TENANT_ID, "name": NAME, "tier": TIER, "parent": NULLABLE_TENANT,
                   "version": VERSION, "updated_at_ms": MS},
}
TENANT_VIEW = {
    "type": "object", "additionalProperties": False,
    "required": ["tenant_id", "name", "tier", "parent", "sub_tenants", "version", "updated_at_ms", "placement",
                 "numbers", "sip_domains"],
    "properties": {"tenant_id": TENANT_ID, "name": NAME, "tier": TIER, "parent": NULLABLE_TENANT,
                   "sub_tenants": {"type": "array", "items": TENANT_ID}, "version": VERSION, "updated_at_ms": MS,
                   "placement": NULLABLE_PLACEMENT,
                   "numbers": {"type": "integer", "minimum": 0, "description": "How many (list_numbers pages them)"},
                   "sip_domains": {"type": "integer", "minimum": 0}},
}


def obj(required, props, **extra):
    d = {"type": "object", "required": list(required), "properties": props}
    d.update(extra)
    return d


# cmd: (admin?, required args, args)
COMMANDS = {
    "ping": (False, [], {}),
    "get_tenant": (False, ["tenant_id"], {"tenant_id": TENANT_ID}),
    "list_tenants": (False, [], {"after": TENANT_ID, "limit": LIMIT, "parent": TENANT_ID}),
    "get_placement": (False, ["tenant_id"], {"tenant_id": TENANT_ID}),
    "list_placements": (False, [], {"after": TENANT_ID, "limit": LIMIT}),
    "list_numbers": (False, ["tenant_id"], {"tenant_id": TENANT_ID, "after": PHONE, "limit": LIMIT}),
    "list_sip_domains": (False, ["tenant_id"], {"tenant_id": TENANT_ID, "after": DOMAIN, "limit": LIMIT}),
    "lookup_number": (False, ["number"], {"number": PHONE}),
    "lookup_sip_domain": (False, ["domain"], {"domain": DOMAIN}),
    "get_digest": (False, [], {}),
    "put_tenant": (True, ["tenant_id", "name"], {"tenant_id": TENANT_ID, "name": NAME, "tier": TIER,
                                                  "parent": TENANT_ID, "expected_version": EXPECTED}),
    "delete_tenant": (True, ["tenant_id"], {"tenant_id": TENANT_ID, "expected_version": EXPECTED}),
    "set_placement": (True, ["tenant_id", "placement"], {"tenant_id": TENANT_ID, "placement": PLACEMENT_IN,
                                                         "expected_version": EXPECTED}),
    "delete_placement": (True, ["tenant_id"], {"tenant_id": TENANT_ID, "expected_version": EXPECTED}),
    "add_number": (True, ["tenant_id", "number"], {"tenant_id": TENANT_ID, "number": PHONE}),
    "remove_number": (True, ["tenant_id", "number"], {"tenant_id": TENANT_ID, "number": PHONE}),
    "add_sip_domain": (True, ["tenant_id", "domain"], {"tenant_id": TENANT_ID, "domain": DOMAIN}),
    "remove_sip_domain": (True, ["tenant_id", "domain"], {"tenant_id": TENANT_ID, "domain": DOMAIN}),
}

EVENTS = {
    "tenant.changed": (["tenant"], {"tenant": {"$ref": "#/$defs/tenant_record"},
                                    "previous": {"$ref": "#/$defs/tenant_record"}}),
    "tenant.deleted": (["tenant"], {"tenant": {"$ref": "#/$defs/tenant_record"}}),
    "placement.changed": (["placement"], {"placement": {"$ref": "#/$defs/placement"},
                                          "previous": {"$ref": "#/$defs/placement"}}),
    "placement.deleted": (["placement"], {"placement": {"$ref": "#/$defs/placement"}}),
    "number.added": (["number"], {"number": PHONE}),
    "number.removed": (["number"], {"number": PHONE}),
    "sip_domain.added": (["domain"], {"domain": DOMAIN}),
    "sip_domain.removed": (["domain"], {"domain": DOMAIN}),
}

ERROR_CODES = ["bad_request", "forbidden", "not_found", "conflict", "unavailable", "internal"]
ERROR_RULES = ["duplicate_tenant", "unknown_parent", "nested_sub_tenant", "parent_not_enterprise", "tier_mismatch",
               "missing_tier", "parent_takes_traffic", "missing_placement", "duplicate_number", "duplicate_sip_domain",
               "version_mismatch", "has_sub_tenants", "has_numbers", "other_tenant", "placement_via_parity_rs",
               "placement_only"]


def schema_doc(sid, title, description, body):
    doc = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": sid, "title": title,
           "description": description}
    doc.update(body)
    return doc


def requests_schema():
    defs = {}
    for cmd, (admin, required, args) in COMMANDS.items():
        d = obj(["cmd", *required], {"cmd": {"const": cmd}, **args})
        if admin:
            d["x-admin"] = True
        defs["cmd_" + cmd] = d
    return schema_doc(
        "https://parity.example/schemas/store-requests/v1.json",
        "parity-store commands, schema_version 1",
        "On tenant.store.requests (read commands only: a command marked x-admin there is answered forbidden) "
        "and tenant.store.admin.requests (every command but the placement writes; written only by admin-console, "
        "by Kafka ACL). parity-store stores and decides nothing: placements are parity-rs's decisions. Since "
        "0.16.0 set_placement and delete_placement go on tenant.store.placement.requests (written only by "
        "parity-rs, by Kafka ACL; the full record with expected_version, which replaces the whole row); there "
        "anything but them and the reads is forbidden, and the admin topic answers them forbidden (rule "
        "placement_via_parity_rs; parity-store's setting placement.admin-writes=allowed keeps admin-console's "
        "labelled temporary bypass until it places through parity-rs). "
        "Keyed by the tenant the command acts on (else by id). reply_to is required and must name a replies.* "
        "topic; a request without a valid id and reply_to is never answered (dead-lettered).",
        {"type": "object", "required": ["schema_version", "cmd", "id", "reply_to"],
         "properties": {"schema_version": {"const": 1}, "cmd": {"type": "string"}, "id": REQUEST_ID,
                        "reply_to": REPLY_TO},
         "oneOf": [{"$ref": f"#/$defs/cmd_{c}"} for c in COMMANDS],
         "unevaluatedProperties": False,
         "$defs": defs})


def replies_schema():
    page = lambda field, items: obj(  # noqa: E731
        [field, "next_after"], {field: {"type": "array", "items": items}, "next_after": {"type": ["string", "null"]}},
        additionalProperties=False)
    results = {
        "result_ping": obj(["pong"], {"pong": {"const": True}}, additionalProperties=False),
        "result_tenant": obj(["tenant"], {"tenant": {"$ref": "#/$defs/tenant"}}, additionalProperties=False),
        "result_tenants": page("tenants", {"$ref": "#/$defs/tenant"}),
        "result_placement": obj(["tenant_id", "placement"], {"tenant_id": TENANT_ID,
                                                             "placement": {"$ref": "#/$defs/placement"}},
                                additionalProperties=False),
        "result_placements": page("placements", obj(["tenant_id", "placement"], {
            "tenant_id": TENANT_ID, "placement": {"$ref": "#/$defs/placement"}}, additionalProperties=False)),
        "result_numbers": dict(page("numbers", PHONE), required=["tenant_id", "numbers", "next_after"],
                               properties={"tenant_id": TENANT_ID, **page("numbers", PHONE)["properties"]}),
        "result_sip_domains": dict(page("sip_domains", DOMAIN), required=["tenant_id", "sip_domains", "next_after"],
                                   properties={"tenant_id": TENANT_ID, **page("sip_domains", DOMAIN)["properties"]}),
        "result_lookup_number": obj(["number", "tenant_id", "parent", "placement"], {
            "number": PHONE, "tenant_id": TENANT_ID, "parent": NULLABLE_TENANT, "placement": NULLABLE_PLACEMENT},
            additionalProperties=False),
        "result_lookup_sip_domain": obj(["domain", "tenant_id", "parent", "placement"], {
            "domain": DOMAIN, "tenant_id": TENANT_ID, "parent": NULLABLE_TENANT, "placement": NULLABLE_PLACEMENT},
            additionalProperties=False),
        "result_digest": obj(["revision", "tenants", "numbers", "sip_domains", "sha256"], {
            "revision": {"type": "integer", "minimum": 0}, "tenants": {"type": "integer", "minimum": 0},
            "numbers": {"type": "integer", "minimum": 0}, "sip_domains": {"type": "integer", "minimum": 0},
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}, additionalProperties=False),
        "result_tenant_change": obj(["tenant", "changed"], {"tenant": {"$ref": "#/$defs/tenant"},
                                                            "changed": {"type": "boolean"}},
                                    additionalProperties=False),
        "result_placement_change": obj(["tenant_id", "placement", "changed"], {
            "tenant_id": TENANT_ID, "placement": NULLABLE_PLACEMENT, "changed": {"type": "boolean"}},
            additionalProperties=False),
        "result_number_change": obj(["tenant_id", "number", "changed"], {
            "tenant_id": TENANT_ID, "number": PHONE, "changed": {"type": "boolean"}}, additionalProperties=False),
        "result_sip_domain_change": obj(["tenant_id", "domain", "changed"], {
            "tenant_id": TENANT_ID, "domain": DOMAIN, "changed": {"type": "boolean"}}, additionalProperties=False),
    }
    return schema_doc(
        "https://parity.example/schemas/store-replies/v1.json",
        "parity-store answers, schema_version 1",
        "On the request's reply_to, keyed like the request. result is one of the result_* shapes in $defs "
        "(CONVENTIONS.md 'parity-store API' names the one per command). message is for people: no numbers, "
        "no other tenant's data. rule names the broken rule of a conflict or bad_request (the tenant maps rule "
        "codes, or version_mismatch, has_sub_tenants, has_numbers, other_tenant).",
        {"type": "object", "required": ["schema_version", "id", "ok"],
         "properties": {
             "schema_version": {"const": 1}, "id": REQUEST_ID, "ok": {"type": "boolean"},
             "result": {"anyOf": [{"$ref": f"#/$defs/{k}"} for k in results]},
             "error": obj(["code", "message"], {"code": {"enum": ERROR_CODES},
                                                "message": {"type": "string", "maxLength": 500},
                                                "rule": {"enum": ERROR_RULES}}, additionalProperties=False)},
         "if": {"properties": {"ok": {"const": True}}},
         "then": {"required": ["result"], "not": {"required": ["error"]}},
         "else": {"required": ["error"], "not": {"required": ["result"]}},
         "unevaluatedProperties": False,
         "$defs": {"placement": PLACEMENT_VIEW, "tenant": TENANT_VIEW, **results}})


def events_schema():
    defs = {"tenant_record": TENANT_RECORD, "placement": PLACEMENT_VIEW}
    for t, (required, props) in EVENTS.items():
        defs["type_" + t.replace(".", "_")] = obj(["type", *required], {"type": {"const": t}, **props})
    return schema_doc(
        "https://parity.example/schemas/store-events/v1.json",
        "tenant.store.events, schema_version 1",
        "Every committed change of the maps, in commit order per tenant. Keyed by tenant_id, headers tenant_id, "
        "schema_version, traceparent. Published through parity-store's outbox: at least once; drop an event_id "
        "or a revision already applied. Numbers here are tenants' numbers (x-pii: phone): operators and "
        "admin-console only.",
        {"type": "object", "required": ["schema_version", "event_id", "type", "at_ms", "tenant_id", "revision"],
         "properties": {"schema_version": {"const": 1}, "event_id": UUID, "type": {"type": "string"},
                        "at_ms": MS, "tenant_id": TENANT_ID, "revision": REVISION},
         "oneOf": [{"$ref": "#/$defs/type_" + t.replace(".", "_")} for t in EVENTS],
         "unevaluatedProperties": False,
         "$defs": defs})


def state_schema():
    base = {"schema_version": {"const": 1}, "revision": REVISION}
    return schema_doc(
        "https://parity.example/schemas/store-state/v1.json",
        "tenant.store.state, schema_version 1",
        "The compacted map every local copy reads from the start (the snapshot) and then follows (the changes). "
        "Keys: tenant:<tenant_id>, number:<e164>, sip_domain:<domain>; the value's kind and id match the key. "
        "A null value is a tombstone: the key is gone. Headers schema_version and revision (on tombstones too, so a "
        "copy knows the revision of a removal), and tenant_id on non-null values.",
        {"oneOf": [{"$ref": "#/$defs/tenant"}, {"$ref": "#/$defs/number"}, {"$ref": "#/$defs/sip_domain"}],
         "$defs": {
             "placement": PLACEMENT_VIEW,
             "tenant": obj(["schema_version", "kind", "tenant_id", "name", "tier", "parent", "version", "placement",
                            "revision"],
                           {**base, "kind": {"const": "tenant"}, "tenant_id": TENANT_ID, "name": NAME, "tier": TIER,
                            "parent": NULLABLE_TENANT, "version": VERSION, "placement": NULLABLE_PLACEMENT},
                           additionalProperties=False),
             "number": obj(["schema_version", "kind", "number", "tenant_id", "revision"],
                           {**base, "kind": {"const": "number"}, "number": PHONE, "tenant_id": TENANT_ID},
                           additionalProperties=False),
             "sip_domain": obj(["schema_version", "kind", "domain", "tenant_id", "revision"],
                               {**base, "kind": {"const": "sip_domain"}, "domain": DOMAIN, "tenant_id": TENANT_ID},
                               additionalProperties=False)}})


# ---- fixtures -------------------------------------------------------------------------------

ACME_PLACEMENT = {"home_zone": "eu1", "failover_zone": "eu2", "zone": "eu1", "cell": "eu1a",
                  "deployment_unit": "bin-a", "media_track": "blue"}
ACME_PLACEMENT_VIEW = dict(ACME_PLACEMENT, version=1, updated_at_ms=T0)
ACME_VIEW = {"tenant_id": "acme", "name": "Acme Ltd", "tier": "small", "parent": None, "sub_tenants": [],
             "version": 1, "updated_at_ms": T0, "placement": ACME_PLACEMENT_VIEW, "numbers": 4, "sip_domains": 1}
ACME_RECORD = {"tenant_id": "acme", "name": "Acme Ltd", "tier": "small", "parent": None, "version": 1,
               "updated_at_ms": T0}
N = "+15550100100"
D = "acme.eu1.platform.example"

ARGS = {
    "ping": {}, "get_tenant": {"tenant_id": "acme"}, "list_tenants": {"limit": 100},
    "get_placement": {"tenant_id": "acme"}, "list_placements": {"after": "acme", "limit": 50},
    "list_numbers": {"tenant_id": "acme"}, "list_sip_domains": {"tenant_id": "acme"},
    "lookup_number": {"number": N}, "lookup_sip_domain": {"domain": D}, "get_digest": {},
    "put_tenant": {"tenant_id": "acme", "name": "Acme Ltd", "tier": "small", "expected_version": 0},
    "delete_tenant": {"tenant_id": "acme", "expected_version": 3},
    "set_placement": {"tenant_id": "acme", "placement": ACME_PLACEMENT, "expected_version": 1},
    "delete_placement": {"tenant_id": "acme"},
    "add_number": {"tenant_id": "acme", "number": N}, "remove_number": {"tenant_id": "acme", "number": N},
    "add_sip_domain": {"tenant_id": "acme", "domain": D}, "remove_sip_domain": {"tenant_id": "acme", "domain": D},
}


PLACEMENT_WRITES = ("set_placement", "delete_placement")


def request(name, topic=None, key="acme", **override):
    cmd = name
    admin = COMMANDS[cmd][0]
    placement = cmd in PLACEMENT_WRITES
    topic = topic or ("tenant.store.placement.requests" if placement else
                      "tenant.store.admin.requests" if admin else "tenant.store.requests")
    rs = topic == "tenant.store.placement.requests"
    value = {"schema_version": 1, "cmd": cmd,
             "id": f"rs:0193a1b2-c3d4-7e5f-9a6b-7c8d9e0f1a2c:{cmd}:1" if rs else f"admin-api-1:{cmd}-0001",
             "reply_to": "replies.parity-rs.eu1a" if rs else "replies.admin-console", **ARGS[cmd]}
    for k, v in override.items():
        if v is None:
            value.pop(k, None)
        else:
            value[k] = v
    return {"topic": topic, "key": key, "headers": {"traceparent": TRACE}, "value": value}


def reply(result=None, error=None):
    v = {"schema_version": 1, "id": "admin-api-1:get_tenant-0001", "ok": error is None}
    if error is None:
        v["result"] = result
    else:
        v["error"] = error
    return {"topic": "replies.admin-console", "key": "acme", "headers": {"schema_version": "1", "traceparent": TRACE},
            "value": v}


def event(etype, revision=7, **body):
    v = {"schema_version": 1, "event_id": "0192f6a1-e000-7aaa-8bbb-000000000001", "type": etype, "at_ms": T0,
         "tenant_id": "acme", "revision": revision, **body}
    return {"topic": "tenant.store.events", "key": "acme",
            "headers": {"tenant_id": "acme", "schema_version": "1", "traceparent": TRACE}, "value": v}


def state(key, value, revision=9):
    headers = {"schema_version": "1", "revision": str(value["revision"] if value else revision)}
    if value is not None:
        headers["tenant_id"] = value["tenant_id"]
    return {"topic": "tenant.store.state", "key": key, "headers": headers, "value": value}


STATE_TENANT = {"schema_version": 1, "kind": "tenant", "tenant_id": "acme", "name": "Acme Ltd", "tier": "small",
                "parent": None, "version": 1, "placement": ACME_PLACEMENT_VIEW, "revision": 7}
STATE_NUMBER = {"schema_version": 1, "kind": "number", "number": N, "tenant_id": "acme", "revision": 8}
STATE_DOMAIN = {"schema_version": 1, "kind": "sip_domain", "domain": D, "tenant_id": "acme", "revision": 9}


def fixtures():
    valid, invalid = {}, {}
    for cmd in COMMANDS:
        valid[f"request-{cmd.replace('_', '-')}"] = (f"{cmd}", request(cmd, key="acme" if "tenant_id" in ARGS[cmd]
                                                                           else f"admin-api-1:{cmd}-0001"), "apply")
    valid["request-admin-topic-read"] = ("A read command on the admin topic is answered too",
                                         request("get_tenant", topic="tenant.store.admin.requests"), "apply")
    valid["request-sub-tenant"] = ("put_tenant of a sub-tenant: no tier (it takes its parent's)",
                                   dict(request("put_tenant", key="initech-de"),
                                        value={"schema_version": 1, "cmd": "put_tenant", "id": "admin-api-1:p-2",
                                               "reply_to": "replies.admin-console", "tenant_id": "initech-de",
                                               "name": "Initech Germany", "parent": "initech"}), "apply")
    valid["request-placement-topic-read"] = ("A read on parity-rs's placement topic is answered too",
                                             request("get_placement", topic="tenant.store.placement.requests"),
                                             "apply")
    invalid["request-placement-on-admin-topic"] = ("set_placement on the admin topic (0.16.0): forbidden, rule "
                                                   "placement_via_parity_rs; placements are parity-rs's",
                                                   request("set_placement", topic="tenant.store.admin.requests"),
                                                   ["placement_via_parity_rs"], "reply_error:forbidden")
    invalid["request-tenant-write-on-placement-topic"] = ("A tenant write on parity-rs's placement topic: "
                                                          "forbidden, nothing changes",
                                                          request("put_tenant",
                                                                  topic="tenant.store.placement.requests"),
                                                          ["placement_only"], "reply_error:forbidden")
    invalid["request-write-on-read-topic"] = ("A write on tenant.store.requests: forbidden, nothing changes",
                                              request("add_number", topic="tenant.store.requests"),
                                              ["admin_only"], "reply_error:forbidden")
    invalid["request-no-reply-to"] = ("No reply_to: never answered", request("get_tenant", reply_to=None),
                                      ["schema", "bad_reply_to"], "dlt")
    invalid["request-reply-to-events"] = ("A reply_to that isn't replies.*: never answered (no injection into "
                                          "the events topic)", request("get_tenant", reply_to="tenant.store.events"),
                                          ["schema", "bad_reply_to"], "dlt")
    invalid["request-no-id"] = ("No id: never answered", request("ping", id=None), ["schema", "bad_id"], "dlt")
    invalid["request-not-e164"] = ("A number that isn't E.164: bad_request",
                                   request("add_number", number="0049301234"), ["schema"], "reply_error:bad_request")
    invalid["request-unknown-cmd"] = ("An unknown command: bad_request", request("ping", cmd="move_tenant"),
                                      ["schema"], "reply_error:bad_request")
    invalid["request-unknown-field"] = ("An unknown field: bad_request", request("get_tenant", tenant="acme"),
                                        ["schema"], "reply_error:bad_request")
    invalid["request-nul-tenant"] = ("A NUL in tenant_id (Postgres would refuse it): bad_request, never retried",
                                     request("get_tenant", tenant_id="ac\u0000me"), ["schema"],
                                     "reply_error:bad_request")
    invalid["request-version-2"] = ("An unknown major: bad_request", request("ping", schema_version=2),
                                    ["schema"], "reply_error:bad_request")
    invalid["request-not-json"] = ("A body that isn't JSON: never answered",
                                   {"topic": "tenant.store.requests", "key": "acme", "headers": {}},
                                   ["schema", "bad_id", "bad_reply_to"], "dlt")

    valid["reply-tenant"] = ("get_tenant's answer", reply({"tenant": ACME_VIEW}), "apply")
    valid["reply-lookup-number"] = ("lookup_number's answer", reply(
        {"number": N, "tenant_id": "acme", "parent": None, "placement": ACME_PLACEMENT_VIEW}), "apply")
    valid["reply-digest"] = ("get_digest's answer", reply(
        {"revision": 42, "tenants": 5, "numbers": 14, "sip_domains": 4, "sha256": "0" * 64}), "apply")
    valid["reply-numbers-page"] = ("list_numbers' answer", reply(
        {"tenant_id": "acme", "numbers": [N, "+15550100200"], "next_after": "+15550100200"}), "apply")
    valid["reply-conflict"] = ("A version conflict", reply(error={
        "code": "conflict", "message": "the tenant's version is 4, not 3", "rule": "version_mismatch"}), "apply")
    valid["reply-forbidden"] = ("A write on the read topic", reply(error={
        "code": "forbidden", "message": "write commands are accepted on tenant.store.admin.requests only"}), "apply")
    invalid["reply-result-and-error"] = ("result and error together", reply(
        {"pong": True}, None) | {"value": {"schema_version": 1, "id": "x", "ok": True, "result": {"pong": True},
                                           "error": {"code": "internal", "message": "x"}}}, ["schema"], "apply")

    valid["event-tenant-changed"] = ("A new tenant", event("tenant.changed", tenant=ACME_RECORD), "apply")
    valid["event-tenant-renamed"] = ("A renamed tenant, with the previous record", event(
        "tenant.changed", tenant=dict(ACME_RECORD, name="Acme Group", version=2), previous=ACME_RECORD), "apply")
    valid["event-tenant-deleted"] = ("A deleted tenant", event("tenant.deleted", tenant=ACME_RECORD), "apply")
    valid["event-placement-changed"] = ("A bin move", event(
        "placement.changed", placement=dict(ACME_PLACEMENT_VIEW, deployment_unit="bin-b", version=2),
        previous=ACME_PLACEMENT_VIEW), "apply")
    valid["event-placement-deleted"] = ("A placement removed", event("placement.deleted",
                                                                     placement=ACME_PLACEMENT_VIEW), "apply")
    valid["event-number-added"] = ("A number added", event("number.added", number=N), "apply")
    valid["event-number-removed"] = ("A number removed", event("number.removed", number=N), "apply")
    valid["event-sip-domain-added"] = ("A SIP domain added", event("sip_domain.added", domain=D), "apply")
    valid["event-sip-domain-removed"] = ("A SIP domain removed", event("sip_domain.removed", domain=D), "apply")
    e = event("number.added", number=N)
    e["key"] = "globex"
    invalid["event-key-not-tenant"] = ("An event keyed by another tenant", e, ["key_mismatch"], "reject")
    e = event("number.added", number=N)
    e["headers"]["tenant_id"] = "globex"
    invalid["event-header-body-mismatch"] = ("The tenant header names another tenant", e,
                                             ["header_body_mismatch"], "reject")
    e = event("number.added", number=N)
    del e["headers"]["tenant_id"]
    invalid["event-missing-tenant-header"] = ("No tenant_id header", e, ["missing_header"], "reject")
    invalid["event-no-revision"] = ("No revision", event("number.added", number=N) | {"value": {
        k: v for k, v in event("number.added", number=N)["value"].items() if k != "revision"}}, ["schema"], "reject")

    valid["state-tenant"] = ("A tenant with its placement", state("tenant:acme", STATE_TENANT), "apply")
    valid["state-parent"] = ("A parent: no placement", state("tenant:initech", dict(
        STATE_TENANT, tenant_id="initech", name="Initech Group", tier="enterprise", placement=None)), "apply")
    valid["state-number"] = ("A number's owner", state("number:" + N, STATE_NUMBER), "apply")
    valid["state-sip-domain"] = ("A SIP domain's owner", state("sip_domain:" + D, STATE_DOMAIN), "apply")
    valid["state-number-tombstone"] = ("A removed number: a tombstone", state("number:" + N, None), "apply")
    valid["state-tenant-tombstone"] = ("A deleted tenant: a tombstone", state("tenant:acme", None), "apply")
    invalid["state-key-mismatch"] = ("The key names another number", state("number:+15550100200", STATE_NUMBER),
                                     ["key_mismatch"], "reject")
    invalid["state-bad-key"] = ("A key of no known kind", state("tenant_number:" + N, None), ["key_mismatch"],
                                "reject")
    invalid["state-unknown-kind"] = ("An unknown kind", state("queue:sales", dict(STATE_NUMBER, kind="queue")),
                                     ["schema", "key_mismatch"], "reject")
    return valid, invalid


def write(rel, doc):
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def main():
    write("schemas/store/store-requests.v1.json", requests_schema())
    write("schemas/store/store-replies.v1.json", replies_schema())
    write("schemas/store/store-events.v1.json", events_schema())
    write("schemas/store/store-state.v1.json", state_schema())
    out = ROOT / "fixtures" / "store.v1"
    if out.exists():
        shutil.rmtree(out)
    valid, invalid = fixtures()
    for name, (description, record, reader) in valid.items():
        write(f"fixtures/store.v1/valid/{name}.json", {"description": description, "kind": "store",
                                                        "writer_valid": True, "reader": reader, "record": record})
    for name, (description, record, violates, reader) in invalid.items():
        write(f"fixtures/store.v1/invalid/{name}.json", {"description": description, "kind": "store",
                                                          "writer_valid": False, "violates": violates,
                                                          "reader": reader, "record": record})
    print(f"wrote 4 schemas, {len(valid)} valid and {len(invalid)} invalid store fixtures")


if __name__ == "__main__":
    main()
