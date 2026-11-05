//! parity-billing's Kafka contracts and admin-console's provisioning contract (0.10.0): the
//! `billing` fixtures against `schemas/billing/` and `schemas/admin/`, the cross-field rules of
//! CONVENTIONS.md "Billing API", "Entitlements" and "Provisioning", the reader action they call
//! for, coverage, and the shared test entitlements (`tenants/test-entitlements.v1.json`).

use crate::rules::{is_dns_label, is_reply_topic};
use crate::{Report, Schemas, read_json};
use serde_json::{Map, Value};
use std::collections::BTreeSet;
use std::path::Path;

/// Every rule code a `billing` fixture can declare.
pub const RULE_CODES: [&str; 8] = [
    "schema",
    "bad_id",
    "bad_reply_to",
    "reply_to_not_allowed",
    "wrong_topic",
    "key_mismatch",
    "header_body_mismatch",
    "missing_header",
];

pub const WEB_TOPIC: &str = "billing.web.requests";
pub const READ_TOPIC: &str = "billing.requests";
pub const ADMIN_TOPIC: &str = "billing.admin.requests";
pub const EVENTS_TOPIC: &str = "billing.events";
pub const ENTITLEMENTS_TOPIC: &str = "billing.entitlements";
pub const PROVISIONING_TOPIC: &str = "admin.provisioning.requests";
pub const PROVISIONING_REPLIES: &str = "replies.parity-billing";
pub const TEST_FILE: &str = "tenants/test-entitlements.v1.json";

/// The schema of a billing topic.
pub fn schema_for(topic: &str) -> Option<&'static str> {
    match topic {
        WEB_TOPIC | READ_TOPIC | ADMIN_TOPIC => Some("billing-requests"),
        EVENTS_TOPIC => Some("billing-events"),
        ENTITLEMENTS_TOPIC => Some("billing-entitlements"),
        PROVISIONING_TOPIC => Some("provisioning-requests"),
        PROVISIONING_REPLIES => Some("provisioning-replies"),
        t if t.starts_with("replies.") => Some("billing-replies"),
        _ => None,
    }
}

/// The only `reply_to` a request topic accepts (CONVENTIONS.md: pinned per topic).
pub fn pinned_reply_to(topic: &str) -> Option<&'static str> {
    match topic {
        WEB_TOPIC => Some("replies.parity-frontend"),
        READ_TOPIC | ADMIN_TOPIC => Some("replies.admin-console"),
        PROVISIONING_TOPIC => Some(PROVISIONING_REPLIES),
        _ => None,
    }
}

/// `^[A-Za-z0-9._:-]{1,200}$`
fn is_request_id(s: &str) -> bool {
    (1..=200).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
}

/// The mark of a billing command (`web`, `admin`, or none), if the schema defines it.
pub fn mark(schemas: &Schemas, cmd: &str) -> Option<Option<&'static str>> {
    schemas.docs["billing-requests"]["$defs"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(_, d)| d)
        .find(|d| d["properties"]["cmd"]["const"].as_str() == Some(cmd))
        .map(|d| {
            if d["x-web"] == true {
                Some("web")
            } else if d["x-admin"] == true {
                Some("admin")
            } else {
                None
            }
        })
}

/// Whether a known command may run on a request topic: ping everywhere, the `x-web` commands on
/// billing.web.requests, the `x-admin` ones on billing.admin.requests, the unmarked on
/// billing.requests.
pub fn allowed(topic: &str, cmd: &str, mark: Option<&str>) -> bool {
    cmd == "ping"
        || matches!(
            (topic, mark),
            (WEB_TOPIC, Some("web")) | (ADMIN_TOPIC, Some("admin")) | (READ_TOPIC, None)
        )
}

/// The rules a billing record breaks.
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
    let tombstone = topic == ENTITLEMENTS_TOPIC && value.is_some_and(Value::is_null);
    if !tombstone && value.is_none_or(|v| !schemas.errors(schema, v).is_empty()) {
        out.insert("schema");
    }
    let field = |name: &str| value.and_then(|v| v.get(name)).and_then(Value::as_str);
    let header = |n: &str| headers.get(n).and_then(Value::as_str);
    match schema {
        "billing-requests" | "provisioning-requests" => {
            if !field("id").is_some_and(is_request_id) {
                out.insert("bad_id");
            }
            match field("reply_to") {
                Some(r) if is_reply_topic(r) => {
                    if pinned_reply_to(topic).is_some_and(|p| p != r) {
                        out.insert("reply_to_not_allowed");
                    }
                }
                _ => {
                    out.insert("bad_reply_to");
                }
            }
            if schema == "billing-requests"
                && let Some(cmd) = field("cmd")
                && let Some(m) = mark(schemas, cmd)
                && !allowed(topic, cmd, m)
            {
                out.insert("wrong_topic");
            }
        }
        "billing-events" => {
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
        "billing-entitlements" => {
            let key_tenant = key.strip_prefix("tenant:").filter(|t| is_dns_label(t));
            if header("schema_version").is_none()
                || header("version").is_none()
                || (!tombstone && !header("tenant_id").is_some_and(is_dns_label))
            {
                out.insert("missing_header");
            }
            match key_tenant {
                None => {
                    out.insert("key_mismatch");
                }
                Some(t) if !tombstone && field("tenant_id").is_some_and(|b| b != t) => {
                    out.insert("key_mismatch");
                }
                _ => {}
            }
            if !tombstone {
                if let (Some(h), Some(b)) = (header("tenant_id"), field("tenant_id"))
                    && h != b
                {
                    out.insert("header_body_mismatch");
                }
                let body_version = value.and_then(|v| v.get("version")).and_then(Value::as_i64);
                if let (Some(h), Some(b)) = (header("version"), body_version)
                    && h.parse::<i64>().ok() != Some(b)
                {
                    out.insert("header_body_mismatch");
                }
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
        Some("billing-requests" | "provisioning-requests") => {
            if value.is_none()
                || broken.contains("bad_id")
                || broken.contains("bad_reply_to")
                || broken.contains("reply_to_not_allowed")
            {
                "dlt"
            } else if broken.contains("wrong_topic") {
                "reply_error:forbidden"
            } else if broken.contains("schema") {
                "reply_error:bad_request"
            } else {
                "apply"
            }
        }
        Some("billing-replies" | "provisioning-replies")
            if value
                .and_then(|v| v.get("id"))
                .is_some_and(Value::is_string) =>
        {
            "apply"
        }
        Some("billing-replies" | "provisioning-replies") => "reject",
        _ if broken.is_empty() => "apply",
        _ => "reject",
    }
}

/// A `billing` fixture: it breaks exactly the rules it declares, and its reader action follows.
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
        .billing_codes
        .extend(declared.iter().map(|c| (*c).to_owned()));
    if f["writer_valid"] == true {
        let name = match (schema_for(topic), value) {
            (Some("billing-requests"), Some(v)) => v["cmd"].as_str().map(|c| format!("cmd {c}")),
            (Some("provisioning-requests"), Some(v)) => {
                v["cmd"].as_str().map(|c| format!("provisioning {c}"))
            }
            (Some("billing-events"), Some(v)) => v["type"].as_str().map(|t| format!("event {t}")),
            (Some("billing-entitlements"), Some(Value::Null)) => {
                Some("entitlements tombstone".into())
            }
            (Some("billing-entitlements"), Some(_)) => Some("entitlements record".into()),
            _ => None,
        };
        report.billing_seen.extend(name);
    }
}

/// The marks are honest: a `get_`/`list_`/`ping` command never carries `x-admin`, and no command
/// carries both marks. (Whether a command is accepted on a topic is the `wrong_topic` rule.)
pub fn mark_lint(schemas: &Schemas) -> Vec<String> {
    let mut out = Vec::new();
    for d in schemas.docs["billing-requests"]["$defs"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(_, d)| d)
    {
        let Some(cmd) = d["properties"]["cmd"]["const"].as_str() else {
            continue;
        };
        if d["x-web"] == true && d["x-admin"] == true {
            out.push(format!(
                "billing-requests: {cmd} is marked both x-web and x-admin"
            ));
        }
        if (cmd == "ping" || cmd.starts_with("get_") || cmd.starts_with("list_"))
            && d["x-admin"] == true
        {
            out.push(format!(
                "billing-requests: {cmd} is a read but is marked \"x-admin\""
            ));
        }
        if cmd == "ping" && d["x-web"] == true {
            out.push("billing-requests: ping is answered on every topic: no mark".to_owned());
        }
    }
    out
}

/// Every command, provisioning command and event type has a valid fixture, and an entitlement record
/// and a tombstone; every rule an invalid one.
pub(crate) fn coverage(schemas: &Schemas, report: &mut Report) {
    let mut want: Vec<String> = Vec::new();
    for (schema, field, prefix) in [
        ("billing-requests", "cmd", "cmd"),
        ("provisioning-requests", "cmd", "provisioning"),
        ("billing-events", "type", "event"),
    ] {
        for d in schemas.docs[schema]["$defs"]
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
    want.push("entitlements record".into());
    want.push("entitlements tombstone".into());
    report.errors.extend(mark_lint(schemas));
    for w in want {
        if !report.billing_seen.contains(&w) {
            report
                .errors
                .push(format!("coverage: no valid billing fixture for {w}"));
        }
    }
    for code in RULE_CODES {
        if !report.billing_codes.contains(code) {
            report
                .errors
                .push(format!("coverage: no billing fixture for rule {code}"));
        }
    }
}

/// The shared test entitlements: each validates, names a shared test tenant that takes traffic
/// (not a parent), once.
pub(crate) fn check_test_file(root: &Path, schemas: &Schemas, report: &mut Report) {
    let path = root.join(TEST_FILE);
    let doc = match read_json(&path) {
        Ok(v) => v,
        Err(e) => return report.fail(&path, format!("{e:#}")),
    };
    let tenants = match read_json(&root.join(crate::tenants::TEST_FILE)) {
        Ok(v) => v,
        Err(e) => return report.fail(&path, format!("the test tenants: {e:#}")),
    };
    let all = tenants["tenants"].as_array().cloned().unwrap_or_default();
    let parents: BTreeSet<&str> = all.iter().filter_map(|t| t["parent"].as_str()).collect();
    let known: BTreeSet<&str> = all.iter().filter_map(|t| t["id"].as_str()).collect();
    let mut seen = BTreeSet::new();
    let entries = doc["entitlements"].as_array().cloned().unwrap_or_default();
    if doc["schema_version"] != 1 || entries.is_empty() {
        report.fail(&path, "needs schema_version 1 and at least one entitlement");
    }
    for (i, e) in entries.iter().enumerate() {
        let errs = schemas.errors("billing-entitlements", e);
        if !errs.is_empty() {
            report.fail(&path, format!("/entitlements/{i}: {}", errs.join("; ")));
        }
        let t = e["tenant_id"].as_str().unwrap_or_default();
        if !known.contains(t) {
            report.fail(
                &path,
                format!("/entitlements/{i}: {t} isn't a shared test tenant"),
            );
        } else if parents.contains(t) {
            report.fail(
                &path,
                format!("/entitlements/{i}: {t} is a parent (no traffic, no record)"),
            );
        }
        if !seen.insert(t.to_owned()) {
            report.fail(&path, format!("/entitlements/{i}: {t} listed twice"));
        }
    }
    *report
        .checked
        .entry("test-entitlements".into())
        .or_default() += entries.len();
}

/// The break gate for the released test entitlements: each still validates, and still exists.
pub(crate) fn break_gate(
    root: &Path,
    schemas: &Schemas,
    released: &Path,
    report: &mut Report,
) -> anyhow::Result<()> {
    let old = read_json(released)?;
    let now = read_json(&root.join(TEST_FILE))?;
    let current: BTreeSet<String> = now["entitlements"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|e| e["tenant_id"].as_str().map(str::to_owned))
        .collect();
    for e in old["entitlements"].as_array().into_iter().flatten() {
        let t = e["tenant_id"].as_str().unwrap_or_default();
        let errs = schemas.errors("billing-entitlements", e);
        if !errs.is_empty() {
            report.fail(
                released,
                format!(
                    "break gate: the released entitlements of {t} no longer validate: {}",
                    errs.join("; ")
                ),
            );
        }
        if !current.contains(t) {
            report.fail(
                released,
                format!("break gate: the test entitlements of {t} were removed"),
            );
        }
    }
    Ok(())
}
