#!/usr/bin/env python3
"""Writes the tenant maps fixtures (0.4.0): fixtures/tenant-maps.v1/{valid,invalid}/*.json,
kind "tenant_maps". Each wraps a whole maps file; an invalid one declares exactly the rules it
breaks (CONVENTIONS.md "Tenant maps") and, with `at`, a JSON pointer one of them must name.
contract-check and parity-store's tests run every one.
Run from anywhere: python3 -I scripts/tenant_maps_fixtures.py. Output is deterministic.
"""
import copy
import json
import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "fixtures" / "tenant-maps.v1"


def placement(cell="eu1a", unit="bin-a", **extra):
    p = {"home_zone": "eu1", "failover_zone": "eu2", "zone": "eu1", "cell": cell,
         "deployment_unit": unit, "media_track": "blue"}
    p.update(extra)
    return p


def tenant(tid, tier="small", numbers=(), domains=None, **extra):
    t = {"id": tid, "name": tid.capitalize() + " Ltd", "tier": tier, "placement": placement()}
    t["numbers"] = list(numbers)
    t["sip_domains"] = [f"{tid}.eu1.platform.example"] if domains is None else list(domains)
    t.update(extra)
    return {k: v for k, v in t.items() if v is not None}


def maps(*tenants, test=None):
    doc = {"schema_version": 1, "tenants": list(tenants)}
    if test is not None:
        doc["test"] = test
    return doc


ACME = tenant("acme", numbers=["+15550100100"])
GLOBEX = tenant("globex", tier="mid", numbers=["+15550101100"])
PARENT = {"id": "initech", "name": "Initech Group", "tier": "enterprise"}


def sub(tid, parent="initech", numbers=("+15550102100",), **extra):
    t = {"id": tid, "name": tid, "parent": parent, "placement": placement(),
         "numbers": list(numbers), "sip_domains": [f"{tid}.eu1.platform.example"]}
    t.update(extra)
    return {k: v for k, v in t.items() if v is not None}


def readme_example():
    """parity-store README's example: a production file (real-looking numbers, no test section)."""
    return maps(
        tenant("acme", numbers=["+493012345678", "+493012345679"]),
        dict(tenant("globex", tier="mid", numbers=["+441234567890"]),
             placement=placement(cell="eu1b", unit="bin-b", media_track="green")),
        PARENT,
        sub("initech-de", numbers=["+4989555000"]),
        dict(sub("initech-us", numbers=["+12125550100"], sip_domains=["initech-us.us1.platform.example"]),
             placement={"home_zone": "us1", "failover_zone": "us2", "zone": "us1", "cell": "us1a",
                        "deployment_unit": "bin-a", "media_track": "blue"}),
    )


def without(t, *keys):
    """A copy of a tenant without some fields."""
    return {k: v for k, v in t.items() if k not in keys}


def with_(doc, path, value):
    """A copy of doc with value set at a list of keys/indexes (None deletes)."""
    doc = copy.deepcopy(doc)
    node = doc
    for k in path[:-1]:
        node = node[k]
    if value is None:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return doc


BASE = maps(ACME, GLOBEX, PARENT, sub("initech-de"), sub("initech-uk", numbers=["+15550103100"]))
TEST = {"agents": {"acme": ["agent-a1"], "globex": ["agent-g1"], "initech-de": ["agent-i1"]}}
BASE_TEST = dict(BASE, test=TEST)

VALID = {
    "readme-example": ("parity-store README's example (a production file)", readme_example()),
    "empty": ("No tenants: a new platform", maps()),
    "small-mid-enterprise": ("A small, a mid and an enterprise parent with two sub-tenants", BASE),
    "test-file": ("A test file: fictional numbers, reserved domains, agent ids", BASE_TEST),
    "enterprise-own-cell": ("An enterprise tenant without sub-tenants has its own placement",
                            maps(tenant("hooli", tier="enterprise", numbers=["+15550104100"]))),
    "sub-tenant-repeats-tier": ("A sub-tenant may repeat its parent's tier",
                                maps(PARENT, sub("initech-de", tier="enterprise"))),
    "optional-placement-fields": (
        "failover_zone and media_track may be left out; numbers and sip_domains too",
        maps({"id": "acme", "name": "Acme Ltd", "tier": "small",
              "placement": {"home_zone": "eu1", "zone": "eu1", "cell": "eu1a", "deployment_unit": "bin-a"}})),
    "ofcom-drama-numbers": ("A test file may use Ofcom's drama range +44 7700 900xxx",
                            maps(tenant("acme", numbers=["+447700900123"]), test={"agents": {"acme": ["a1"]}})),
}

# name: (description, file, violates, at)
INVALID = {
    "schema-not-e164": ("A number without its + is not E.164",
                        with_(BASE, ["tenants", 0, "numbers", 0], "15550100100"),
                        ["schema"], "/tenants/0/numbers/0"),
    "schema-number-too-long": ("E.164 has at most 15 digits",
                               with_(BASE, ["tenants", 0, "numbers", 0], "+1555010010012345"),
                               ["schema"], "/tenants/0/numbers/0"),
    "schema-bad-tenant-id": ("A tenant id is a DNS label: lowercase, no underscore",
                             with_(BASE, ["tenants", 0, "id"], "Acme_Ltd"), ["schema"], "/tenants/0/id"),
    "schema-unknown-field": ("An unknown tenant field is refused",
                             with_(BASE, ["tenants", 0, "region"], "eu"), ["schema"], "/tenants/0"),
    "schema-version-2": ("An unknown major", with_(BASE, ["schema_version"], 2), ["schema"], "/schema_version"),
    "schema-placement-without-cell": ("A placement names its cell",
                                      with_(BASE, ["tenants", 0, "placement", "cell"], None),
                                      ["schema"], "/tenants/0/placement"),
    "schema-uppercase-domain": ("SIP domains are lowercase",
                                with_(BASE, ["tenants", 0, "sip_domains", 0], "Acme.eu1.platform.example"),
                                ["schema"], "/tenants/0/sip_domains/0"),
    "schema-unknown-tier": ("Tiers are small, mid and enterprise",
                            with_(BASE, ["tenants", 0, "tier"], "huge"), ["schema"], "/tenants/0/tier"),
    "schema-name-control-character": ("A name has no control characters (log safety)",
                                      with_(BASE, ["tenants", 0, "name"], "Acme\nLtd"), ["schema"], "/tenants/0/name"),
    "duplicate-tenant": ("Two tenants with one id",
                         maps(ACME, dict(GLOBEX, id="acme", sip_domains=["acme2.eu1.platform.example"])),
                         ["duplicate_tenant"], "/tenants/1/id"),
    "missing-tier": ("A tenant without a parent states its tier",
                     with_(BASE, ["tenants", 0, "tier"], None), ["missing_tier"], "/tenants/0"),
    "unknown-parent": ("A parent that doesn't exist",
                       maps(sub("initech-de", parent="initrode")), ["unknown_parent"], "/tenants/0/parent"),
    "parent-not-enterprise": ("Only an enterprise tenant can be a parent",
                              maps(without(ACME, "placement", "numbers", "sip_domains"), sub("acme-sales", parent="acme")),
                              ["parent_not_enterprise"], "/tenants/1/parent"),
    "nested-sub-tenant": ("One level only: a sub-tenant can't have sub-tenants",
                          maps(PARENT, without(sub("initech-de"), "placement", "numbers", "sip_domains"),
                               sub("initech-de-munich", parent="initech-de", numbers=["+15550102900"])),
                          ["nested_sub_tenant"], "/tenants/2/parent"),
    "tier-mismatch": ("A sub-tenant has its parent's tier",
                      maps(PARENT, sub("initech-de", tier="mid")), ["tier_mismatch"], "/tenants/1/tier"),
    "parent-with-placement": ("A parent takes no traffic: no placement",
                              maps(dict(PARENT, placement=placement()), sub("initech-de")),
                              ["parent_takes_traffic"], "/tenants/0"),
    "parent-with-numbers": ("A parent takes no traffic: no numbers",
                            maps(dict(PARENT, numbers=["+15550102999"]), sub("initech-de")),
                            ["parent_takes_traffic"], "/tenants/0"),
    "missing-placement": ("A tenant that takes traffic has a placement",
                          with_(BASE, ["tenants", 1, "placement"], None), ["missing_placement"], "/tenants/1"),
    "enterprise-without-sub-tenants-or-placement": (
        "An enterprise tenant with no sub-tenants is served itself, so it needs a placement",
        maps(PARENT), ["missing_placement"], "/tenants/0"),
    "duplicate-number-two-tenants": ("A number belongs to one tenant",
                                     with_(BASE, ["tenants", 1, "numbers", 0], "+15550100100"),
                                     ["duplicate_number"], "/tenants/1/numbers/0"),
    "duplicate-number-one-tenant": ("A number is listed once",
                                    with_(BASE, ["tenants", 0, "numbers"], ["+15550100100", "+15550100100"]),
                                    ["duplicate_number"], "/tenants/0/numbers/1"),
    "duplicate-sip-domain": ("A SIP domain belongs to one tenant",
                             with_(BASE, ["tenants", 1, "sip_domains", 0], "acme.eu1.platform.example"),
                             ["duplicate_sip_domain"], "/tenants/1/sip_domains/0"),
    "test-agents-unknown-tenant": ("Test agents for a tenant that isn't in the file",
                                   with_(BASE_TEST, ["test", "agents", "initrode"], ["agent-x1"]),
                                   ["test_unknown_tenant"], "/test/agents/initrode"),
    "test-agents-for-a-parent": ("A parent has no agents of its own: they belong to its sub-tenants",
                                 with_(BASE_TEST, ["test", "agents", "initech"], ["agent-x1"]),
                                 ["test_unknown_tenant"], "/test/agents/initech"),
    "duplicate-agent": ("An agent id appears once in a test file (every occurrence is reported)",
                        with_(BASE_TEST, ["test", "agents", "globex"], ["agent-a1"]),
                        ["duplicate_agent"], "/test/agents/globex/0"),
    "test-number-not-fictional": ("A test file never holds a dialable number",
                                  with_(BASE_TEST, ["tenants", 0, "numbers", 0], "+493012345678"),
                                  ["test_number_not_fictional"], "/tenants/0/numbers/0"),
    "test-domain-not-reserved": ("A test file's SIP domains are under .example, .test, .invalid or .localhost",
                                 with_(BASE_TEST, ["tenants", 0, "sip_domains", 0], "acme.example.com"),
                                 ["test_domain_not_reserved"], "/tenants/0/sip_domains/0"),
    "several-rules": ("Every broken rule is reported, not only the first",
                      maps(ACME, dict(ACME, sip_domains=["acme.eu1.platform.example"]), sub("initech-de", parent="initrode")),
                      ["duplicate_tenant", "duplicate_number", "duplicate_sip_domain", "unknown_parent"], None),
}


def write(rel, doc):
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    for name, (description, file) in VALID.items():
        write(f"valid/{name}.json", {"description": description, "kind": "tenant_maps",
                                     "writer_valid": True, "file": file})
    for name, (description, file, violates, at) in INVALID.items():
        doc = {"description": description, "kind": "tenant_maps", "writer_valid": False,
               "violates": violates}
        if at is not None:
            doc["at"] = at
        doc["file"] = file
        write(f"invalid/{name}.json", doc)
    print(f"wrote {len(VALID)} valid and {len(INVALID)} invalid tenant maps fixtures to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
