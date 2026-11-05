//! gRPC scripts: the strict parse of every body, the per-package rules, coverage and the mark lint
//! (TC ADR 0001 §6, revision 1). `parity.telephony.v1` gets the cross-field rules, writer-invalid
//! scripts and the mark lint; `parity.routing.v1` (G5) gets its writer rules on every call, and a
//! script with `given` is executable: declared streams, rising `delivery_seq`, a consistent world.

use crate::Report;
use prost_reflect::{DescriptorPool, DeserializeOptions, DynamicMessage, FieldDescriptor, Kind};
use serde_json::{Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

pub const ROUTING: &str = "parity.routing.v1";
/// parity-gateway ↔ routing-engine (0.3.0).
pub const AGENTS: &str = "parity.agents.v1";
/// parity-gateway → interaction-engine (0.3.0).
pub const INTERACTION: &str = "parity.interaction.v1";
pub const TELEPHONY: &str = "parity.telephony.v1";

/// The service of each package.
pub fn service_of(package: &str) -> Option<&'static str> {
    match package {
        ROUTING => Some("parity.routing.v1.RoutingService"),
        TELEPHONY => Some("parity.telephony.v1.TelephonyService"),
        AGENTS => Some("parity.agents.v1.AgentService"),
        INTERACTION => Some("parity.interaction.v1.InteractionControlService"),
        _ => None,
    }
}

/// Every gRPC rule code a writer-invalid script may declare (closed list, §6).
pub const GRPC_RULE_CODES: [&str; 6] = [
    "tenant_mismatch",
    "missing_owner",
    "bad_epoch",
    "bad_command_id",
    "bad_uuid",
    "bad_sip_status",
];

/// RPCs that carry no tenant: they span tenants.
const TENANTLESS: [&str; 2] = ["Watch", "Acknowledge"];
/// RPCs whose request has no `owner` (nothing claimed, or read-only).
const OWNERLESS: [&str; 4] = ["Watch", "Acknowledge", "GetSession", "DeclineArrival"];
/// Watch messages about the node rather than a session: `delivery_seq` 0, no `session`.
const NODE_LEVEL: [&str; 2] = ["node_status", "heartbeat"];

/// The `parity-error` a correct server answers to a call breaking `violates` (as G1 does for
/// Kafka reader actions): the tenant rule wins, every other rule is `invalid_request`.
pub fn expected_error(violates: &BTreeSet<String>) -> (&'static str, &'static str) {
    if violates.contains("tenant_mismatch") {
        ("INVALID_ARGUMENT", "tenant_mismatch")
    } else {
        ("INVALID_ARGUMENT", "invalid_request")
    }
}

/// The telephony writer rules a call breaks (§4.2): what a correct IE never sends.
pub fn call_violations(
    method: &str,
    body: &Value,
    metadata: &Map<String, Value>,
) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    let rpc = method.rsplit('/').next().unwrap_or_default();
    let owner = body.get("owner");
    if !TENANTLESS.contains(&rpc) {
        let body_tenant = owner
            .and_then(|o| o.get("tenant_id"))
            .or_else(|| body.get("tenant_id"))
            .and_then(Value::as_str);
        let meta_tenant = metadata.get("tenant-id").and_then(Value::as_str);
        if meta_tenant.is_none() || body_tenant != meta_tenant {
            out.insert("tenant_mismatch".to_owned());
        }
    }
    if !OWNERLESS.contains(&rpc) {
        match owner {
            None => {
                out.insert("missing_owner".to_owned());
            }
            Some(o) => {
                let epoch = o.get("owner_epoch").and_then(Value::as_str).unwrap_or("0");
                if epoch == "0" {
                    out.insert("bad_epoch".to_owned());
                }
            }
        }
        if rpc != "ClaimSession"
            && !body
                .get("command_id")
                .and_then(Value::as_str)
                .is_some_and(is_command_id)
        {
            out.insert("bad_command_id".to_owned());
        }
    }
    let uuids = [
        owner.and_then(|o| o.get("interaction_id")),
        body.get("interaction_id"),
        body.get("leg_id"),
        body.get("play_id"),
        body.get("recording_id"),
        body.get("collection_id"),
    ];
    if uuids
        .into_iter()
        .flatten()
        .any(|v| !v.as_str().is_some_and(crate::rules::is_uuid))
    {
        out.insert("bad_uuid".to_owned());
    }
    let status = body.get("sip_status").and_then(Value::as_u64);
    let bad_status = match rpc {
        "RejectLeg" => !status.is_some_and(|s| [404, 480, 486, 503, 603].contains(&s)),
        "DeclineArrival"
            if body.get("reason").and_then(Value::as_str) == Some("DECLINE_REASON_REFUSED") =>
        {
            !status.is_some_and(|s| [403, 404, 480, 503, 603].contains(&s))
        }
        _ => false,
    };
    if bad_status {
        out.insert("bad_sip_status".to_owned());
    }
    out
}

fn is_command_id(s: &str) -> bool {
    (1..=200).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".:_-".contains(&b))
}

/// RPCs whose request has no `owner` in routing.v1 (they span interactions).
const ROUTING_OWNERLESS: [&str; 2] = ["Watch", "Acknowledge"];
/// Routing Watch messages never replayed: `delivery_seq` 0 (0.14.0: a lent agent's availability,
/// the latest wins).
const ROUTING_UNSEQUENCED: [&str; 3] = ["heartbeat", "position", "agent_availability"];

/// The routing writer rules a call breaks (G5): what a correct IE never sends.
pub fn routing_violations(
    method: &str,
    body: &Value,
    metadata: &Map<String, Value>,
) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    let rpc = method.rsplit('/').next().unwrap_or_default();
    if ROUTING_OWNERLESS.contains(&rpc) {
        return out;
    }
    let owner = body.get("owner");
    let body_tenant = owner
        .and_then(|o| o.get("tenant_id"))
        .and_then(Value::as_str);
    let meta_tenant = metadata.get("tenant-id").and_then(Value::as_str);
    if meta_tenant.is_none() || body_tenant != meta_tenant {
        out.insert("tenant_mismatch".to_owned());
    }
    match owner {
        None => {
            out.insert("missing_owner".to_owned());
        }
        Some(o) => {
            let epoch = o.get("owner_epoch").and_then(Value::as_str).unwrap_or("0");
            if epoch == "0" {
                out.insert("bad_epoch".to_owned());
            }
        }
    }
    let uuids = [
        owner.and_then(|o| o.get("interaction_id")),
        body.get("entry_id"),
        body.get("reservation_id"),
        body.get("recording_id"),
    ];
    if uuids
        .into_iter()
        .flatten()
        .any(|v| !v.as_str().is_some_and(crate::rules::is_uuid))
    {
        out.insert("bad_uuid".to_owned());
    }
    out
}

/// The package a script is in: declared, or routing for the 0.1.0 scripts.
fn package_of(f: &Value) -> &str {
    f["package"].as_str().unwrap_or(ROUTING)
}

/// What one script contributes to coverage.
#[derive(Default)]
pub struct Seen {
    /// Message full names parsed in a valid script.
    pub messages: BTreeSet<String>,
    /// `<package>.WatchResponse.<case>` delivered in a valid script.
    pub watch_cases: BTreeSet<String>,
    /// `parity-error` values expected in any script.
    pub errors: BTreeSet<String>,
    /// gRPC rule codes declared by writer-invalid scripts.
    pub rule_codes: BTreeSet<String>,
}

/// Checks one gRPC script.
pub fn check(pool: &DescriptorPool, file: &Path, f: &Value, report: &mut Report) {
    let package = package_of(f);
    let Some(service) = service_of(package).and_then(|s| pool.get_service_by_name(s)) else {
        return report.fail(
            file,
            format!("{package} has no service in the descriptor set"),
        );
    };
    let writer_valid = f["writer_valid"].as_bool().unwrap_or(true);
    let streams = f["streams"].as_object();
    let peers = f["peers"].as_object();
    let options = DeserializeOptions::new().deny_unknown_fields(true);
    let steps: Vec<&Value> = f["steps"].as_array().into_iter().flatten().collect();
    let last_call = steps.iter().rposition(|s| s["op"] == "call");
    let mut seqs: BTreeMap<(String, String, String), BTreeMap<u64, Value>> = BTreeMap::new();
    let executable = package == ROUTING && f.get("given").is_some();
    if executable {
        check_routing_world(file, f, report);
    }
    for (i, step) in steps.iter().enumerate() {
        for (key, declared) in [("stream", streams), ("peer", peers)] {
            if let Some(label) = step[key].as_str()
                && !declared.is_some_and(|d| d.contains_key(label))
            {
                report.fail(file, format!("step {i}: {key} {label} isn't declared"));
            }
        }
        if package == TELEPHONY
            && matches!(step["op"].as_str(), Some("call" | "deliver"))
            && step["stream"].is_null()
        {
            report.fail(file, format!("step {i}: a telephony step names its stream"));
        }
        let is_watch = step["method"]
            .as_str()
            .is_some_and(|m| m.ends_with("/Watch"));
        if package == ROUTING
            && executable
            && (step["op"] == "deliver" || is_watch)
            && step["stream"].is_null()
        {
            report.fail(
                file,
                format!("step {i}: an executable routing script's Watch and deliveries name their stream"),
            );
        }
        let op = step["op"].as_str().unwrap_or_default();
        if op == "advance" || op == "inject" {
            continue;
        }
        let mut bodies = vec![(step["message"].as_str().unwrap_or_default(), &step["body"])];
        if let Some(method) = step["method"].as_str() {
            let name = method.rsplit('/').next().unwrap_or_default();
            if !method.starts_with(&format!("{}/", service.full_name())) {
                report.fail(file, format!("step {i}: {method} isn't in {package}"));
            }
            match service.methods().find(|m| m.name() == name) {
                None => report.fail(file, format!("step {i}: no method {name}")),
                Some(m) => {
                    if m.input().full_name() != bodies[0].0 {
                        report.fail(
                            file,
                            format!("step {i}: {name} takes {}", m.input().full_name()),
                        );
                    }
                    if let Some(out) = step["expect"]["message"].as_str() {
                        if m.output().full_name() != out {
                            report.fail(
                                file,
                                format!("step {i}: {name} returns {}", m.output().full_name()),
                            );
                        }
                        bodies.push((out, &step["expect"]["body"]));
                    }
                }
            }
            if let Some(code) = step["expect"]["parity_error"].as_str() {
                report.grpc.errors.insert(format!("{package}:{code}"));
            }
            if package == TELEPHONY || package == ROUTING {
                let empty = Map::new();
                let meta = step["metadata"].as_object().unwrap_or(&empty);
                let found = if package == TELEPHONY {
                    call_violations(method, &step["body"], meta)
                } else {
                    routing_violations(method, &step["body"], meta)
                };
                let is_last = Some(i) == last_call;
                if writer_valid || !is_last {
                    if !found.is_empty() {
                        report.fail(file, format!("step {i}: breaks the gRPC rules {found:?}"));
                    }
                } else {
                    check_writer_invalid(file, f, step, &found, report);
                }
            }
        } else if op == "deliver" {
            let expected = format!("{package}.WatchResponse");
            if bodies[0].0 != expected {
                report.fail(
                    file,
                    format!("step {i}: a {package} script delivers {expected}"),
                );
            }
            if package == TELEPHONY {
                check_delivery(file, i, step, &mut seqs, report);
            } else if executable {
                check_routing_delivery(file, i, step, &mut seqs, report);
            }
        }
        for (message, body) in bodies {
            if message.is_empty() {
                continue;
            }
            if !message.starts_with(&format!("{package}.")) {
                report.fail(file, format!("step {i}: {message} isn't in {package}"));
            }
            let Some(desc) = pool.get_message_by_name(message) else {
                report.fail(file, format!("step {i}: no message {message}"));
                continue;
            };
            if let Err(e) = DynamicMessage::deserialize_with_options(desc, body.clone(), &options) {
                report.fail(file, format!("step {i}: {message}: {e}"));
            }
            if writer_valid {
                report.grpc.messages.insert(message.to_owned());
                if message.ends_with(".WatchResponse") {
                    for key in body.as_object().into_iter().flatten().map(|(k, _)| k) {
                        report.grpc.watch_cases.insert(format!("{message}.{key}"));
                    }
                }
            }
        }
    }
    let is_race = file
        .file_name()
        .is_some_and(|n| n.to_string_lossy().starts_with("race-"));
    if (package == TELEPHONY || package == ROUTING) && is_race {
        let last = steps.last();
        if !last.is_some_and(|s| s["op"] == "call" && s.get("expect").is_some()) {
            report.fail(
                file,
                "a race script ends with a call whose expect pins the outcome",
            );
        }
    }
}

fn check_writer_invalid(
    file: &Path,
    f: &Value,
    step: &Value,
    found: &BTreeSet<String>,
    report: &mut Report,
) {
    let declared: BTreeSet<String> = f["violates"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect();
    if *found != declared {
        report.fail(
            file,
            format!("the last call breaks {found:?}, declares {declared:?}"),
        );
    }
    let (status, code) = expected_error(&declared);
    if step["expect"]["status"] != status || step["expect"]["parity_error"] != code {
        report.fail(
            file,
            format!(
                "the rules it breaks call for {status} / {code}, the script expects {}",
                step["expect"]
            ),
        );
    }
    report.grpc.rule_codes.extend(declared);
}

/// `delivery_seq` 0 and no `session` exactly for node-level messages; per stream, session and
/// incarnation the sequence rises, except a replay identical to an earlier message.
fn check_delivery(
    file: &Path,
    i: usize,
    step: &Value,
    seqs: &mut BTreeMap<(String, String, String), BTreeMap<u64, Value>>,
    report: &mut Report,
) {
    let body = &step["body"];
    let case = body
        .as_object()
        .into_iter()
        .flatten()
        .map(|(k, _)| k.as_str())
        .find(|k| !["incarnation", "node_id", "delivery_seq", "session", "at_ms"].contains(k))
        .unwrap_or_default();
    let node_level = NODE_LEVEL.contains(&case);
    let seq: u64 = body["delivery_seq"]
        .as_str()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let has_session = body.get("session").is_some();
    if node_level != (seq == 0) || node_level == has_session {
        report.fail(
            file,
            format!("step {i}: {case} has delivery_seq {seq} and session {has_session}"),
        );
        return;
    }
    if node_level {
        return;
    }
    let key = (
        step["stream"].as_str().unwrap_or_default().to_owned(),
        format!(
            "{}/{}",
            body["session"]["tenant_id"], body["session"]["interaction_id"]
        ),
        body["incarnation"].as_str().unwrap_or_default().to_owned(),
    );
    let seen = seqs.entry(key).or_default();
    match seen.get(&seq) {
        Some(earlier) => {
            let epoch = |b: &Value| -> u64 {
                b[case]["target"]["owner_epoch"]
                    .as_str()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0)
            };
            let unfenced = |b: &Value| -> Value {
                let mut b = b.clone();
                if let Some(t) = b[case]["target"].as_object_mut() {
                    t.remove("owner_epoch");
                }
                b
            };
            if unfenced(earlier) != unfenced(body) {
                report.fail(
                    file,
                    format!("step {i}: delivery_seq {seq} replayed with another body"),
                );
            } else if epoch(body) < epoch(earlier) {
                report.fail(
                    file,
                    format!("step {i}: delivery_seq {seq} replayed with an older owner_epoch"),
                );
            } else {
                // A re-stamped replay: later replays compare with the newest fence.
                seen.insert(seq, body.clone());
            }
        }
        None => {
            if seen.keys().next_back().is_some_and(|max| *max > seq) {
                report.fail(file, format!("step {i}: delivery_seq {seq} goes back"));
            }
            seen.insert(seq, body.clone());
        }
    }
}

/// An executable routing script's world (G5): streams in `given.bin`; queues and agents of the
/// bin's tenants; each agent's queues its tenant's; injected agents and every call's tenant in it.
fn check_routing_world(file: &Path, f: &Value, report: &mut Report) {
    let given = &f["given"];
    let strs = |v: &Value| -> BTreeSet<String> {
        v.as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect()
    };
    let tenants = strs(&given["tenants"]);
    let bin = given["bin"].as_str().unwrap_or_default();
    for (label, s) in f["streams"].as_object().into_iter().flatten() {
        if s["bin"].as_str() != Some(bin) {
            report.fail(file, format!("stream {label} isn't in the given bin {bin}"));
        }
    }
    let mut queues = BTreeSet::new();
    for q in given["queues"].as_array().into_iter().flatten() {
        let tenant = q["tenant_id"].as_str().unwrap_or_default();
        if !tenants.contains(tenant) {
            report.fail(file, format!("queue of {tenant}, not a tenant of the bin"));
        }
        queues.insert(format!(
            "{tenant}/{}",
            q["queue_id"].as_str().unwrap_or_default()
        ));
    }
    // 0.9.0: a given queue's max-wait action. QUEUE names another given queue of the tenant and the
    // chain ends in a terminal action (CR-057); FLOW_STEP names its step.
    let mut next: BTreeMap<String, BTreeMap<&str, &str>> = BTreeMap::new();
    for q in given["queues"].as_array().into_iter().flatten() {
        let tenant = q["tenant_id"].as_str().unwrap_or_default();
        let id = q["queue_id"].as_str().unwrap_or_default();
        match q["max_wait_action"].as_str() {
            Some("OVERFLOW_ACTION_QUEUE") => {
                let target = q["max_wait_queue_id"].as_str().unwrap_or_default();
                if target == id || !queues.contains(&format!("{tenant}/{target}")) {
                    report.fail(
                        file,
                        format!("queue {tenant}/{id}: max_wait_queue_id must be another given queue of {tenant}"),
                    );
                }
                next.entry(tenant.to_owned())
                    .or_default()
                    .insert(id, target);
            }
            Some("OVERFLOW_ACTION_FLOW_STEP") if q["max_wait_flow_step"].as_str().is_none() => {
                report.fail(
                    file,
                    format!("queue {tenant}/{id}: FLOW_STEP needs max_wait_flow_step"),
                );
            }
            _ => {}
        }
    }
    for (tenant, chain) in &next {
        if let Some(cycle) = crate::scenarios::max_wait_cycle(chain) {
            report.fail(
                file,
                format!("{tenant}: max-wait queue chain {cycle} never ends in a terminal action (CR-057)"),
            );
        }
    }
    let mut agents = BTreeSet::new();
    for a in given["agents"].as_array().into_iter().flatten() {
        let tenant = a["tenant_id"].as_str().unwrap_or_default();
        let id = a["agent_id"].as_str().unwrap_or_default();
        if !tenants.contains(tenant) {
            report.fail(
                file,
                format!("agent {tenant}/{id}: not a tenant of the bin"),
            );
        }
        for q in strs(&a["queues"]) {
            if !queues.contains(&format!("{tenant}/{q}")) {
                report.fail(
                    file,
                    format!("agent {tenant}/{id} serves {q}, not a queue of {tenant}"),
                );
            }
        }
        if !agents.insert(format!("{tenant}/{id}")) {
            report.fail(file, format!("agent {tenant}/{id} is given twice"));
        }
    }
    for (i, step) in f["steps"].as_array().into_iter().flatten().enumerate() {
        if step["op"] == "inject"
            && step["what"]
                .as_str()
                .is_some_and(|w| w.starts_with("agent_") || w == "supervisor_force")
        {
            let a = &step["args"];
            let key = format!(
                "{}/{}",
                a["tenant_id"].as_str().unwrap_or_default(),
                a["agent_id"].as_str().unwrap_or_default()
            );
            if !agents.contains(&key) {
                report.fail(file, format!("step {i}: agent {key} isn't given"));
            }
        }
        if step["op"] == "call"
            && let Some(t) = step["body"]["owner"]["tenant_id"].as_str()
            && !tenants.contains(t)
        {
            report.fail(file, format!("step {i}: tenant {t} isn't in the given bin"));
        }
        if step["op"] == "inject"
            && step["what"] == "stream_drop"
            && let Some(label) = step["args"]["stream"].as_str()
            && f["streams"].get(label).is_none()
        {
            report.fail(
                file,
                format!("step {i}: stream_drop of undeclared stream {label}"),
            );
        }
        if step["op"] == "inject"
            && !matches!(
                step["what"].as_str(),
                Some("agent_ready" | "agent_answers" | "stream_drop" | "supervisor_force")
            )
        {
            report.fail(
                file,
                format!("step {i}: {} isn't a routing stimulus", step["what"]),
            );
        }
    }
}

/// Routing deliveries (G5): `delivery_seq` 0 exactly for `Heartbeat`, `PositionUpdate` and (0.14.0)
/// `AgentAvailability`; per
/// stream and interaction the sequence rises, except a replay: identical to the earlier message
/// but for `target.owner_epoch`, which carries RE's current fence and never falls (0.9.0, CR-054).
fn check_routing_delivery(
    file: &Path,
    i: usize,
    step: &Value,
    seqs: &mut BTreeMap<(String, String, String), BTreeMap<u64, Value>>,
    report: &mut Report,
) {
    let body = &step["body"];
    let case = body
        .as_object()
        .into_iter()
        .flatten()
        .map(|(k, _)| k.as_str())
        .find(|k| !["leader_epoch", "delivery_seq"].contains(k))
        .unwrap_or_default();
    let seq: u64 = body["delivery_seq"]
        .as_str()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let unsequenced = ROUTING_UNSEQUENCED.contains(&case);
    if unsequenced != (seq == 0) {
        report.fail(file, format!("step {i}: {case} has delivery_seq {seq}"));
        return;
    }
    if unsequenced {
        return;
    }
    let event = &body[case];
    let (tenant, interaction) = if event.get("target").is_some() {
        (
            &event["target"]["tenant_id"],
            &event["target"]["interaction_id"],
        )
    } else {
        (&event["tenant_id"], &event["interaction_id"])
    };
    let key = (
        step["stream"].as_str().unwrap_or_default().to_owned(),
        format!("{tenant}/{interaction}"),
        String::new(),
    );
    let seen = seqs.entry(key).or_default();
    match seen.get(&seq) {
        Some(earlier) => {
            let epoch = |b: &Value| -> u64 {
                b[case]["target"]["owner_epoch"]
                    .as_str()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0)
            };
            let unfenced = |b: &Value| -> Value {
                let mut b = b.clone();
                if let Some(t) = b[case]["target"].as_object_mut() {
                    t.remove("owner_epoch");
                }
                b
            };
            if unfenced(earlier) != unfenced(body) {
                report.fail(
                    file,
                    format!("step {i}: delivery_seq {seq} replayed with another body"),
                );
            } else if epoch(body) < epoch(earlier) {
                report.fail(
                    file,
                    format!("step {i}: delivery_seq {seq} replayed with an older owner_epoch"),
                );
            } else {
                // A re-stamped replay: later replays compare with the newest fence.
                seen.insert(seq, body.clone());
            }
        }
        None => {
            if seen.keys().next_back().is_some_and(|max| *max > seq) {
                report.fail(file, format!("step {i}: delivery_seq {seq} goes back"));
            }
            seen.insert(seq, body.clone());
        }
    }
}

/// The names `fixtures/telephony.v1/deferred.json` exempts from coverage (`kind:name`).
pub fn deferred(root: &Path, report: &mut Report) -> BTreeSet<String> {
    let file = root.join("fixtures/telephony.v1/deferred.json");
    let Ok(text) = std::fs::read_to_string(&file) else {
        return BTreeSet::new();
    };
    match serde_json::from_str::<Value>(&text) {
        Ok(v) => v["deferred"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|d| Some(format!("{}:{}", d["kind"].as_str()?, d["name"].as_str()?)))
            .collect(),
        Err(e) => {
            report.fail(&file, format!("not JSON: {e}"));
            BTreeSet::new()
        }
    }
}

/// Coverage per package (§6): routing keeps step 0's; telephony adds the error codes and the
/// writer rules, minus the deferred names, and a deferred name with a script fails.
pub fn coverage(
    pool: &DescriptorPool,
    deferred: &BTreeSet<String>,
    report: &Report,
) -> Vec<String> {
    let seen = &report.grpc;
    let mut missing = Vec::new();
    let mut early = Vec::new();
    for package in [ROUTING, TELEPHONY, AGENTS, INTERACTION] {
        let Some(service) = service_of(package).and_then(|s| pool.get_service_by_name(s)) else {
            missing.push(format!("service of {package}"));
            continue;
        };
        for m in service.methods() {
            let is_deferred =
                package == TELEPHONY && deferred.contains(&format!("rpc:{}", m.name()));
            for msg in [m.input(), m.output()] {
                let have = seen.messages.contains(msg.full_name());
                if is_deferred && have {
                    early.push(format!(
                        "rpc {} is deferred but has a valid script",
                        m.name()
                    ));
                } else if !is_deferred && !have {
                    missing.push(format!("message {}", msg.full_name()));
                }
            }
        }
        if let Some(watch) = pool.get_message_by_name(&format!("{package}.WatchResponse")) {
            for f in watch.oneofs().flat_map(|o| o.fields().collect::<Vec<_>>()) {
                let have =
                    seen.watch_cases
                        .contains(&format!("{}.{}", watch.full_name(), f.name()));
                let is_deferred =
                    package == TELEPHONY && deferred.contains(&format!("event:{}", f.name()));
                if is_deferred && have {
                    early.push(format!(
                        "event {} is deferred but has a valid script",
                        f.name()
                    ));
                } else if !is_deferred && !have {
                    missing.push(format!("{}.{}", watch.full_name(), f.name()));
                }
            }
        }
    }
    if let Some(codes) = pool.get_enum_by_name(&format!("{TELEPHONY}.ErrorCode")) {
        for v in codes.values() {
            let code = v
                .name()
                .trim_start_matches("ERROR_CODE_")
                .to_ascii_lowercase();
            // stream_superseded ends the older of two streams: no step can expect it, so the
            // runners assert it whenever a script opens a second stream for a replica (§6).
            if ["unspecified", "internal", "stream_superseded"].contains(&code.as_str()) {
                continue;
            }
            let have = seen.errors.contains(&format!("{TELEPHONY}:{code}"));
            let is_deferred = deferred.contains(&format!("error:{code}"));
            if is_deferred && have {
                early.push(format!("error {code} is deferred but has a script"));
            } else if !is_deferred && !have {
                missing.push(format!("{TELEPHONY} error {code}"));
            }
        }
    }
    missing.extend(
        GRPC_RULE_CODES
            .iter()
            .filter(|c| !seen.rule_codes.contains(**c))
            .map(|c| format!("gRPC rule {c}")),
    );
    let mut out: Vec<String> = missing
        .into_iter()
        .map(|m| format!("coverage: no fixture for {m}"))
        .collect();
    out.extend(early.into_iter().map(|e| format!("coverage: {e}")));
    out
}

/// The mark lint (§4.7, §6): every field marked `PII: phone`, `PCI: digits` or `SECRET: key` is
/// listed with that class in `masking/cases.v1.json`; among the messages a telephony
/// `WatchResponse` reaches, only `DtmfReceived` holds digits; no telephony field carries SDP.
pub fn mark_lint(pool: &DescriptorPool, cases: &Value) -> Vec<String> {
    let class_list = |class: &str| -> BTreeSet<String> {
        cases["classify"][class]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect()
    };
    let (phone, secret) = (class_list("phone"), class_list("secret"));
    let mut out = Vec::new();
    let mut digit_fields = BTreeSet::new();
    for message in pool
        .all_messages()
        .filter(|m| m.package_name() == TELEPHONY)
    {
        for field in message.fields() {
            let comment = leading_comment(&field);
            let name = field.name().to_owned();
            if comment.contains("PII: phone") && !phone.contains(&name) {
                out.push(format!(
                    "mark lint: {} is PII: phone but not a phone name in masking/cases.v1.json",
                    field.full_name()
                ));
            }
            let is_digits = comment.contains("PCI: digits");
            if (is_digits || comment.contains("SECRET: key")) && !secret.contains(&name) {
                out.push(format!(
                    "mark lint: {} is marked secret but not a secret name in masking/cases.v1.json",
                    field.full_name()
                ));
            }
            if is_digits {
                digit_fields.insert(field.full_name().to_owned());
            }
            if name == "sdp" || name.ends_with("_sdp") {
                out.push(format!("mark lint: {} carries SDP", field.full_name()));
            }
        }
    }
    out.extend(marks_elsewhere(pool));
    if let Some(watch) = pool.get_message_by_name(&format!("{TELEPHONY}.WatchResponse")) {
        let mut stack = vec![watch];
        let mut visited = BTreeSet::new();
        while let Some(m) = stack.pop() {
            if !visited.insert(m.full_name().to_owned()) {
                continue;
            }
            for field in m.fields() {
                if digit_fields.contains(field.full_name()) && m.name() != "DtmfReceived" {
                    out.push(format!(
                        "mark lint: event message {} holds digits",
                        m.full_name()
                    ));
                }
                if let Kind::Message(inner) = field.kind() {
                    stack.push(inner);
                }
            }
        }
    }
    out
}

const MARKS: [&str; 3] = ["PII: phone", "PCI: digits", "SECRET: key"];

/// A mark the lint would not see: in a trailing or detached comment, or on anything but a field.
fn marks_elsewhere(pool: &DescriptorPool) -> Vec<String> {
    let mut out = Vec::new();
    for file in pool.files().filter(|f| f.package_name() == TELEPHONY) {
        let Some(info) = file.file_descriptor_proto().source_code_info.as_ref() else {
            out.push(format!("mark lint: {} has no source info", file.name()));
            continue;
        };
        for loc in &info.location {
            let field_leading = is_field_path(&loc.path);
            let mut texts: Vec<&str> = loc
                .leading_detached_comments
                .iter()
                .map(String::as_str)
                .collect();
            texts.extend(loc.trailing_comments.as_deref());
            if !field_leading {
                texts.extend(loc.leading_comments.as_deref());
            }
            if texts.iter().any(|t| MARKS.iter().any(|m| t.contains(m))) {
                out.push(format!(
                    "mark lint: {} has a mark outside a field's leading comment (path {:?})",
                    file.name(),
                    loc.path
                ));
            }
        }
    }
    out
}

/// A message field's path: 4 (message_type), its index, then (3, index)* for nesting, 2, index.
fn is_field_path(path: &[i32]) -> bool {
    let n = path.len();
    n >= 4
        && n.is_multiple_of(2)
        && path[0] == 4
        && path[n - 2] == 2
        && path[2..n - 2].chunks(2).all(|c| c[0] == 3)
}

fn leading_comment(field: &FieldDescriptor) -> String {
    let path = field.path();
    let file = field.parent_file();
    file.file_descriptor_proto()
        .source_code_info
        .as_ref()
        .and_then(|info| info.location.iter().find(|l| l.path == path))
        .and_then(|l| l.leading_comments.clone())
        .unwrap_or_default()
}
