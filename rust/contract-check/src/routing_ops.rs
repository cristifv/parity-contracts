//! parity-rs's tenant routing contracts (0.16.0): the `routing_ops` fixtures against
//! `schemas/tenant-routing/`, the cross-field rules of CONVENTIONS.md "Tenant routing API" (the
//! topic decides what a command may do: `x-topics`), the reader action they call for, coverage,
//! and the shared test topology (`tenants/test-topology.v1.json`) against the test tenants.

use crate::rules::{is_dns_label, is_reply_topic};
use crate::{Report, Schemas, read_json};
use serde_json::{Map, Value};
use std::collections::BTreeSet;
use std::path::Path;

/// Every rule code a `routing_ops` fixture can declare.
pub const RULE_CODES: [&str; 7] = [
    "schema",
    "bad_id",
    "bad_reply_to",
    "wrong_topic",
    "key_mismatch",
    "header_body_mismatch",
    "missing_header",
];

pub const TOPOLOGY_FILE: &str = "tenants/test-topology.v1.json";
const TENANTS_FILE: &str = "tenants/test-tenants.v1.json";

/// The kind of a tenant routing topic (`requests`, `ops`, `onboarding`, `peer`, `events`,
/// `handover`) and its zone, for `tenant.routing.<kind>.<zone>`.
pub fn topic_kind(topic: &str) -> Option<(&'static str, &str)> {
    let rest = topic.strip_prefix("tenant.routing.")?;
    for (prefix, kind) in [
        ("ops.requests.", "ops"),
        ("onboarding.requests.", "onboarding"),
        ("peer.requests.", "peer"),
        ("requests.", "requests"),
        ("events.", "events"),
        ("handover.", "handover"),
    ] {
        if let Some(zone) = rest.strip_prefix(prefix)
            && is_name(zone)
        {
            return Some((kind, zone));
        }
    }
    None
}

/// A region, zone, unit or track name: `^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$`.
pub fn is_name(s: &str) -> bool {
    let b = s.as_bytes();
    (1..=32).contains(&b.len())
        && b.iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'-')
        && b[0] != b'-'
        && b[b.len() - 1] != b'-'
}

/// The schema of a topic this module owns.
pub fn schema_for(topic: &str) -> Option<&'static str> {
    match topic_kind(topic) {
        Some(("events", _)) => Some("routing-events"),
        Some(("handover", _)) => Some("routing-handover"),
        Some(_) => Some("routing-requests"),
        None if topic.starts_with("replies.") => Some("routing-replies"),
        None => None,
    }
}

/// `^[A-Za-z0-9._:-]{1,200}$`
fn is_request_id(s: &str) -> bool {
    (1..=200).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
}

/// The topics a command is allowed on (`x-topics`), if the schema defines the command.
pub fn allowed_topics(schemas: &Schemas, cmd: &str) -> Option<Vec<String>> {
    schemas.docs["routing-requests"]["$defs"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(_, d)| d)
        .find(|d| d["properties"]["cmd"]["const"].as_str() == Some(cmd))
        .map(|d| {
            d["x-topics"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
}

/// The key an event must have, from its type's `x-key` (`tenant`, `saga`, `unit`).
fn event_key(schemas: &Schemas, v: &Value) -> Option<String> {
    let t = v["type"].as_str()?;
    let def = schemas.docs["routing-events"]["$defs"]
        .as_object()?
        .values()
        .find(|d| d["properties"]["type"]["const"].as_str() == Some(t))?;
    match def["x-key"].as_str()? {
        "tenant" => v["tenant_id"].as_str().map(str::to_owned),
        "saga" => v["saga_id"].as_str().map(str::to_owned),
        "unit" => v["unit"]["unit"].as_str().map(|u| format!("unit:{u}")),
        _ => None,
    }
}

/// The rules a tenant routing record breaks.
pub fn violations(
    schemas: &Schemas,
    topic: &str,
    key: &str,
    headers: &Map<String, Value>,
    value: Option<&Value>,
) -> BTreeSet<&'static str> {
    let mut out = BTreeSet::new();
    let Some(schema) = schema_for(topic) else {
        out.insert("schema");
        return out;
    };
    if value.is_none_or(|v| !schemas.errors(schema, v).is_empty()) {
        out.insert("schema");
    }
    let field = |name: &str| value.and_then(|v| v.get(name)).and_then(Value::as_str);
    let header = |n: &str| headers.get(n).and_then(Value::as_str);
    let header_matches = |out: &mut BTreeSet<&'static str>| {
        if let (Some(h), Some(b)) = (header("tenant_id"), field("tenant_id"))
            && h != b
        {
            out.insert("header_body_mismatch");
        }
    };
    match schema {
        "routing-requests" => {
            if !field("id").is_some_and(is_request_id) {
                out.insert("bad_id");
            }
            if !field("reply_to").is_some_and(is_reply_topic) {
                out.insert("bad_reply_to");
            }
            header_matches(&mut out);
            let kind = topic_kind(topic).map(|(k, _)| k).unwrap_or_default();
            if let Some(cmd) = field("cmd")
                && let Some(topics) = allowed_topics(schemas, cmd)
            {
                let mut ok = topics.iter().any(|t| t == kind);
                // The onboarding topic places from a hint only: an operator's target is the ops topic's.
                if cmd == "place_tenant"
                    && kind == "onboarding"
                    && value.is_some_and(|v| v.get("target").is_some())
                {
                    ok = false;
                }
                if !ok {
                    out.insert("wrong_topic");
                }
            }
        }
        "routing-events" | "routing-handover" => {
            if header("schema_version").is_none()
                || (field("tenant_id").is_some() && !header("tenant_id").is_some_and(is_dns_label))
            {
                out.insert("missing_header");
            }
            header_matches(&mut out);
            let want = if schema == "routing-handover" {
                field("tenant_id").map(str::to_owned)
            } else {
                value.and_then(|v| event_key(schemas, v))
            };
            if want.is_some_and(|w| w != key) {
                out.insert("key_mismatch");
            }
        }
        _ => {}
    }
    out
}

/// The reader action CONVENTIONS.md requires, derived from the broken rules.
pub fn expected_reader(
    topic: &str,
    broken: &BTreeSet<&str>,
    value: Option<&Value>,
) -> &'static str {
    match schema_for(topic) {
        Some("routing-requests") => {
            if value.is_none() || broken.contains("bad_id") || broken.contains("bad_reply_to") {
                "dlt"
            } else if broken.contains("wrong_topic") {
                "reply_error:forbidden"
            } else if !broken.is_empty() {
                "reply_error:bad_request"
            } else {
                "apply"
            }
        }
        Some("routing-replies")
            if value
                .and_then(|v| v.get("id"))
                .is_some_and(Value::is_string) =>
        {
            "apply"
        }
        Some("routing-replies") => "reject",
        _ if broken.is_empty() => "apply",
        _ => "reject",
    }
}

/// A `routing_ops` fixture: it breaks exactly the rules it declares, and its reader action follows.
pub(crate) fn check_fixture(schemas: &Schemas, file: &Path, f: &Value, report: &mut Report) {
    let rec = &f["record"];
    let topic = rec["topic"].as_str().unwrap_or_default();
    let empty = Map::new();
    let headers = rec["headers"].as_object().unwrap_or(&empty);
    let value = rec.get("value");
    let found = violations(
        schemas,
        topic,
        rec["key"].as_str().unwrap_or_default(),
        headers,
        value,
    );
    let declared: BTreeSet<&str> = f["violates"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect();
    if found != declared {
        let detail = schema_for(topic)
            .and_then(|s| value.map(|v| schemas.errors(s, v)))
            .unwrap_or_default();
        report.fail(
            file,
            format!(
                "breaks {found:?}, declares {declared:?} ({})",
                detail.join("; ")
            ),
        );
    }
    let expected = expected_reader(topic, &found, value);
    if f["reader"].as_str() != Some(expected) {
        report.fail(
            file,
            format!(
                "reader action {:?}, but the rules it breaks call for {expected:?}",
                f["reader"]
            ),
        );
    }
    report
        .routing_codes
        .extend(declared.iter().map(|c| (*c).to_owned()));
    if f["writer_valid"] == true
        && let Some(v) = value
    {
        let name = match schema_for(topic) {
            Some("routing-requests") => v["cmd"].as_str().map(|c| format!("cmd {c}")),
            Some("routing-events") => v["type"].as_str().map(|t| format!("event {t}")),
            Some("routing-handover") => v["state"].as_str().map(|s| format!("handover {s}")),
            _ => None,
        };
        report.routing_seen.extend(name);
    }
}

/// Every command, event type and handover state has a valid fixture; every rule an invalid one;
/// every command names its topics.
pub(crate) fn coverage(schemas: &Schemas, report: &mut Report) {
    let mut want: Vec<String> = Vec::new();
    for (schema, field, prefix) in [
        ("routing-requests", "cmd", "cmd"),
        ("routing-events", "type", "event"),
    ] {
        for d in schemas.docs[schema]["$defs"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(_, d)| d)
        {
            if let Some(c) = d["properties"][field]["const"].as_str() {
                want.push(format!("{prefix} {c}"));
                if schema == "routing-requests"
                    && d["x-topics"].as_array().is_none_or(|a| a.is_empty())
                {
                    report
                        .errors
                        .push(format!("routing-requests: {c} has no x-topics"));
                }
            }
        }
    }
    want.extend(["handover released".into(), "handover adopted".into()]);
    for w in want {
        if !report.routing_seen.contains(&w) {
            report
                .errors
                .push(format!("coverage: no valid routing_ops fixture for {w}"));
        }
    }
    for code in RULE_CODES {
        if !report.routing_codes.contains(code) {
            report
                .errors
                .push(format!("coverage: no routing_ops fixture for rule {code}"));
        }
    }
}

/// The test topology: valid against topology.v1, its cross-field rules hold, and every placed test
/// tenant resolves in it (records before 2026-11-03 hold the region in zone/home_zone/failover_zone
/// and the zone in cell; newer ones hold zones everywhere).
pub(crate) fn check_test_topology(root: &Path, schemas: &Schemas, report: &mut Report) {
    let file = root.join(TOPOLOGY_FILE);
    let topo = match read_json(&file) {
        Ok(v) => v,
        Err(e) => return report.fail(&file, format!("{e:#}")),
    };
    let errs = schemas.errors("routing-topology", &topo);
    if !errs.is_empty() {
        return report.fail(&file, format!("fails topology.v1: {}", errs.join("; ")));
    }
    let mut problems = topology_problems(&topo);
    if let Ok(tenants) = read_json(&root.join(TENANTS_FILE)) {
        for t in tenants["tenants"].as_array().into_iter().flatten() {
            let id = t["tenant_id"].as_str().unwrap_or("?");
            if let Some(p) = t.get("placement").filter(|p| p.is_object())
                && let Err(e) = resolve(&topo, p)
            {
                problems.push(format!("test tenant {id}: {e}"));
            }
        }
    }
    for p in problems {
        report.fail(&file, p);
    }
    *report.checked.entry("test topology".into()).or_default() += 1;
}

/// The cross-field rules of topology.v1.
pub fn topology_problems(topo: &Value) -> Vec<String> {
    let mut out = Vec::new();
    let empty = Map::new();
    let zones = topo["zones"].as_object().unwrap_or(&empty);
    let regions = topo["regions"].as_object().unwrap_or(&empty);
    for (name, r) in regions {
        let d = r["default_zone"].as_str().unwrap_or_default();
        if zones.get(d).and_then(|z| z["region"].as_str()) != Some(name) {
            out.push(format!(
                "region {name}: default_zone {d} is not one of its zones"
            ));
        }
    }
    for (name, z) in zones {
        let region = z["region"].as_str().unwrap_or_default();
        let Some(r) = regions.get(region) else {
            out.push(format!("zone {name}: unknown region {region}"));
            continue;
        };
        if let Some(f) = z["failover"].as_str() {
            let same = zones
                .get(f)
                .and_then(|fz| regions.get(fz["region"].as_str().unwrap_or_default()))
                .is_some_and(|fr| fr["residency"] == r["residency"]);
            if f == name || !same {
                out.push(format!(
                    "zone {name}: failover {f} is not another zone of the same residency"
                ));
            }
        }
        let track = z["default_media_track"].as_str().unwrap_or_default();
        if !z["media_tracks"]
            .as_array()
            .is_some_and(|a| a.iter().any(|t| t == track))
        {
            out.push(format!(
                "zone {name}: default_media_track {track} is not in media_tracks"
            ));
        }
    }
    out
}

/// The zone a placement is served from, after checking its unit, track, home and failover zone.
pub fn resolve(topo: &Value, p: &Value) -> Result<String, String> {
    let zones = &topo["zones"];
    let regions = &topo["regions"];
    let s = |k: &str| p[k].as_str();
    let zone = [s("cell"), s("zone")]
        .into_iter()
        .flatten()
        .find(|z| zones.get(*z).is_some())
        .ok_or("neither cell nor zone names a zone")?
        .to_owned();
    let z = &zones[&zone];
    let unit = s("deployment_unit").unwrap_or_default();
    if z["units"].get(unit).is_none() {
        return Err(format!("unit {unit} is not a unit of zone {zone}"));
    }
    if let Some(t) = s("media_track")
        && !z["media_tracks"]
            .as_array()
            .is_some_and(|a| a.iter().any(|x| x == t))
    {
        return Err(format!("media track {t} is not a track of zone {zone}"));
    }
    let region = z["region"].as_str().unwrap_or_default();
    // home_zone and zone: the zone itself, or (an older record) its region.
    for k in ["home_zone", "zone"] {
        if let Some(v) = s(k)
            && v != zone
            && v != region
        {
            return Err(format!(
                "{k} {v} is neither zone {zone} nor its region {region}"
            ));
        }
    }
    if let Some(f) = s("failover_zone") {
        let fz = if zones.get(f).is_some() {
            Some(f.to_owned())
        } else {
            regions
                .get(f)
                .and_then(|r| r["default_zone"].as_str())
                .map(str::to_owned)
        };
        match fz {
            Some(fz) if fz != zone => {}
            _ => return Err(format!("failover_zone {f} names no other zone")),
        }
    }
    Ok(zone)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn topics() {
        assert_eq!(
            topic_kind("tenant.routing.ops.requests.eu1a"),
            Some(("ops", "eu1a"))
        );
        assert_eq!(
            topic_kind("tenant.routing.requests.eu1a"),
            Some(("requests", "eu1a"))
        );
        assert_eq!(topic_kind("tenant.routing.requests"), None);
        assert_eq!(topic_kind("tenant.routing.requests.EU1A"), None);
        assert_eq!(
            schema_for("replies.parity-cc-tools"),
            Some("routing-replies")
        );
    }

    #[test]
    fn resolves_old_and_new_records() {
        let topo = json!({"regions": {"eu1": {"residency": "eu", "default_zone": "eu1a"},
                                      "eu2": {"residency": "eu", "default_zone": "eu2a"}},
                          "zones": {"eu1a": {"region": "eu1", "media_tracks": ["blue"], "units": {"bin-a": {}}},
                                    "eu2a": {"region": "eu2", "media_tracks": ["blue"], "units": {"bin-a": {}}}}});
        let old = json!({"home_zone": "eu1", "failover_zone": "eu2", "zone": "eu1", "cell": "eu1a",
                         "deployment_unit": "bin-a", "media_track": "blue"});
        assert_eq!(resolve(&topo, &old), Ok("eu1a".into()));
        let new = json!({"home_zone": "eu1a", "failover_zone": "eu2a", "zone": "eu1a", "cell": "eu1a",
                         "deployment_unit": "bin-a"});
        assert_eq!(resolve(&topo, &new), Ok("eu1a".into()));
        let bad = json!({"home_zone": "eu1a", "zone": "eu1a", "cell": "eu1a", "deployment_unit": "bin-z"});
        assert!(resolve(&topo, &bad).is_err());
        let self_failover = json!({"home_zone": "eu1a", "failover_zone": "eu1", "zone": "eu1a",
                                   "cell": "eu1a", "deployment_unit": "bin-a"});
        assert!(resolve(&topo, &self_failover).is_err());
    }
}
