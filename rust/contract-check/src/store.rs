//! parity-store's Kafka contracts (0.5.0): the `store` fixtures against `schemas/store/`, the
//! cross-field rules of CONVENTIONS.md "parity-store API", the reader action they call for, and
//! coverage (every command, event type and state kind has a valid fixture; every rule an invalid
//! one).

use crate::rules::{is_dns_label, is_reply_topic};
use crate::{Report, Schemas};
use serde_json::{Map, Value};
use std::collections::BTreeSet;
use std::path::Path;

/// Every rule code a `store` fixture can declare.
pub const RULE_CODES: [&str; 9] = [
    "schema",
    "bad_id",
    "bad_reply_to",
    "admin_only",
    "placement_only",
    "placement_via_parity_rs",
    "key_mismatch",
    "header_body_mismatch",
    "missing_header",
];

pub const READ_TOPIC: &str = "tenant.store.requests";
pub const ADMIN_TOPIC: &str = "tenant.store.admin.requests";
/// 0.16.0: parity-rs's narrow topic: placement writes (and the reads) only.
pub const PLACEMENT_TOPIC: &str = "tenant.store.placement.requests";
/// The writes that belong to the placement topic (CONVENTIONS "parity-store API").
pub const PLACEMENT_COMMANDS: [&str; 2] = ["set_placement", "delete_placement"];
pub const EVENTS_TOPIC: &str = "tenant.store.events";
pub const STATE_TOPIC: &str = "tenant.store.state";
const STATE_KINDS: [&str; 3] = ["tenant", "number", "sip_domain"];

/// The schema of a store topic.
pub fn schema_for(topic: &str) -> Option<&'static str> {
    match topic {
        READ_TOPIC | ADMIN_TOPIC | PLACEMENT_TOPIC => Some("store-requests"),
        EVENTS_TOPIC => Some("store-events"),
        STATE_TOPIC => Some("store-state"),
        t if t.starts_with("replies.") => Some("store-replies"),
        _ => None,
    }
}

/// `^[A-Za-z0-9._:-]{1,200}$`
fn is_request_id(s: &str) -> bool {
    (1..=200).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
}

/// The commands marked `"x-admin": true` in the requests schema: the writes.
pub fn admin_commands(schemas: &Schemas) -> BTreeSet<String> {
    schemas.docs["store-requests"]["$defs"]
        .as_object()
        .into_iter()
        .flatten()
        .filter(|(_, d)| d["x-admin"] == true)
        .filter_map(|(_, d)| d["properties"]["cmd"]["const"].as_str().map(str::to_owned))
        .collect()
}

/// Whether a state key names its kind and a well-formed id (`tenant:<id>`, `number:<e164>`,
/// `sip_domain:<domain>`), and which.
fn state_key(key: &str) -> Option<(&str, &str)> {
    let (kind, id) = key.split_once(':')?;
    let ok = match kind {
        "tenant" => is_dns_label(id),
        "number" => {
            id.len() >= 8
                && id.len() <= 16
                && id.starts_with('+')
                && id[1..].bytes().all(|b| b.is_ascii_digit())
                && !id[1..].starts_with('0')
        }
        "sip_domain" => !id.is_empty() && id.len() <= 253 && id.contains('.'),
        _ => false,
    };
    ok.then_some((kind, id))
}

/// The rules a store record breaks.
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
    let tombstone = topic == STATE_TOPIC && value.is_some_and(Value::is_null);
    if !tombstone && value.is_none_or(|v| !schemas.errors(schema, v).is_empty()) {
        out.insert("schema");
    }
    let field = |name: &str| value.and_then(|v| v.get(name)).and_then(Value::as_str);
    match schema {
        "store-requests" => {
            if !field("id").is_some_and(is_request_id) {
                out.insert("bad_id");
            }
            if !field("reply_to").is_some_and(is_reply_topic) {
                out.insert("bad_reply_to");
            }
            let write = field("cmd").filter(|c| admin_commands(schemas).contains(*c));
            let placement = write.is_some_and(|c| PLACEMENT_COMMANDS.contains(&c));
            if topic == READ_TOPIC && write.is_some() {
                out.insert("admin_only");
            }
            if topic == PLACEMENT_TOPIC && write.is_some() && !placement {
                out.insert("placement_only");
            }
            if topic == ADMIN_TOPIC && placement {
                out.insert("placement_via_parity_rs");
            }
        }
        "store-events" => {
            let header = |n: &str| headers.get(n).and_then(Value::as_str);
            if !header("tenant_id").is_some_and(is_dns_label) || header("schema_version").is_none()
            {
                out.insert("missing_header");
            }
            if let (Some(h), Some(b)) = (header("tenant_id"), field("tenant_id"))
                && h != b
            {
                out.insert("header_body_mismatch");
            }
            if field("tenant_id").is_some_and(|t| t != key) {
                out.insert("key_mismatch");
            }
        }
        "store-state" => match (state_key(key), value.filter(|v| !v.is_null())) {
            (None, _) => {
                out.insert("key_mismatch");
            }
            (Some((kind, id)), Some(v)) => {
                let own = match kind {
                    "tenant" => "tenant_id",
                    "number" => "number",
                    _ => "domain",
                };
                if v["kind"].as_str() != Some(kind) || v[own].as_str() != Some(id) {
                    out.insert("key_mismatch");
                }
            }
            (Some(_), None) => {}
        },
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
        Some("store-requests") => {
            if value.is_none() || broken.contains("bad_id") || broken.contains("bad_reply_to") {
                "dlt"
            } else if broken.contains("admin_only")
                || broken.contains("placement_only")
                || broken.contains("placement_via_parity_rs")
            {
                "reply_error:forbidden"
            } else if broken.contains("schema") {
                "reply_error:bad_request"
            } else {
                "apply"
            }
        }
        Some("store-replies")
            if value
                .and_then(|v| v.get("id"))
                .is_some_and(Value::is_string) =>
        {
            "apply"
        }
        Some("store-replies") => "reject",
        _ if broken.is_empty() => "apply",
        _ => "reject",
    }
}

/// A `store` fixture: it breaks exactly the rules it declares, and its reader action follows.
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
        .store_codes
        .extend(declared.iter().map(|c| (*c).to_owned()));
    if f["writer_valid"] == true
        && let Some(v) = value.filter(|v| !v.is_null())
    {
        let name = match schema_for(topic) {
            Some("store-requests") => v["cmd"].as_str().map(|c| format!("cmd {c}")),
            Some("store-events") => v["type"].as_str().map(|t| format!("event {t}")),
            Some("store-state") => v["kind"].as_str().map(|k| format!("state {k}")),
            _ => None,
        };
        report.store_seen.extend(name);
    }
}

/// The write lint (parity-store BUGS ST-019): a command named like a write carries
/// `"x-admin": true`, and one named like a read doesn't. parity-store also fails closed (only its
/// fixed reads run on the read topic); this keeps the schema's mark honest for other readers.
pub fn admin_lint(schemas: &Schemas) -> Vec<String> {
    let writes = ["put_", "delete_", "set_", "add_", "remove_"];
    let reads = ["get_", "list_", "lookup_", "ping"];
    let mut out = Vec::new();
    for d in schemas.docs["store-requests"]["$defs"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(_, d)| d)
    {
        let Some(cmd) = d["properties"]["cmd"]["const"].as_str() else {
            continue;
        };
        let admin = d["x-admin"] == true;
        if writes.iter().any(|w| cmd.starts_with(w)) && !admin {
            out.push(format!(
                "store-requests: {cmd} is a write but lacks \"x-admin\": true"
            ));
        }
        if reads.iter().any(|r| cmd.starts_with(r)) && admin {
            out.push(format!(
                "store-requests: {cmd} is a read but is marked \"x-admin\""
            ));
        }
    }
    out
}

/// Every command, event type and state kind has a valid fixture; every rule an invalid one.
pub(crate) fn coverage(schemas: &Schemas, report: &mut Report) {
    let mut want: Vec<String> = Vec::new();
    for (schema, field, prefix) in [
        ("store-requests", "cmd", "cmd"),
        ("store-events", "type", "event"),
    ] {
        let doc = &schemas.docs[schema];
        for d in doc["$defs"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(_, d)| d)
        {
            if let Some(c) = d["properties"][field]["const"].as_str() {
                want.push(format!("{prefix} {c}"));
            }
        }
    }
    want.extend(STATE_KINDS.iter().map(|k| format!("state {k}")));
    report.errors.extend(admin_lint(schemas));
    for w in want {
        if !report.store_seen.contains(&w) {
            report
                .errors
                .push(format!("coverage: no valid store fixture for {w}"));
        }
    }
    for code in RULE_CODES {
        if !report.store_codes.contains(code) {
            report
                .errors
                .push(format!("coverage: no store fixture for rule {code}"));
        }
    }
}
