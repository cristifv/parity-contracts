//! The tenant maps file (0.4.0): `schemas/tenants/tenant-maps.v1.json` plus CONVENTIONS.md's
//! "Tenant maps" rules, the shared test tenants (`tenants/test-tenants.v1.json`) and the
//! `tenant_maps` fixtures. parity-store applies the same rules, with the same codes and
//! locations, when it reads its maps file at startup.

use crate::{Report, Schemas, read_json};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

/// Every rule code, in the order the rules run. `schema` stops the others: they need the
/// structure the schema guarantees.
pub const RULE_CODES: [&str; 15] = [
    "schema",
    "duplicate_tenant",
    "missing_tier",
    "unknown_parent",
    "nested_sub_tenant",
    "parent_not_enterprise",
    "tier_mismatch",
    "parent_takes_traffic",
    "missing_placement",
    "duplicate_number",
    "duplicate_sip_domain",
    "test_unknown_tenant",
    "duplicate_agent",
    "test_number_not_fictional",
    "test_domain_not_reserved",
];

/// The shared test tenants, relative to the repository root.
pub const TEST_FILE: &str = "tenants/test-tenants.v1.json";

/// The tenants every consumer may rely on being in the shared test file.
const TEST_TENANTS_REQUIRED: [&str; 2] = ["acme", "globex"];

/// One broken rule: its code and a JSON pointer into the file.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Violation {
    pub code: &'static str,
    pub at: String,
}

impl std::fmt::Display for Violation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let at = if self.at.is_empty() { "/" } else { &self.at };
        write!(f, "{} at {at}", self.code)
    }
}

fn v(code: &'static str, at: impl Into<String>) -> Violation {
    Violation {
        code,
        at: at.into(),
    }
}

/// Every rule `file` breaks; empty for a valid file.
pub fn violations(schemas: &Schemas, file: &Value) -> Vec<Violation> {
    let schema = schemas.error_paths("tenant-maps", file);
    if !schema.is_empty() {
        return schema.into_iter().map(|at| v("schema", at)).collect();
    }
    rules(file)
}

fn strs<'a>(t: &'a Value, field: &str) -> Vec<&'a str> {
    t[field]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect()
}

/// The cross-tenant rules, on a file that passed the schema.
fn rules(file: &Value) -> Vec<Violation> {
    let mut out = Vec::new();
    let tenants: &[Value] = file["tenants"].as_array().map_or(&[], Vec::as_slice);
    let mut by_id: BTreeMap<&str, &Value> = BTreeMap::new();
    for (i, t) in tenants.iter().enumerate() {
        let id = t["id"].as_str().unwrap_or_default();
        if by_id.contains_key(id) {
            out.push(v("duplicate_tenant", format!("/tenants/{i}/id")));
        } else {
            by_id.insert(id, t);
        }
    }
    let parents: BTreeSet<&str> = tenants
        .iter()
        .filter_map(|t| t["parent"].as_str())
        .collect();
    for (i, t) in tenants.iter().enumerate() {
        let at = format!("/tenants/{i}");
        match t["parent"].as_str() {
            None if t.get("tier").is_none() => out.push(v("missing_tier", &at)),
            None => {}
            Some(p) => match by_id.get(p) {
                None => out.push(v("unknown_parent", format!("{at}/parent"))),
                Some(pt) => {
                    if pt.get("parent").is_some() {
                        out.push(v("nested_sub_tenant", format!("{at}/parent")));
                    } else if pt["tier"].as_str() != Some("enterprise") {
                        out.push(v("parent_not_enterprise", format!("{at}/parent")));
                    }
                    if let (Some(tier), Some(parent_tier)) =
                        (t["tier"].as_str(), pt["tier"].as_str())
                        && tier != parent_tier
                    {
                        out.push(v("tier_mismatch", format!("{at}/tier")));
                    }
                }
            },
        }
        let id = t["id"].as_str().unwrap_or_default();
        let has_placement = t.get("placement").is_some();
        if parents.contains(id) {
            if has_placement || !strs(t, "numbers").is_empty() || !strs(t, "sip_domains").is_empty()
            {
                out.push(v("parent_takes_traffic", &at));
            }
        } else if !has_placement {
            out.push(v("missing_placement", &at));
        }
    }
    for (field, code) in [
        ("numbers", "duplicate_number"),
        ("sip_domains", "duplicate_sip_domain"),
    ] {
        let mut seen = BTreeSet::new();
        for (i, t) in tenants.iter().enumerate() {
            for (j, value) in strs(t, field).into_iter().enumerate() {
                if !seen.insert(value) {
                    out.push(v(code, format!("/tenants/{i}/{field}/{j}")));
                }
            }
        }
    }
    if let Some(test) = file.get("test") {
        test_rules(test, tenants, &by_id, &parents, &mut out);
    }
    out
}

/// The rules of a test file (one with a `test` section).
fn test_rules(
    test: &Value,
    tenants: &[Value],
    by_id: &BTreeMap<&str, &Value>,
    parents: &BTreeSet<&str>,
    out: &mut Vec<Violation>,
) {
    let mut count: BTreeMap<&str, usize> = BTreeMap::new();
    let agents = test["agents"].as_object();
    for (tenant, ids) in agents.into_iter().flatten() {
        if !by_id.contains_key(tenant.as_str()) || parents.contains(tenant.as_str()) {
            out.push(v("test_unknown_tenant", format!("/test/agents/{tenant}")));
        }
        for id in ids
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            *count.entry(id).or_default() += 1;
        }
    }
    // Every occurrence of a repeated agent id: object key order differs between languages.
    for (tenant, ids) in agents.into_iter().flatten() {
        for (k, id) in ids.as_array().into_iter().flatten().enumerate() {
            if id.as_str().is_some_and(|id| count[id] > 1) {
                out.push(v("duplicate_agent", format!("/test/agents/{tenant}/{k}")));
            }
        }
    }
    for (i, t) in tenants.iter().enumerate() {
        for (j, n) in strs(t, "numbers").into_iter().enumerate() {
            if !fictional_number(n) {
                out.push(v(
                    "test_number_not_fictional",
                    format!("/tenants/{i}/numbers/{j}"),
                ));
            }
        }
        for (j, d) in strs(t, "sip_domains").into_iter().enumerate() {
            if !reserved_domain(d) {
                out.push(v(
                    "test_domain_not_reserved",
                    format!("/tenants/{i}/sip_domains/{j}"),
                ));
            }
        }
    }
}

/// +1 555 01xx xxx (NPA 555 with an NXX starting with 0: never dialable) or Ofcom's drama range
/// +44 7700 900000–900999.
pub fn fictional_number(n: &str) -> bool {
    let digits = |prefix: &str, count: usize| {
        n.strip_prefix(prefix)
            .is_some_and(|rest| rest.len() == count && rest.bytes().all(|b| b.is_ascii_digit()))
    };
    digits("+155501", 5) || digits("+447700900", 3)
}

/// Under a top-level name reserved by RFC 2606 / RFC 6761.
pub fn reserved_domain(d: &str) -> bool {
    [".example", ".test", ".invalid", ".localhost"]
        .iter()
        .any(|s| d.ends_with(s))
}

/// A `tenant_maps` fixture: a valid file breaks nothing; an invalid one breaks exactly the rules
/// it declares, one of them at `at` when given.
pub(crate) fn check_fixture(schemas: &Schemas, file: &Path, f: &Value, report: &mut Report) {
    let found = violations(schemas, &f["file"]);
    let codes: BTreeSet<&str> = found.iter().map(|x| x.code).collect();
    let declared: BTreeSet<&str> = f["violates"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect();
    if codes != declared {
        let first: Vec<String> = found.iter().take(5).map(ToString::to_string).collect();
        report.fail(
            file,
            format!(
                "breaks {codes:?}, declares {declared:?} ({})",
                first.join("; ")
            ),
        );
    }
    if let Some(at) = f["at"].as_str()
        && !found.iter().any(|x| x.at == at)
    {
        let all: Vec<String> = found.iter().map(ToString::to_string).collect();
        report.fail(
            file,
            format!("no violation at {at}; found {}", all.join("; ")),
        );
    }
    report
        .tenant_codes
        .extend(declared.into_iter().map(str::to_owned));
}

/// The shared test file: valid, a test file, and with the tenants consumers rely on.
pub(crate) fn check_test_file(root: &Path, schemas: &Schemas, report: &mut Report) {
    let path = root.join(TEST_FILE);
    let file = match read_json(&path) {
        Ok(f) => f,
        Err(e) => return report.fail(&path, format!("{e:#}")),
    };
    report
        .checked
        .insert("tenants (shared test file)".into(), 1);
    let found = violations(schemas, &file);
    if !found.is_empty() {
        let all: Vec<String> = found.iter().map(ToString::to_string).collect();
        report.fail(&path, format!("invalid: {}", all.join("; ")));
    }
    if file.get("test").is_none() {
        report.fail(&path, "has no test section: it must be marked as test data");
    }
    let ids: BTreeSet<&str> = file["tenants"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|t| t["id"].as_str())
        .collect();
    for id in TEST_TENANTS_REQUIRED {
        if !ids.contains(id) {
            report.fail(&path, format!("lacks tenant {id}"));
        }
    }
}

/// Rule 5 for tenant maps: every rule code has an invalid fixture.
pub(crate) fn coverage(report: &mut Report) {
    for code in RULE_CODES {
        if !report.tenant_codes.contains(code) {
            report
                .errors
                .push(format!("coverage: no tenant_maps fixture for rule {code}"));
        }
    }
}

/// Where each released test-file entry lives: a tenant, number, SIP domain or agent → its tenant.
fn entries(file: &Value) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for t in file["tenants"].as_array().into_iter().flatten() {
        let id = t["id"].as_str().unwrap_or_default().to_owned();
        out.insert(format!("tenant {id}"), id.clone());
        for n in strs(t, "numbers") {
            out.insert(format!("number {n}"), id.clone());
        }
        for d in strs(t, "sip_domains") {
            out.insert(format!("sip_domain {d}"), id.clone());
        }
    }
    for (tenant, ids) in file["test"]["agents"].as_object().into_iter().flatten() {
        for id in ids
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            out.insert(format!("agent {id}"), tenant.clone());
        }
    }
    out
}

/// The break gate for the shared test file: the released copy still validates, and every
/// tenant, number, SIP domain and agent it had is still there, on the same tenant. Consumers'
/// tests name them, so a removal or a move breaks them.
pub(crate) fn break_gate(
    root: &Path,
    schemas: &Schemas,
    released: &Path,
    report: &mut Report,
) -> anyhow::Result<()> {
    let old = read_json(released)?;
    let found = violations(schemas, &old);
    if !found.is_empty() {
        let all: Vec<String> = found.iter().map(ToString::to_string).collect();
        report.fail(
            released,
            format!(
                "break gate: the released test file no longer validates: {}",
                all.join("; ")
            ),
        );
    }
    let now = entries(&read_json(&root.join(TEST_FILE))?);
    for (entry, tenant) in entries(&old) {
        match now.get(&entry) {
            None => report.fail(
                released,
                format!("break gate: {entry} was removed from {TEST_FILE}"),
            ),
            Some(t) if *t != tenant => report.fail(
                released,
                format!("break gate: {entry} moved from tenant {tenant} to {t}"),
            ),
            Some(_) => {}
        }
    }
    Ok(())
}
