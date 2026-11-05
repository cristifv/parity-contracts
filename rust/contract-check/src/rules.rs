//! The Kafka cross-field rules of CONVENTIONS.md: headers, keys, `reply_to`, the `call_id`
//! alias and the schema version. A malformed header counts as missing.

use serde_json::{Map, Value};
use std::collections::BTreeSet;

/// Every rule code a fixture can declare (the part before `:`).
pub const RULE_CODES: [&str; 7] = [
    "schema",
    "missing_header",
    "header_body_mismatch",
    "key_mismatch",
    "bad_reply_to",
    "unknown_schema_version",
    "alias_mismatch",
];

const HEADERS: [&str; 4] = [
    "tenant_id",
    "interaction_id",
    "schema_version",
    "traceparent",
];

/// The rules a record breaks. `schema_ok` is the result of the topic's schema on the body.
pub fn kafka_violations(
    topic: &str,
    key: &str,
    headers: &Map<String, Value>,
    value: Option<&Value>,
    schema_ok: bool,
) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    if value.is_none() || !schema_ok {
        out.insert("schema".to_owned());
    }
    let header = |name: &str| {
        headers
            .get(name)
            .and_then(Value::as_str)
            .filter(|v| header_well_formed(name, v))
    };
    for name in HEADERS {
        if header(name).is_none() {
            out.insert(format!("missing_header:{name}"));
        }
    }
    let body = value.and_then(Value::as_object);
    let field = |name: &str| body.and_then(|b| b.get(name));
    for name in ["tenant_id", "interaction_id"] {
        if let (Some(h), Some(Value::String(b))) = (header(name), field(name))
            && h != b
        {
            out.insert(format!("header_body_mismatch:{name}"));
        }
    }
    if let Some(Value::Number(v)) = field("schema_version") {
        if header("schema_version").is_some_and(|h| h != v.to_string()) {
            out.insert("header_body_mismatch:schema_version".to_owned());
        }
        if v.as_i64() != Some(1) {
            out.insert("unknown_schema_version".to_owned());
        }
    }
    let expected_key = field("interaction_id")
        .and_then(Value::as_str)
        .or_else(|| header("interaction_id"));
    if expected_key.is_some_and(|k| k != key) {
        out.insert("key_mismatch".to_owned());
    }
    if topic == "interaction.commands" {
        if !field("reply_to")
            .and_then(Value::as_str)
            .is_some_and(is_reply_topic)
        {
            out.insert("bad_reply_to".to_owned());
        }
        if let (Some(alias), Some(id)) = (field("call_id"), field("interaction_id"))
            && alias != id
        {
            out.insert("alias_mismatch".to_owned());
        }
    }
    out
}

/// The reader action CONVENTIONS.md requires for a record, derived from the rules it breaks
/// (G1). `family` is `events`, `commands` or `replies`; `known_type` says whether an event's
/// `type` is a v1 type; `tolerant_ok` whether the event passes the schema without the
/// writer-only strictness (a reader ignores unknown fields).
pub fn expected_reader(
    family: &str,
    broken: &BTreeSet<String>,
    value: Option<&Value>,
    known_type: bool,
    tolerant_ok: bool,
) -> &'static str {
    let faults: Vec<&str> = broken
        .iter()
        .map(String::as_str)
        .filter(|c| *c != "missing_header:traceparent")
        .collect();
    match family {
        "commands" => {
            if value.is_none()
                || broken.contains("bad_reply_to")
                || broken.contains("missing_header:tenant_id")
            {
                "dlt"
            } else if faults.is_empty() {
                "apply"
            } else {
                "reply_error:bad_request"
            }
        }
        "events" => {
            let header_or_key = faults.iter().any(|c| {
                c.starts_with("missing_header:")
                    || c.starts_with("header_body_mismatch:")
                    || *c == "key_mismatch"
                    || *c == "unknown_schema_version"
            });
            if value.is_none() || header_or_key {
                "reject"
            } else if !known_type {
                "ignore"
            } else if faults.contains(&"schema") && !tolerant_ok {
                "reject"
            } else {
                "apply"
            }
        }
        _ => {
            if value
                .and_then(|v| v.get("id"))
                .is_some_and(Value::is_string)
            {
                "apply"
            } else {
                "reject"
            }
        }
    }
}

fn header_well_formed(name: &str, v: &str) -> bool {
    match name {
        "tenant_id" => is_dns_label(v),
        "interaction_id" => is_uuid(v),
        "schema_version" => !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()),
        "traceparent" => is_traceparent(v),
        _ => true,
    }
}

/// `^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`
pub fn is_dns_label(s: &str) -> bool {
    let b = s.as_bytes();
    let alnum = |c: &u8| c.is_ascii_lowercase() || c.is_ascii_digit();
    (1..=63).contains(&b.len())
        && b.first().is_some_and(alnum)
        && b.last().is_some_and(alnum)
        && b.iter().all(|c| alnum(c) || *c == b'-')
}

/// An RFC 9562 UUID in lowercase canonical text, versions 1 to 8.
pub fn is_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, c)| {
            if [8, 13, 18, 23].contains(&i) {
                *c == b'-'
            } else {
                c.is_ascii_digit() || (b'a'..=b'f').contains(c)
            }
        })
        && (b'1'..=b'8').contains(&b[14])
        && b"89ab".contains(&b[19])
}

/// W3C `00-<32 hex>-<16 hex>-<2 hex>`, lowercase.
pub fn is_traceparent(s: &str) -> bool {
    let hex = |p: &str, n: usize| {
        p.len() == n
            && p.bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    };
    let parts: Vec<&str> = s.split('-').collect();
    parts.len() == 4
        && parts[0] == "00"
        && hex(parts[1], 32)
        && hex(parts[2], 16)
        && hex(parts[3], 2)
}

/// `^replies\.[a-z0-9][a-z0-9.-]{0,200}$`
pub fn is_reply_topic(s: &str) -> bool {
    let Some(rest) = s.strip_prefix("replies.") else {
        return false;
    };
    let b = rest.as_bytes();
    (1..=201).contains(&b.len())
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b.iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'.' || *c == b'-')
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const ID: &str = "0192f6a1-7c3e-7b21-9a4d-5e6f70819203";
    const TP: &str = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";

    fn headers(tenant: &str, sv: &str) -> Map<String, Value> {
        json!({"tenant_id": tenant, "interaction_id": ID, "schema_version": sv, "traceparent": TP})
            .as_object()
            .unwrap()
            .clone()
    }
    fn set(codes: &[&str]) -> BTreeSet<String> {
        codes.iter().map(|c| c.to_string()).collect()
    }

    #[test]
    fn a_clean_event_breaks_nothing() {
        let v = json!({"schema_version": 1, "tenant_id": "acme", "interaction_id": ID});
        assert!(
            kafka_violations(
                "interaction.events",
                ID,
                &headers("acme", "1"),
                Some(&v),
                true
            )
            .is_empty()
        );
    }

    #[test]
    fn each_cross_field_rule() {
        let v = json!({"schema_version": 1, "tenant_id": "acme", "interaction_id": ID});
        let e = "interaction.events";
        assert_eq!(
            kafka_violations(e, ID, &headers("globex", "1"), Some(&v), true),
            set(&["header_body_mismatch:tenant_id"])
        );
        assert_eq!(
            kafka_violations(e, "acme", &headers("acme", "1"), Some(&v), true),
            set(&["key_mismatch"])
        );
        assert_eq!(
            kafka_violations(e, ID, &headers("acme", "2"), Some(&v), true),
            set(&["header_body_mismatch:schema_version"])
        );
        assert_eq!(
            kafka_violations(e, ID, &headers("Acme", "1"), Some(&v), true),
            set(&["missing_header:tenant_id"])
        );
        let v2 = json!({"schema_version": 2, "tenant_id": "acme", "interaction_id": ID});
        assert_eq!(
            kafka_violations(e, ID, &headers("acme", "2"), Some(&v2), false),
            set(&["schema", "unknown_schema_version"])
        );
        assert_eq!(
            kafka_violations(e, ID, &headers("acme", "1"), None, false),
            set(&["schema"])
        );
    }

    #[test]
    fn command_rules() {
        let c = "interaction.commands";
        let ok = json!({"schema_version": 1, "tenant_id": "acme", "interaction_id": ID, "reply_to": "replies.parity-voice-api"});
        assert!(kafka_violations(c, ID, &headers("acme", "1"), Some(&ok), true).is_empty());
        let mut bad = ok.clone();
        bad["reply_to"] = json!("interaction.events");
        assert_eq!(
            kafka_violations(c, ID, &headers("acme", "1"), Some(&bad), true),
            set(&["bad_reply_to"])
        );
        let mut alias = ok.clone();
        alias["call_id"] = json!("0192f6a1-8a10-7c55-b123-0a1b2c3d4e5f");
        assert_eq!(
            kafka_violations(c, ID, &headers("acme", "1"), Some(&alias), true),
            set(&["alias_mismatch"])
        );
    }

    #[test]
    fn reader_actions_follow_the_broken_rules() {
        let v = json!({"type": "hold_started", "id": "x"});
        let r = |family, codes: &[&str], known, tolerant| {
            expected_reader(family, &set(codes), Some(&v), known, tolerant)
        };
        assert_eq!(
            r("commands", &["bad_reply_to", "schema"], true, true),
            "dlt"
        );
        assert_eq!(
            r("commands", &["missing_header:tenant_id"], true, true),
            "dlt"
        );
        assert_eq!(
            expected_reader("commands", &set(&["schema"]), None, false, false),
            "dlt"
        );
        assert_eq!(
            r("commands", &["key_mismatch"], true, true),
            "reply_error:bad_request"
        );
        assert_eq!(
            r("commands", &["missing_header:traceparent"], true, true),
            "apply"
        );
        assert_eq!(
            r("events", &["missing_header:tenant_id"], true, true),
            "reject"
        );
        assert_eq!(r("events", &["key_mismatch"], true, true), "reject");
        assert_eq!(r("events", &["schema"], false, false), "ignore");
        assert_eq!(r("events", &["schema"], true, true), "apply");
        assert_eq!(r("events", &["schema"], true, false), "reject");
        assert_eq!(r("replies", &[], true, true), "apply");
        assert_eq!(
            expected_reader(
                "replies",
                &set(&["schema"]),
                Some(&json!({"ok": true})),
                false,
                false
            ),
            "reject"
        );
    }

    #[test]
    fn formats() {
        assert!(
            is_dns_label("acme")
                && is_dns_label("a1-b2")
                && !is_dns_label("-a")
                && !is_dns_label("a.b")
                && !is_dns_label(&"a".repeat(64))
        );
        assert!(
            is_uuid(ID)
                && !is_uuid(&ID.to_uppercase())
                && !is_uuid("0192f6a1-7c3e-0b21-9a4d-5e6f70819203")
        );
        assert!(
            is_traceparent(TP)
                && !is_traceparent("01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        );
        assert!(
            is_reply_topic("replies.admin-console")
                && !is_reply_topic("replies.")
                && !is_reply_topic("replies.Admin")
        );
    }
}
