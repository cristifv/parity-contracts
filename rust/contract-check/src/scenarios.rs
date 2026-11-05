//! routing-scenario.v1 (CONVENTIONS "Routing scenarios"; parity-cc-routing PLAN §7.1): every file in
//! `scenarios/routing/` passes the schema and the cross-field rules, and the canonical set exists.

use crate::{Report, Schemas, json_files, read_json};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

/// The canonical scenarios the user asked for (2026-10-28): a quiet day, a busy day, spikes, an
/// outage, a shift start, several tenants at once.
pub const CANONICAL: [&str; 7] = [
    "quiet-day",
    "busy-day",
    "spike-10x-5min",
    "burst-30s",
    "outage-site-50pct",
    "shift-start",
    "multi-tenant-mixed",
];

/// Schema, then the rules a schema can't express: the name is the file's, tenants are the shared
/// test tenants (once each), queue ids unique per tenant, membership and max-wait targets name
/// existing queues, events and KPIs name declared tenants (and sites, queues), events fall inside
/// the scenario.
pub fn check(root: &Path, schemas: &Schemas, report: &mut Report) {
    let dir = root.join("scenarios/routing");
    let files = match json_files(&dir) {
        Ok(f) => f,
        Err(e) => {
            report.fail(&dir, format!("{e:#}"));
            return;
        }
    };
    let shared: BTreeSet<String> = read_json(&root.join("tenants/test-tenants.v1.json"))
        .ok()
        .and_then(|t| {
            t["test"]["agents"]
                .as_object()
                .map(|o| o.keys().cloned().collect())
        })
        .unwrap_or_default();
    let mut names = BTreeSet::new();
    for file in files {
        let doc = match read_json(&file) {
            Ok(v) => v,
            Err(e) => {
                report.fail(&file, format!("{e:#}"));
                continue;
            }
        };
        let errs = schemas.errors("routing-scenario", &doc);
        if !errs.is_empty() {
            report.fail(
                &file,
                format!("not a valid routing-scenario.v1: {}", errs.join("; ")),
            );
            continue;
        }
        *report.checked.entry("scenarios".into()).or_default() += 1;
        for e in cross_field(&doc, &shared) {
            report.fail(&file, e);
        }
        let stem = file
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        if doc["name"].as_str() != Some(stem.as_str()) {
            report.fail(&file, "`name` must equal the file name");
        }
        names.insert(stem);
    }
    for c in CANONICAL {
        if !names.contains(c) {
            report.fail(&dir, format!("the canonical scenario `{c}` is missing"));
        }
    }
    // Phase 5b+5c (parity-cc-routing PLAN §3.8, §5.9): the learned forecaster's training catalogue
    // and its held-out OOD suite are routing-scenario.v1 files too, each with its `forecast.purpose`.
    // The leakage check between them is scripts/forecast_scenarios.py's (ci.sh).
    for (sub, purpose) in [
        ("scenarios/forecast-training", "training_catalogue"),
        ("scenarios/forecast-ood", "ood"),
    ] {
        let dir = root.join(sub);
        let files = match json_files(&dir) {
            Ok(f) => f,
            Err(e) => {
                report.fail(&dir, format!("{e:#}"));
                continue;
            }
        };
        if files.is_empty() {
            report.fail(&dir, "no scenario files");
        }
        for file in files {
            let doc = match read_json(&file) {
                Ok(v) => v,
                Err(e) => {
                    report.fail(&file, format!("{e:#}"));
                    continue;
                }
            };
            let errs = schemas.errors("routing-scenario", &doc);
            if !errs.is_empty() {
                report.fail(
                    &file,
                    format!("not a valid routing-scenario.v1: {}", errs.join("; ")),
                );
                continue;
            }
            *report
                .checked
                .entry("forecast scenarios".into())
                .or_default() += 1;
            for e in cross_field(&doc, &shared) {
                report.fail(&file, e);
            }
            for e in forecast_rules(&doc, purpose) {
                report.fail(&file, e);
            }
            let stem = file
                .file_stem()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default();
            if doc["name"].as_str() != Some(stem.as_str()) {
                report.fail(&file, "`name` must equal the file name");
            }
        }
    }
}

/// The `forecast` block's rules a schema can't express: the purpose of its directory, an OOD
/// window inside the scenario, data faults naming the tenant's queues.
pub fn forecast_rules(doc: &Value, purpose: &str) -> Vec<String> {
    let mut out = Vec::new();
    let empty = Vec::new();
    let f = &doc["forecast"];
    if f["purpose"].as_str() != Some(purpose) {
        out.push(format!("`forecast.purpose` must be `{purpose}` here"));
        return out;
    }
    let duration = doc["duration_s"].as_f64().unwrap_or(0.0);
    if purpose == "ood" {
        let onset = f["ood"]["onset_s"].as_f64().unwrap_or(0.0);
        let end = f["ood"]["end_s"].as_f64().unwrap_or(onset);
        if onset >= duration || end > duration || end < onset {
            out.push("`forecast.ood` window must lie inside the scenario".into());
        }
    }
    if purpose == "training_catalogue" && !f["patterns"].is_object() {
        out.push("a training catalogue needs `forecast.patterns`".into());
    }
    let queues: BTreeSet<&str> = doc["tenants"]
        .as_array()
        .unwrap_or(&empty)
        .iter()
        .flat_map(|t| t["queues"].as_array().unwrap_or(&empty).iter())
        .filter_map(|q| q["id"].as_str())
        .collect();
    for d in f["data_faults"].as_array().unwrap_or(&empty) {
        let named = d["queue"].as_str().into_iter().chain(
            d["queues"]
                .as_array()
                .unwrap_or(&empty)
                .iter()
                .filter_map(Value::as_str),
        );
        for q in named {
            if !queues.contains(q) {
                out.push(format!("data fault names unknown queue `{q}`"));
            }
        }
        if d["at_s"].as_f64().unwrap_or(0.0) > duration {
            out.push("data fault after the scenario's end".into());
        }
    }
    out
}

/// A loop in a tenant's `queue` max-wait actions (queue -> its target), e.g. `sales -> support ->
/// sales`: a caller would move forever and never get a terminal action (CR-057). The first loop
/// found, as `a -> b -> a`.
pub fn max_wait_cycle(next: &BTreeMap<&str, &str>) -> Option<String> {
    for start in next.keys() {
        let mut path = vec![*start];
        let mut at = *start;
        while let Some(n) = next.get(at) {
            if let Some(i) = path.iter().position(|p| p == n) {
                let mut cycle: Vec<&str> = path[i..].to_vec();
                cycle.push(n);
                return Some(cycle.join(" -> "));
            }
            path.push(n);
            at = n;
        }
    }
    None
}

/// The cross-field rules of one scenario (already schema-valid).
pub fn cross_field(doc: &Value, shared: &BTreeSet<String>) -> Vec<String> {
    let mut out = Vec::new();
    let empty = Vec::new();
    let duration = doc["duration_s"].as_f64().unwrap_or(0.0);
    let mut tenants = BTreeSet::new();
    let mut sites: BTreeSet<(String, String)> = BTreeSet::new();
    let mut queues: BTreeSet<(String, String)> = BTreeSet::new();
    for t in doc["tenants"].as_array().unwrap_or(&empty) {
        let id = t["id"].as_str().unwrap_or_default().to_owned();
        if !shared.contains(&id) {
            out.push(format!(
                "tenant `{id}` is not one of the shared test tenants with agents"
            ));
        }
        if !tenants.insert(id.clone()) {
            out.push(format!("tenant `{id}` twice"));
        }
        let mut qids = BTreeSet::new();
        for q in t["queues"].as_array().unwrap_or(&empty) {
            let qid = q["id"].as_str().unwrap_or_default().to_owned();
            if !qids.insert(qid.clone()) {
                out.push(format!("{id}: queue `{qid}` twice"));
            }
            queues.insert((id.clone(), qid));
        }
        let next: BTreeMap<&str, &str> = t["queues"]
            .as_array()
            .unwrap_or(&empty)
            .iter()
            .filter(|q| q["max_wait_action"]["action"] == "queue")
            .filter_map(|q| {
                Some((
                    q["id"].as_str()?,
                    q["max_wait_action"]["queue_id"].as_str()?,
                ))
            })
            .collect();
        if let Some(cycle) = max_wait_cycle(&next) {
            out.push(format!(
                "{id}: max_wait_action queue chain {cycle} never ends in a terminal action (CR-057)"
            ));
        }
        for q in t["queues"].as_array().unwrap_or(&empty) {
            let a = &q["max_wait_action"];
            match a["action"].as_str() {
                Some("queue") => {
                    let target = a["queue_id"].as_str().unwrap_or_default();
                    if !qids.contains(target) || Some(target) == q["id"].as_str() {
                        out.push(format!("{id}: max_wait_action queue `{target}` must be another queue of the tenant"));
                    }
                }
                Some("flow_step") if a["flow_step"].as_str().is_none() => {
                    out.push(format!("{id}: max_wait_action flow_step needs `flow_step`"));
                }
                _ => {}
            }
            let shares: f64 = q["priority_mix"]
                .as_array()
                .unwrap_or(&empty)
                .iter()
                .filter_map(|m| m["share"].as_f64())
                .sum();
            if shares > 1.0 + 1e-9 {
                out.push(format!("{id}: priority_mix shares add up to more than 1"));
            }
        }
        let mut groups: Vec<&Value> = t["agents"].as_array().unwrap_or(&empty).iter().collect();
        for e in doc["events"].as_array().unwrap_or(&empty) {
            if e["kind"] == "mass_login" && e["tenant"].as_str() == Some(id.as_str()) {
                groups.push(&e["group"]);
            }
        }
        for g in groups {
            for q in g["queues"].as_array().unwrap_or(&empty) {
                if !qids.contains(q.as_str().unwrap_or_default()) {
                    out.push(format!("{id}: an agent group names unknown queue {q}"));
                }
            }
            let ids = g["ids"].as_array().map_or(0, Vec::len) as u64;
            if ids > g["count"].as_u64().unwrap_or(0) {
                out.push(format!(
                    "{id}: an agent group lists more ids than its count"
                ));
            }
            if ids < g["count"].as_u64().unwrap_or(0) && g["id_prefix"].as_str().is_none() {
                out.push(format!(
                    "{id}: an agent group needs `id_prefix` for agents beyond its ids"
                ));
            }
            if let Some(s) = g["site"].as_str() {
                sites.insert((id.clone(), s.to_owned()));
            }
        }
    }
    for e in doc["events"].as_array().unwrap_or(&empty) {
        let kind = e["kind"].as_str().unwrap_or_default();
        if e["at_s"].as_f64().unwrap_or(0.0) > duration {
            out.push(format!("event `{kind}` after the scenario's end"));
        }
        let tenant = e["tenant"].as_str();
        if let Some(t) = tenant
            && !tenants.contains(t)
        {
            out.push(format!("event `{kind}` names undeclared tenant `{t}`"));
        }
        let needs_tenant = matches!(
            kind,
            "mass_login" | "mass_logout" | "supervisor_pin" | "queue_config" | "request_flood"
        );
        if needs_tenant && tenant.is_none() {
            out.push(format!("event `{kind}` needs `tenant`"));
        }
        if kind == "mass_login" && !e["group"].is_object() {
            out.push("event `mass_login` needs `group`".into());
        }
        if let (Some(t), Some(s)) = (tenant, e["site"].as_str())
            && !sites.contains(&(t.to_owned(), s.to_owned()))
        {
            out.push(format!("event `{kind}` names unknown site `{s}`"));
        }
        if matches!(kind, "queue_config" | "request_flood")
            && let Some(t) = tenant
            && !queues.contains(&(
                t.to_owned(),
                e["queue"].as_str().unwrap_or_default().to_owned(),
            ))
        {
            out.push(format!("event `{kind}` names an unknown queue"));
        }
    }
    for k in doc["expectations"]["kpi"].as_array().unwrap_or(&empty) {
        if let Some(t) = k["tenant"].as_str() {
            if !tenants.contains(t) {
                out.push(format!("kpi names undeclared tenant `{t}`"));
            }
            if let Some(q) = k["queue"].as_str()
                && !queues.contains(&(t.to_owned(), q.to_owned()))
            {
                out.push(format!("kpi names unknown queue `{q}`"));
            }
        }
    }
    out
}
