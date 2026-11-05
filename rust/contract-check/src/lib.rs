//! Checks parity-contracts' schemas and golden fixtures (ADR 0001 §4.7, rules 1–7).
//!
//! Schema checks, the Kafka cross-field rules, the `violates` match, the gRPC scripts (a strict
//! protobuf parse of every body; for `telephony.v1` the cross-field rules, writer-invalid scripts
//! and the mark lint: TC ADR 0001 §6), coverage, the PII lint and the JSON Schema break gate. The
//! `event_sequence` kind comes at step 9; `routing.v1`'s cross-field rules with G5.

pub mod billing;
pub mod grpc;
pub mod logs;
pub mod routing_ops;
pub mod rules;
pub mod scenarios;
pub mod store;
pub mod tenants;

use anyhow::{Context, Result, anyhow, bail};
use prost_reflect::DescriptorPool;
use serde_json::{Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

const SCHEMA_FILES: [(&str, &str); 31] = [
    // 0.16.0: parity-rs's tenant routing API and topology.
    (
        "routing-requests",
        "schemas/tenant-routing/tenant-routing-requests.v1.json",
    ),
    (
        "routing-replies",
        "schemas/tenant-routing/tenant-routing-replies.v1.json",
    ),
    (
        "routing-events",
        "schemas/tenant-routing/tenant-routing-events.v1.json",
    ),
    (
        "routing-handover",
        "schemas/tenant-routing/tenant-routing-handover.v1.json",
    ),
    (
        "routing-topology",
        "schemas/tenant-routing/topology.v1.json",
    ),
    // 0.13.0: log-record.v1 and the services' log event catalogues.
    ("log-record", "schemas/logging/log-record.v1.json"),
    ("log-catalogue", "schemas/logging/log-catalogue.v1.json"),
    // 0.12.0 (parity-cc-routing phase 5): parity.staffing, queue.events, parity.forecast.
    ("staffing-events", "schemas/events/staffing-events.v1.json"),
    ("queue-events", "schemas/events/queue-events.v1.json"),
    ("forecast-events", "schemas/events/forecast-events.v1.json"),
    // 0.11.0 (parity-cc-routing phases 3 and 4): config.routing, config.routing_pin, agent.events.
    ("config-routing", "schemas/config/config-routing.v1.json"),
    (
        "config-routing-pin",
        "schemas/config/config-routing-pin.v1.json",
    ),
    ("agent-events", "schemas/events/agent-events.v1.json"),
    (
        "billing-requests",
        "schemas/billing/billing-requests.v1.json",
    ),
    ("billing-replies", "schemas/billing/billing-replies.v1.json"),
    ("billing-events", "schemas/billing/billing-events.v1.json"),
    (
        "billing-entitlements",
        "schemas/billing/billing-entitlements.v1.json",
    ),
    (
        "provisioning-requests",
        "schemas/admin/provisioning-requests.v1.json",
    ),
    (
        "provisioning-replies",
        "schemas/admin/provisioning-replies.v1.json",
    ),
    (
        "routing-scenario",
        "schemas/scenarios/routing-scenario.v1.json",
    ),
    ("store-requests", "schemas/store/store-requests.v1.json"),
    ("store-replies", "schemas/store/store-replies.v1.json"),
    ("store-events", "schemas/store/store-events.v1.json"),
    ("store-state", "schemas/store/store-state.v1.json"),
    ("tenant-maps", "schemas/tenants/tenant-maps.v1.json"),
    ("ws-to-gateway", "schemas/agent-ws/to-gateway.v1.json"),
    ("ws-to-desktop", "schemas/agent-ws/to-desktop.v1.json"),
    ("events", "schemas/events/interaction-events.v1.json"),
    ("commands", "schemas/commands/interaction-commands.v1.json"),
    ("replies", "schemas/commands/interaction-replies.v1.json"),
    ("fixture", "schemas/fixtures/fixture.v1.json"),
];
const PII_NAMES: [&str; 5] = ["from", "to", "number", "callback_number", "caller_id"];

/// The compiled schemas, by short name, and the raw documents (for coverage and the PII lint).
pub struct Schemas {
    validators: BTreeMap<&'static str, jsonschema::Validator>,
    docs: BTreeMap<&'static str, Value>,
}

impl Schemas {
    /// Rule 1: every schema is valid against its meta-schema, registered by `$id`, and compiled.
    pub fn load(root: &Path) -> Result<Self> {
        let mut docs = BTreeMap::new();
        for (name, rel) in SCHEMA_FILES {
            let doc = read_json(&root.join(rel))?;
            if let Err(e) = jsonschema::meta::validate(&doc) {
                bail!("{rel}: not a valid 2020-12 schema: {e}");
            }
            docs.insert(name, doc);
        }
        // The reader's view of events (G1): the same schema without the writer-only
        // strictness, since readers ignore fields they don't know.
        let mut reader = docs["events"].clone();
        if let Some(o) = reader.as_object_mut() {
            o.remove("unevaluatedProperties");
            o.insert(
                "$id".into(),
                Value::String(
                    "https://parity.example/schemas/interaction-events/v1-reader.json".into(),
                ),
            );
        }
        docs.insert("events-reader", reader);
        let validators = {
            let mut builder = jsonschema::Registry::new();
            for doc in docs.values() {
                let id = doc["$id"]
                    .as_str()
                    .ok_or_else(|| anyhow!("a schema has no $id"))?;
                builder = builder.add(id, doc)?;
            }
            let registry = builder.prepare()?;
            let mut validators = BTreeMap::new();
            for (name, doc) in &docs {
                let reference = serde_json::json!({ "$ref": doc["$id"] });
                let validator = jsonschema::options()
                    .with_registry(&registry)
                    .offline()
                    .build(&reference)
                    .map_err(|e| anyhow!("compiling {name}: {e}"))?;
                validators.insert(*name, validator);
            }
            validators
        };
        Ok(Self { validators, docs })
    }

    /// The first few schema errors of `instance` against the schema `name`.
    pub fn errors(&self, name: &str, instance: &Value) -> Vec<String> {
        match self.validators.get(name) {
            Some(v) => v
                .iter_errors(instance)
                .take(3)
                .map(|e| format!("{} at {}", e, e.instance_path()))
                .collect(),
            None => vec![format!("no schema named {name}")],
        }
    }

    /// The instance location (a JSON pointer) of every schema error, up to 50.
    pub fn error_paths(&self, name: &str, instance: &Value) -> Vec<String> {
        match self.validators.get(name) {
            Some(v) => v
                .iter_errors(instance)
                .take(50)
                .map(|e| e.instance_path().to_string())
                .collect(),
            None => vec![String::new()],
        }
    }

    /// The `const` values of the `oneOf` branches (event types or commands) and `x-reserved`.
    pub fn names(&self, schema: &str, field: &str) -> (BTreeSet<String>, BTreeSet<String>) {
        let doc = &self.docs[schema];
        let mut live = BTreeSet::new();
        if let Some(defs) = doc["$defs"].as_object() {
            for d in defs.values() {
                if let Some(c) = d["properties"][field]["const"].as_str() {
                    live.insert(c.to_owned());
                }
            }
        }
        let reserved = doc["x-reserved"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect();
        (live, reserved)
    }
}

/// The schema that applies to a topic.
pub fn schema_for(topic: &str) -> Option<&'static str> {
    match topic {
        "interaction.events" => Some("events"),
        "interaction.commands" => Some("commands"),
        t if t.starts_with("replies.") => Some("replies"),
        _ => None,
    }
}

/// What a run found.
#[derive(Default)]
pub struct Report {
    pub checked: BTreeMap<String, usize>,
    pub errors: Vec<String>,
    event_types: BTreeSet<String>,
    commands: BTreeSet<String>,
    reserved_seen_valid: BTreeSet<String>,
    rule_codes: BTreeSet<String>,
    tenant_codes: BTreeSet<String>,
    store_codes: BTreeSet<String>,
    store_seen: BTreeSet<String>,
    billing_codes: BTreeSet<String>,
    billing_seen: BTreeSet<String>,
    routing_codes: BTreeSet<String>,
    routing_seen: BTreeSet<String>,
    log_codes: BTreeSet<String>,
    grpc: grpc::Seen,
}

impl Report {
    pub(crate) fn fail(&mut self, file: &Path, msg: impl Into<String>) {
        self.errors
            .push(format!("{}: {}", file.display(), msg.into()));
    }
}

/// Runs rules 2–6 over `root/fixtures`, rule 7 over `baseline` when given.
pub fn run(root: &Path, fds: &Path, baseline: Option<&Path>) -> Result<Report> {
    let schemas = Schemas::load(root)?;
    let pool = DescriptorPool::decode(
        fs::read(fds)
            .with_context(|| format!("reading {}", fds.display()))?
            .as_slice(),
    )?;
    let mut report = Report::default();
    let masking = logs::Masking::load(root)?;
    for file in json_files(&root.join("fixtures"))? {
        if file.file_name().is_some_and(|n| n == "deferred.json") {
            continue;
        }
        let fixture = match read_json(&file) {
            Ok(v) => v,
            Err(e) => {
                report.fail(&file, format!("{e:#}"));
                continue;
            }
        };
        let errs = schemas.errors("fixture", &fixture);
        if !errs.is_empty() {
            report.fail(
                &file,
                format!("not a valid fixture file: {}", errs.join("; ")),
            );
            continue;
        }
        let dir = file
            .parent()
            .and_then(Path::parent)
            .and_then(Path::file_name)
            .map(|d| d.to_string_lossy().into_owned())
            .unwrap_or_default();
        *report.checked.entry(dir).or_default() += 1;
        match fixture["kind"].as_str() {
            Some("kafka") => check_kafka(&schemas, &file, &fixture, &mut report),
            Some("ws") => check_ws(&schemas, &file, &fixture, &mut report),
            Some("tenant_maps") => tenants::check_fixture(&schemas, &file, &fixture, &mut report),
            Some("store") => store::check_fixture(&schemas, &file, &fixture, &mut report),
            Some("billing") => billing::check_fixture(&schemas, &file, &fixture, &mut report),
            Some("routing_ops") => {
                routing_ops::check_fixture(&schemas, &file, &fixture, &mut report)
            }
            Some("log") => {
                logs::check_fixture(&schemas, root, &masking, &file, &fixture, &mut report)
            }
            _ => grpc::check(&pool, &file, &fixture, &mut report),
        }
    }
    coverage(&schemas, &mut report);
    tenants::check_test_file(root, &schemas, &mut report);
    scenarios::check(root, &schemas, &mut report);
    tenants::coverage(&mut report);
    store::coverage(&schemas, &mut report);
    billing::coverage(&schemas, &mut report);
    billing::check_test_file(root, &schemas, &mut report);
    routing_ops::coverage(&schemas, &mut report);
    routing_ops::check_test_topology(root, &schemas, &mut report);
    logs::coverage(root, &schemas, &mut report);
    let deferred = grpc::deferred(root, &mut report);
    let grpc_gaps = grpc::coverage(&pool, &deferred, &report);
    report.errors.extend(grpc_gaps);
    let cases = read_json(&root.join("masking/cases.v1.json"))?;
    report.errors.extend(grpc::mark_lint(&pool, &cases));
    pii_lint(&schemas, &mut report);
    if let Some(dir) = baseline {
        break_gate(root, &schemas, &pool, dir, &mut report)?;
    }
    Ok(report)
}

/// agent-ws.v1 frames (parity-gateway, 0.3.0): a valid frame passes its direction's schema, an
/// invalid one fails it.
fn check_ws(schemas: &Schemas, file: &Path, f: &Value, report: &mut Report) {
    let schema = match f["direction"].as_str() {
        Some("to_gateway") => "ws-to-gateway",
        _ => "ws-to-desktop",
    };
    let errs = schemas.errors(schema, &f["frame"]);
    match (f["writer_valid"].as_bool(), errs.is_empty()) {
        (Some(true), false) => report.fail(
            file,
            format!("a valid frame fails {schema}: {}", errs.join("; ")),
        ),
        (Some(false), true) => report.fail(file, format!("an invalid frame passes {schema}")),
        _ => {}
    }
}

fn check_kafka(schemas: &Schemas, file: &Path, f: &Value, report: &mut Report) {
    let rec = &f["record"];
    let topic = rec["topic"].as_str().unwrap_or_default();
    let reader = f["reader"].as_str().unwrap_or_default();
    let writer_valid = f["writer_valid"].as_bool().unwrap_or(false);
    let Some(schema) = schema_for(topic) else {
        return report.fail(file, format!("no schema for topic {topic}"));
    };
    let empty = Map::new();
    let headers = rec["headers"].as_object().unwrap_or(&empty);
    let value = rec.get("value");
    let schema_errors = value
        .map(|v| schemas.errors(schema, v))
        .unwrap_or_else(|| vec!["the body isn't JSON".into()]);
    let found = rules::kafka_violations(
        topic,
        rec["key"].as_str().unwrap_or_default(),
        headers,
        value,
        schema_errors.is_empty(),
    );
    let declared: BTreeSet<String> = f["violates"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect();
    if found != declared {
        let detail = if schema_errors.is_empty() {
            String::new()
        } else {
            format!(" (schema: {})", schema_errors.join("; "))
        };
        report.fail(
            file,
            format!("breaks {found:?}, declares {declared:?}{detail}"),
        );
    }
    let known_type = value
        .and_then(|v| v["type"].as_str())
        .is_some_and(|t| schemas.names("events", "type").0.contains(t));
    let tolerant_ok = value.is_some_and(|v| schemas.errors("events-reader", v).is_empty());
    let expected = rules::expected_reader(schema, &found, value, known_type, tolerant_ok);
    if reader != expected {
        report.fail(
            file,
            format!("reader action {reader:?}, but the rules it breaks call for {expected:?}"),
        );
    }
    for code in &declared {
        report
            .rule_codes
            .insert(code.split(':').next().unwrap_or(code).to_owned());
    }
    if writer_valid {
        let names = match schema {
            "events" => Some(("events", "type", &mut report.event_types)),
            "commands" => Some(("commands", "cmd", &mut report.commands)),
            _ => None,
        };
        if let (Some((s, field, seen)), Some(v)) = (names, value)
            && let Some(n) = v[field].as_str()
        {
            seen.insert(n.to_owned());
            if schemas.names(s, field).1.contains(n) {
                report.reserved_seen_valid.insert(n.to_owned());
            }
        }
    }
}

/// Rule 5: everything that must have a fixture has one (the Kafka part; gRPC in `grpc`).
fn coverage(schemas: &Schemas, report: &mut Report) {
    let mut missing = Vec::new();
    for (schema, field, seen) in [
        ("events", "type", &report.event_types),
        ("commands", "cmd", &report.commands),
    ] {
        let (live, _) = schemas.names(schema, field);
        missing.extend(live.difference(seen).map(|n| format!("{field} {n}")));
    }
    missing.extend(
        rules::RULE_CODES
            .iter()
            .filter(|c| !report.rule_codes.contains(**c))
            .map(|c| format!("rule {c}")),
    );
    for m in missing {
        report.errors.push(format!("coverage: no fixture for {m}"));
    }
    let reserved: Vec<String> = report.reserved_seen_valid.iter().cloned().collect();
    for r in reserved {
        report
            .errors
            .push(format!("coverage: reserved name {r} has a valid fixture"));
    }
}

/// Rule 6: phone-number properties carry `x-pii: phone`; no event property does.
fn pii_lint(schemas: &Schemas, report: &mut Report) {
    for (name, doc) in schemas.docs.iter().filter(|(n, _)| **n != "events-reader") {
        let mut found = Vec::new();
        walk_properties(doc, &mut |prop, schema| {
            let marked = schema.get("x-pii").and_then(Value::as_str) == Some("phone");
            if PII_NAMES.contains(&prop) && !marked {
                found.push(format!(
                    "{name}: property {prop} lacks \"x-pii\": \"phone\""
                ));
            }
            if *name == "ws-to-desktop" && marked {
                found.push(format!(
                    "ws-to-desktop: property {prop} is a phone number; no number reaches a desktop"
                ));
            }
            if *name == "events" && marked {
                found.push(format!(
                    "events: property {prop} is a phone number; events never carry numbers"
                ));
            }
        });
        report.errors.extend(found);
    }
}

fn walk_properties(v: &Value, f: &mut dyn FnMut(&str, &Map<String, Value>)) {
    match v {
        Value::Object(map) => {
            if let Some(Value::Object(props)) = map.get("properties") {
                for (k, s) in props {
                    if let Value::Object(s) = s {
                        f(k, s);
                    }
                }
            }
            map.values().for_each(|c| walk_properties(c, f));
        }
        Value::Array(items) => items.iter().for_each(|c| walk_properties(c, f)),
        _ => {}
    }
}

/// Rule 7 (F12): every valid fixture of the last release still validates: a Kafka record against
/// the current schemas, a gRPC script (since 0.2.1) by a strict parse of every body against the
/// current descriptor set, so a removed or renamed field or message fails.
fn break_gate(
    root: &Path,
    schemas: &Schemas,
    pool: &DescriptorPool,
    dir: &Path,
    report: &mut Report,
) -> Result<()> {
    let mut count = 0;
    let options = prost_reflect::DeserializeOptions::new().deny_unknown_fields(true);
    for file in json_files(dir)? {
        let f = read_json(&file)?;
        // The shared test tenants (0.4.0): still valid, nothing removed or moved.
        if file.ends_with(tenants::TEST_FILE) {
            count += 1;
            tenants::break_gate(root, schemas, &file, report)?;
            continue;
        }
        // The shared test entitlements (0.10.0): still valid, nothing removed.
        if file.ends_with(billing::TEST_FILE) {
            count += 1;
            billing::break_gate(root, schemas, &file, report)?;
            continue;
        }
        if f["writer_valid"] != true {
            continue;
        }
        if f["kind"] == "grpc" {
            count += 1;
            for (i, step) in f["steps"].as_array().into_iter().flatten().enumerate() {
                let mut bodies = vec![(step["message"].as_str(), &step["body"])];
                bodies.push((step["expect"]["message"].as_str(), &step["expect"]["body"]));
                for (message, body) in bodies {
                    let Some(message) = message else { continue };
                    let parsed = pool
                        .get_message_by_name(message)
                        .ok_or_else(|| format!("no message {message}"))
                        .and_then(|d| {
                            prost_reflect::DynamicMessage::deserialize_with_options(
                                d,
                                body.clone(),
                                &options,
                            )
                            .map(|_| ())
                            .map_err(|e| e.to_string())
                        });
                    if let Err(e) = parsed {
                        report.fail(
                            &file,
                            format!(
                                "break gate: a released script no longer parses (step {i}): {e}"
                            ),
                        );
                    }
                }
            }
            continue;
        }
        if f["kind"] == "store" {
            count += 1;
            let rec = &f["record"];
            let topic = rec["topic"].as_str().unwrap_or_default();
            let value = &rec["value"];
            let errs = match store::schema_for(topic) {
                Some(_) if value.is_null() && topic == store::STATE_TOPIC => vec![],
                Some(s) => schemas.errors(s, value),
                None => vec![format!("unknown topic {topic}")],
            };
            if !errs.is_empty() {
                report.fail(
                    &file,
                    format!(
                        "break gate: a released store message no longer validates: {}",
                        errs.join("; ")
                    ),
                );
            }
            continue;
        }
        if f["kind"] == "billing" {
            count += 1;
            let rec = &f["record"];
            let topic = rec["topic"].as_str().unwrap_or_default();
            let value = &rec["value"];
            let errs = match billing::schema_for(topic) {
                Some(_) if value.is_null() && topic == billing::ENTITLEMENTS_TOPIC => vec![],
                Some(s) => schemas.errors(s, value),
                None => vec![format!("unknown topic {topic}")],
            };
            if !errs.is_empty() {
                report.fail(
                    &file,
                    format!(
                        "break gate: a released billing message no longer validates: {}",
                        errs.join("; ")
                    ),
                );
            }
            continue;
        }
        if f["kind"] == "routing_ops" {
            // 0.16.0: every released valid tenant routing message still validates.
            count += 1;
            let rec = &f["record"];
            let topic = rec["topic"].as_str().unwrap_or_default();
            let errs = match routing_ops::schema_for(topic) {
                Some(s) => schemas.errors(s, &rec["value"]),
                None => vec![format!("unknown topic {topic}")],
            };
            if !errs.is_empty() {
                report.fail(
                    &file,
                    format!(
                        "break gate: a released tenant routing message no longer validates: {}",
                        errs.join("; ")
                    ),
                );
            }
            continue;
        }
        if f["kind"] == "log" {
            // 0.13.0: every released valid log record still passes log-record.v1.
            count += 1;
            for (i, r) in f["records"].as_array().into_iter().flatten().enumerate() {
                let errs = schemas.errors("log-record", r);
                if !errs.is_empty() {
                    report.fail(
                        &file,
                        format!(
                            "break gate: released log record {i} no longer validates: {}",
                            errs.join("; ")
                        ),
                    );
                }
            }
            continue;
        }
        if f["kind"] == "tenant_maps" {
            count += 1;
            let found = tenants::violations(schemas, &f["file"]);
            if !found.is_empty() {
                let all: Vec<String> = found.iter().map(ToString::to_string).collect();
                report.fail(
                    &file,
                    format!(
                        "break gate: a released tenant maps file no longer validates: {}",
                        all.join("; ")
                    ),
                );
            }
            continue;
        }
        if f["kind"] != "kafka" {
            continue;
        }
        count += 1;
        let topic = f["record"]["topic"].as_str().unwrap_or_default();
        let errs = schema_for(topic)
            .map(|s| schemas.errors(s, &f["record"]["value"]))
            .unwrap_or_else(|| vec![format!("unknown topic {topic}")]);
        if !errs.is_empty() {
            report.fail(
                &file,
                format!(
                    "break gate: a released message no longer validates: {}",
                    errs.join("; ")
                ),
            );
        }
    }
    report.checked.insert("baseline (break gate)".into(), count);
    Ok(())
}

pub(crate) fn read_json(path: &Path) -> Result<Value> {
    let text = fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_str(&text).with_context(|| format!("parsing {}", path.display()))
}

/// Every `.json` file under `dir`, sorted.
pub fn json_files(dir: &Path) -> Result<Vec<PathBuf>> {
    let mut out = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        for entry in fs::read_dir(&d).with_context(|| format!("listing {}", d.display()))? {
            let path = entry?.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().is_some_and(|e| e == "json") {
                out.push(path);
            }
        }
    }
    out.sort();
    Ok(out)
}
