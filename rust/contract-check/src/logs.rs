//! log-record.v1 (0.13.0): structured log lines, checked against
//! `schemas/logging/log-record.v1.json`, the services' event catalogues and the masking classes
//! (CONVENTIONS.md "Logging (log-record.v1)"). Used for the `log` fixtures and by
//! `contract-check --logs`, which any service's CI runs on the NDJSON its tests captured.

use crate::{Report, Schemas, json_files, read_json};
use anyhow::{Result, bail};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fmt;
use std::path::{Path, PathBuf};

/// The longest line, in bytes without the newline: Docker splits longer lines (16 KiB).
pub const MAX_LINE: usize = 16_000;
/// The largest `decision`, serialized compactly.
pub const MAX_DECISION: usize = 4096;
/// The events every service shares.
pub const COMMON: &str = "logging/events.common.v1.json";

/// Every rule a line can break: the `violates` codes of `log` fixtures.
pub const RULES: [&str; 19] = [
    "not_json",
    "line_too_long",
    "schema",
    "bad_xsid",
    "missing_xsid",
    "xsid_mismatch",
    "missing_correlation",
    "missing_attr",
    "unknown_service",
    "unknown_event",
    "level_not_allowed",
    "transition_incomplete",
    "decision_too_large",
    "decision_level",
    "stack_level",
    "unmasked_phone",
    "unmasked_secret",
    "unmasked_email",
    "seq_repeat",
];

const LEVELS: [&str; 6] = ["trace", "debug", "info", "warn", "error", "fatal"];

/// One broken rule, with what broke it.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Violation {
    pub code: &'static str,
    pub detail: String,
}

impl fmt::Display for Violation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code, self.detail)
    }
}

fn violation(code: &'static str, detail: impl Into<String>) -> Violation {
    Violation {
        code,
        detail: detail.into(),
    }
}

/// One catalogue entry (`schemas/logging/log-catalogue.v1.json`, `$defs/event`).
#[derive(Debug, Default)]
pub struct EventSpec {
    levels: BTreeSet<String>,
    call_scoped: bool,
    requires: Vec<String>,
    attrs: Vec<String>,
    transition: bool,
    decision: bool,
}

fn strings(v: &Value) -> impl Iterator<Item = String> + '_ {
    v.as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
}

impl EventSpec {
    fn parse(v: &Value) -> Self {
        Self {
            levels: strings(&v["levels"]).collect(),
            call_scoped: v["call_scoped"] == true,
            requires: strings(&v["requires"]).collect(),
            attrs: strings(&v["attrs"]).collect(),
            transition: v["transition"] == true,
            decision: v["decision"] == true,
        }
    }
}

fn events(doc: &Value) -> BTreeMap<String, EventSpec> {
    doc["events"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(k, v)| (k.clone(), EventSpec::parse(v)))
        .collect()
}

/// What a catalogue document breaks: its schema, and the rules a schema can't hold.
pub fn catalogue_errors(schemas: &Schemas, doc: &Value, common: bool) -> Vec<String> {
    let mut errs = schemas.errors("log-catalogue", doc);
    if common != (doc["service"] == "*") {
        errs.push(if common {
            "the common catalogue's service must be \"*\"".into()
        } else {
            "only the common catalogue has service \"*\"".into()
        });
    }
    for (name, e) in doc["events"].as_object().into_iter().flatten() {
        if e.get("sample_rate").is_some() && e["decision"] != true {
            errs.push(format!("{name}: sample_rate without decision"));
        }
    }
    errs
}

/// The catalogues in force: the common one and one per service.
pub struct Catalogues {
    common: BTreeMap<String, EventSpec>,
    services: BTreeMap<String, BTreeMap<String, EventSpec>>,
}

enum Lookup<'c> {
    NoService,
    NoEvent,
    Found(&'c EventSpec),
}

impl Catalogues {
    /// The common catalogue (from `root`) and the service catalogues `files`. A catalogue that
    /// breaks its schema, repeats a common event, or is the second for its service is an error.
    pub fn load(root: &Path, schemas: &Schemas, files: &[PathBuf]) -> Result<Self> {
        let doc = read_json(&root.join(COMMON))?;
        let errs = catalogue_errors(schemas, &doc, true);
        if !errs.is_empty() {
            bail!("{COMMON}: {}", errs.join("; "));
        }
        let common = events(&doc);
        let mut services = BTreeMap::new();
        for file in files {
            let doc = read_json(file)?;
            let mut errs = catalogue_errors(schemas, &doc, false);
            let own = events(&doc);
            errs.extend(
                own.keys()
                    .filter(|k| common.contains_key(*k))
                    .map(|k| format!("{k} is a common event ({COMMON})")),
            );
            if !errs.is_empty() {
                bail!("{}: {}", file.display(), errs.join("; "));
            }
            let service = doc["service"].as_str().unwrap_or_default().to_owned();
            if services.insert(service.clone(), own).is_some() {
                bail!("{}: a second catalogue for {service}", file.display());
            }
        }
        Ok(Self { common, services })
    }

    fn lookup(&self, service: &str, event: &str) -> Lookup<'_> {
        let Some(own) = self.services.get(service) else {
            return Lookup::NoService;
        };
        match own.get(event).or_else(|| self.common.get(event)) {
            Some(spec) => Lookup::Found(spec),
            None => Lookup::NoEvent,
        }
    }
}

/// A field name's masking class.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Class {
    Plain,
    Phone,
    Secret,
    Email,
}

/// interaction-engine's ie-obs rules (crates/ie-obs/src/pii.rs) on top of the names listed in
/// masking/cases.v1.json and masking/billing.v1.json: the strictest reading every service's
/// masker must at least match.
const PHONE_NAMES: [&str; 35] = [
    "from",
    "to",
    "number",
    "phone",
    "caller",
    "callee",
    "caller_id",
    "callback_number",
    "destination",
    "ani",
    "dnis",
    "msisdn",
    "e164",
    "contact",
    "uri",
    "pai",
    "p_asserted_identity",
    "rpid",
    "remote_party_id",
    "diversion",
    "history_info",
    "user",
    "channel_name",
    "caller_channel_name",
    "caller_username",
    "rdnis",
    "dialed_extension",
    "originate_string",
    "dial_string",
    "sip_from_display",
    "caller_caller_id_name",
    "sip_destination_url",
    "sip_call_id",
    "called_number",
    "caller_number",
];
const SECRET_NAMES: [&str; 14] = [
    "digits",
    "dtmf",
    "pin",
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "dek",
    "wrapped_dek",
    "sdp",
    "sip_body",
    "passwd",
    "credentials",
];
/// Any name with one of these `_`-separated tokens is a secret: DTMF digits can be a card number.
const SECRET_TOKENS: [&str; 3] = ["dtmf", "digit", "digits"];
const EXEMPT: [&str; 2] = ["reply_to", "num_digits"];

/// The masking classes by field name.
#[derive(Default)]
pub struct Masking {
    plain: BTreeSet<String>,
    phone: BTreeSet<String>,
    secret: BTreeSet<String>,
    email: BTreeSet<String>,
}

/// camelCase split, lowercase, `-` → `_`, a leading `variable_` stripped (masking/cases.v1.json).
pub fn normalize(name: &str) -> String {
    let chars: Vec<char> = name.chars().collect();
    let mut out = String::with_capacity(name.len() + 4);
    for (i, &c) in chars.iter().enumerate() {
        if c == '-' {
            out.push('_');
            continue;
        }
        if c.is_ascii_uppercase() && i > 0 {
            let prev = chars[i - 1];
            let next_lower = chars.get(i + 1).is_some_and(char::is_ascii_lowercase);
            if prev.is_ascii_lowercase()
                || prev.is_ascii_digit()
                || (prev.is_ascii_uppercase() && next_lower)
            {
                out.push('_');
            }
        }
        out.push(c.to_ascii_lowercase());
    }
    match out.strip_prefix("variable_") {
        Some(rest) => rest.to_owned(),
        None => out,
    }
}

/// `name` is `listed` or ends with `_` + `listed`.
fn matches(name: &str, listed: &str) -> bool {
    name == listed
        || (name.len() > listed.len()
            && name.ends_with(listed)
            && name.as_bytes()[name.len() - listed.len() - 1] == b'_')
}

impl Masking {
    /// The class lists of masking/cases.v1.json and masking/billing.v1.json, normalized.
    pub fn load(root: &Path) -> Result<Self> {
        let mut m = Self::default();
        for rel in ["masking/cases.v1.json", "masking/billing.v1.json"] {
            let doc = read_json(&root.join(rel))?;
            let classify = &doc["classify"];
            for (class, set) in [
                ("plain", &mut m.plain),
                ("phone", &mut m.phone),
                ("secret", &mut m.secret),
                ("email", &mut m.email),
            ] {
                set.extend(strings(&classify[class]).map(|n| normalize(&n)));
            }
        }
        m.plain.extend(EXEMPT.iter().map(|s| (*s).to_owned()));
        Ok(m)
    }

    pub fn classify(&self, name: &str) -> Class {
        let n = normalize(name);
        if self.plain.contains(&n) {
            Class::Plain
        } else if self.email.contains(&n) || n.ends_with("_email") {
            Class::Email
        } else if self.secret.contains(&n)
            || n.split('_').any(|t| SECRET_TOKENS.contains(&t))
            || SECRET_NAMES.iter().any(|s| matches(&n, s))
            || ["_secret", "_token", "_password"]
                .iter()
                .any(|s| n.ends_with(s))
        {
            Class::Secret
        } else if self.phone.contains(&n)
            || PHONE_NAMES.iter().any(|p| matches(&n, p))
            || ["_number", "_phone", "_user"]
                .iter()
                .any(|s| n.ends_with(s))
        {
            Class::Phone
        } else {
            Class::Plain
        }
    }

    /// Every value under a phone, secret or email name must be in its masked form; every other
    /// string must hold no number and no email address.
    fn walk(&self, path: &str, value: &Value, out: &mut Vec<Violation>) {
        match value {
            Value::Object(map) => {
                for (k, child) in map {
                    let at = format!("{path}/{k}");
                    match self.classify(k) {
                        Class::Plain => self.walk(&at, child, out),
                        Class::Phone => masked_phone(&at, child, out),
                        Class::Secret if child != "[redacted]" => out.push(violation(
                            "unmasked_secret",
                            format!("{at} is a secret (DTMF digits, a token): only \"[redacted]\""),
                        )),
                        Class::Email if child != "[email]" => out.push(violation(
                            "unmasked_email",
                            format!("{at} is an email: only \"[email]\""),
                        )),
                        _ => {}
                    }
                }
            }
            Value::Array(items) => {
                for (i, child) in items.iter().enumerate() {
                    self.walk(&format!("{path}/{i}"), child, out);
                }
            }
            Value::String(s) => {
                if has_phone(s) {
                    out.push(violation(
                        "unmasked_phone",
                        format!("{path} holds a phone number"),
                    ));
                }
                if has_email(s) {
                    out.push(violation(
                        "unmasked_email",
                        format!("{path} holds an email address"),
                    ));
                }
            }
            _ => {}
        }
    }
}

/// `***` and the last 3 digits, or `***` alone (masking/cases.v1.json `mask_phone`).
fn is_masked_phone(s: &str) -> bool {
    s == "***"
        || (s.len() == 6 && s.starts_with("***") && s[3..].bytes().all(|c| c.is_ascii_digit()))
}

fn masked_phone(path: &str, value: &Value, out: &mut Vec<Violation>) {
    match value {
        Value::String(s) if is_masked_phone(s) => {}
        Value::Bool(_) | Value::Null => {}
        Value::Array(items) => items
            .iter()
            .enumerate()
            .for_each(|(i, c)| masked_phone(&format!("{path}/{i}"), c, out)),
        Value::Object(map) => map
            .iter()
            .for_each(|(k, c)| masked_phone(&format!("{path}/{k}"), c, out)),
        _ => out.push(violation(
            "unmasked_phone",
            format!("{path} is a phone-class name: only ***<last 3 digits>"),
        )),
    }
}

/// An E.164 number (`+` and 7 to 15 digits), or a `sip:`/`sips:`/`tel:` user part of 7 or more
/// digits.
fn has_phone(s: &str) -> bool {
    let b = s.as_bytes();
    for (i, _) in s.match_indices('+') {
        let run = b[i + 1..].iter().take_while(|c| c.is_ascii_digit()).count();
        if run >= 7 && b[i + 1] != b'0' {
            return true;
        }
    }
    let lower = s.to_ascii_lowercase();
    ["sip:", "sips:", "tel:"].iter().any(|p| {
        lower.match_indices(p).any(|(i, _)| {
            let rest = &lower[i + p.len()..];
            let rest = rest.strip_prefix('+').unwrap_or(rest);
            rest.bytes().take_while(u8::is_ascii_digit).count() >= 7
        })
    })
}

/// `local@domain.tld`, except a SIP URI's user part (`sip:alice@host`: has_phone judges those).
fn has_email(s: &str) -> bool {
    let b = s.as_bytes();
    for (i, _) in s.match_indices('@') {
        let local = b[..i]
            .iter()
            .rev()
            .take_while(|c| c.is_ascii_alphanumeric() || b"._%+-".contains(c))
            .count();
        if local == 0 {
            continue;
        }
        let before = s[..i - local].to_ascii_lowercase();
        if before.ends_with("sip:") || before.ends_with("sips:") {
            continue;
        }
        let len = b[i + 1..]
            .iter()
            .take_while(|c| c.is_ascii_alphanumeric() || **c == b'.' || **c == b'-')
            .count();
        let domain = s[i + 1..i + 1 + len].trim_end_matches('.');
        if let Some((head, tld)) = domain.rsplit_once('.')
            && !head.is_empty()
            && tld.len() >= 2
            && tld.bytes().all(|c| c.is_ascii_alphabetic())
        {
            return true;
        }
    }
    false
}

/// Checks lines one after another; remembers each process's `seq` numbers within a stream.
pub struct Checker<'a> {
    schemas: &'a Schemas,
    catalogues: &'a Catalogues,
    masking: &'a Masking,
    /// Per (service, instance, incarnation): the seqs seen since its last seq 1 (a restart).
    seqs: HashMap<(String, String, String), HashSet<u64>>,
}

impl<'a> Checker<'a> {
    pub fn new(schemas: &'a Schemas, catalogues: &'a Catalogues, masking: &'a Masking) -> Self {
        Self {
            schemas,
            catalogues,
            masking,
            seqs: HashMap::new(),
        }
    }

    /// A new capture (another file): no seq is remembered.
    pub fn new_stream(&mut self) {
        self.seqs.clear();
    }

    /// One NDJSON line (without its newline).
    pub fn line(&mut self, text: &str) -> Vec<Violation> {
        let mut out = Vec::new();
        if text.len() > MAX_LINE {
            out.push(violation(
                "line_too_long",
                format!("{} bytes (at most {MAX_LINE})", text.len()),
            ));
        }
        match serde_json::from_str::<Value>(text) {
            Ok(value) if value.is_object() => out.extend(self.record(&value)),
            _ => out.push(violation("not_json", "not one JSON object")),
        }
        out
    }

    /// One parsed record: every rule but the line's size.
    pub fn record(&mut self, r: &Value) -> Vec<Violation> {
        let mut out = Vec::new();
        let paths = self.schemas.error_paths("log-record", r);
        if paths
            .iter()
            .any(|p| p.starts_with("/xsid") || p.starts_with("/parent_xsid"))
        {
            out.push(violation(
                "bad_xsid",
                "xsid and parent_xsid are lowercase UUIDv7",
            ));
        }
        if paths
            .iter()
            .any(|p| !p.starts_with("/xsid") && !p.starts_with("/parent_xsid"))
        {
            let errs = self.schemas.errors("log-record", r);
            out.push(violation("schema", errs.join("; ")));
        }
        let text = |k: &str| r.get(k).and_then(Value::as_str);
        let level = text("level").unwrap_or_default();
        let xsid = text("xsid");
        if let (Some(parent), Some(x)) = (text("parent_xsid"), xsid)
            && parent == x
        {
            out.push(violation("bad_xsid", "parent_xsid equals xsid"));
        }
        if let (Some(id), Some(x)) = (text("interaction_id"), xsid)
            && id != x
        {
            out.push(violation("xsid_mismatch", "interaction_id isn't the xsid"));
        }
        let service = text("service").unwrap_or_default();
        let event = text("event").unwrap_or_default();
        match self.catalogues.lookup(service, event) {
            Lookup::NoService => out.push(violation(
                "unknown_service",
                format!("no catalogue for service {service:?}"),
            )),
            Lookup::NoEvent => out.push(violation(
                "unknown_event",
                format!("{service} has no event {event:?}"),
            )),
            Lookup::Found(spec) => event_rules(spec, event, level, r, &mut out),
        }
        if let Some(d) = r.get("decision") {
            let size = d.to_string().len();
            if size > MAX_DECISION {
                out.push(violation(
                    "decision_too_large",
                    format!("decision is {size} bytes (at most {MAX_DECISION})"),
                ));
            }
            if !matches!(level, "trace" | "debug") && d.get("sampled") != Some(&Value::Bool(true)) {
                out.push(violation(
                    "decision_level",
                    format!("a decision at {level} must be sampled (or at debug)"),
                ));
            }
        }
        if r.pointer("/error/stack").is_some() && !matches!(level, "error" | "fatal") {
            out.push(violation(
                "stack_level",
                format!("error.stack at {level}: error and fatal only"),
            ));
        }
        self.masking.walk("", r, &mut out);
        if let Some(seq) = r.get("seq").and_then(Value::as_u64) {
            let key = (
                service.to_owned(),
                text("instance").unwrap_or_default().to_owned(),
                text("incarnation").unwrap_or_default().to_owned(),
            );
            let seen = self.seqs.entry(key).or_default();
            if seq == 1 {
                seen.clear();
            }
            if !seen.insert(seq) {
                out.push(violation(
                    "seq_repeat",
                    format!("seq {seq} again for this service, instance and incarnation"),
                ));
            }
        }
        out
    }
}

fn event_rules(spec: &EventSpec, event: &str, level: &str, r: &Value, out: &mut Vec<Violation>) {
    if LEVELS.contains(&level) && !spec.levels.contains(level) {
        out.push(violation(
            "level_not_allowed",
            format!("{event} at {level}; its catalogue allows {:?}", spec.levels),
        ));
    }
    if spec.call_scoped {
        if r.get("xsid").is_none() {
            out.push(violation(
                "missing_xsid",
                format!("{event} is call-scoped: xsid is required"),
            ));
        }
        if r.get("tenant_id").is_none() {
            out.push(violation(
                "missing_correlation",
                format!("{event} is call-scoped: tenant_id is required"),
            ));
        }
    }
    for f in spec.requires.iter().filter(|f| r.get(f.as_str()).is_none()) {
        out.push(violation(
            "missing_correlation",
            format!("{event} requires {f}"),
        ));
    }
    for a in spec
        .attrs
        .iter()
        .filter(|a| r["attrs"].get(a.as_str()).is_none())
    {
        out.push(violation(
            "missing_attr",
            format!("{event} requires attrs.{a}"),
        ));
    }
    if spec.transition && (r.get("from_state").is_none() || r.get("to_state").is_none()) {
        out.push(violation(
            "transition_incomplete",
            format!("{event} is a transition: from_state and to_state"),
        ));
    }
    if !spec.decision && r.get("decision").is_some() {
        out.push(violation(
            "decision_level",
            format!("{event} carries no decision (its catalogue entry)"),
        ));
    }
}

/// Checks NDJSON text: the lines checked, and each violation with its 1-based line number.
/// `skip_non_json` passes over lines that don't start with `{` (a test runner's own output).
pub fn check_ndjson(
    checker: &mut Checker<'_>,
    text: &str,
    skip_non_json: bool,
) -> (usize, Vec<(usize, Violation)>) {
    let mut checked = 0;
    let mut found = Vec::new();
    for (i, line) in text.lines().enumerate() {
        if line.trim().is_empty() || (skip_non_json && !line.trim_start().starts_with('{')) {
            continue;
        }
        checked += 1;
        found.extend(checker.line(line).into_iter().map(|v| (i + 1, v)));
    }
    (checked, found)
}

/// A `log` fixture: the rules its lines break must be exactly those it declares.
pub fn check_fixture(
    schemas: &Schemas,
    root: &Path,
    masking: &Masking,
    file: &Path,
    f: &Value,
    report: &mut Report,
) {
    let files: Vec<PathBuf> = strings(&f["catalogues"]).map(|p| root.join(p)).collect();
    let catalogues = match Catalogues::load(root, schemas, &files) {
        Ok(c) => c,
        Err(e) => return report.fail(file, format!("{e:#}")),
    };
    let mut checker = Checker::new(schemas, &catalogues, masking);
    let mut found = Vec::new();
    if let Some(text) = f["line_text"].as_str() {
        found.extend(checker.line(text));
    }
    for r in f["records"].as_array().into_iter().flatten() {
        let size = r.to_string().len();
        if size > MAX_LINE {
            found.push(violation("line_too_long", format!("{size} bytes")));
        }
        found.extend(checker.record(r));
    }
    let codes: BTreeSet<&str> = found.iter().map(|v| v.code).collect();
    let declared: BTreeSet<String> = strings(&f["violates"]).collect();
    if codes
        .iter()
        .map(|c| (*c).to_owned())
        .collect::<BTreeSet<_>>()
        != declared
    {
        let detail: Vec<String> = found.iter().map(ToString::to_string).collect();
        report.fail(
            file,
            format!(
                "breaks {codes:?}, declares {declared:?} ({})",
                detail.join("; ")
            ),
        );
    }
    report.log_codes.extend(declared);
}

/// Every catalogue under `logging/` is valid, and every rule has a fixture that breaks it.
pub fn coverage(root: &Path, schemas: &Schemas, report: &mut Report) {
    let dir = root.join("logging");
    match json_files(&dir) {
        Ok(files) => {
            let files: Vec<PathBuf> = files.into_iter().filter(|f| !f.ends_with(COMMON)).collect();
            if let Err(e) = Catalogues::load(root, schemas, &files) {
                report.errors.push(format!("logging: {e:#}"));
            }
        }
        Err(e) => report.errors.push(format!("logging: {e:#}")),
    }
    for rule in RULES {
        if !report.log_codes.contains(rule) {
            report
                .errors
                .push(format!("coverage: no log fixture breaks {rule}"));
        }
    }
}

/// The records of one call, by `xsid`: its own, its children's (`parent_xsid`) and those that
/// still name it `interaction_id`; or (0.16.0) of one operation, by `op_id`, or one saga, by
/// `saga_id` or `parent_saga_id`. Ordered by ts, then service, instance and seq. One line each, for
/// a person or a model reading the flow.
pub fn flow(texts: &[String], xsid: &str) -> Vec<String> {
    let mut rows = Vec::new();
    for text in texts {
        for line in text.lines() {
            let Ok(r) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            if ![
                "xsid",
                "parent_xsid",
                "interaction_id",
                "op_id",
                "saga_id",
                "parent_saga_id",
            ]
            .iter()
            .any(|k| r[*k] == xsid)
            {
                continue;
            }
            let s = |k: &str| r[k].as_str().unwrap_or_default().to_owned();
            rows.push((
                s("ts"),
                s("service"),
                s("instance"),
                r["seq"].as_u64().unwrap_or_default(),
                render(&r, xsid),
            ));
        }
    }
    rows.sort();
    rows.into_iter().map(|row| row.4).collect()
}

fn render(r: &Value, xsid: &str) -> String {
    let s = |k: &str| r[k].as_str().unwrap_or_default();
    let mut out = format!(
        "{} {:<28} {:<5} {:<26}",
        s("ts"),
        s("service"),
        s("level"),
        s("event")
    );
    if r.get("from_state").is_some() || r.get("to_state").is_some() {
        out.push_str(&format!(" {}->{}", s("from_state"), s("to_state")));
    }
    if r["xsid"] != xsid && r.get("xsid").is_some() {
        out.push_str(&format!(" child={}", s("xsid")));
    }
    for k in [
        "outcome",
        "leg_id",
        "agent_id",
        "queue_id",
        "entry_id",
        "reservation_id",
        "command_id",
        "op",
        "cause",
        "duration_ms",
        "attempt",
    ] {
        match &r[k] {
            Value::Null => {}
            Value::String(v) => out.push_str(&format!(" {k}={v}")),
            v => out.push_str(&format!(" {k}={v}")),
        }
    }
    if let Some(code) = r.pointer("/error/code").and_then(Value::as_str) {
        out.push_str(&format!(" error={code}"));
    }
    out.push_str(&format!(" | {}", s("msg")));
    out
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]
    use super::*;

    fn root() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
    }

    /// The shared masking cases: this reading classifies and normalizes every listed name as
    /// the services do.
    #[test]
    fn the_shared_masking_cases_hold() {
        let m = Masking::load(&root()).unwrap();
        for rel in ["masking/cases.v1.json", "masking/billing.v1.json"] {
            let doc = read_json(&root().join(rel)).unwrap();
            for c in doc["normalize"].as_array().into_iter().flatten() {
                assert_eq!(normalize(c["in"].as_str().unwrap()), c["out"], "{c}");
            }
            for (class, want) in [
                ("phone", Class::Phone),
                ("secret", Class::Secret),
                ("email", Class::Email),
                ("plain", Class::Plain),
            ] {
                for name in strings(&doc["classify"][class]) {
                    assert_eq!(m.classify(&name), want, "{rel}: {name}");
                }
            }
        }
        for (name, want) in [
            ("from_state", Class::Plain),
            ("to_state", Class::Plain),
            ("state_from", Class::Phone),
            ("xsid", Class::Plain),
            ("parent_xsid", Class::Plain),
            ("email_kind", Class::Plain),
            ("presses", Class::Plain),
            ("dtmf_digit", Class::Secret),
        ] {
            assert_eq!(m.classify(name), want, "{name}");
        }
    }

    #[test]
    fn free_text_scans() {
        assert!(has_phone("no route for +447700900123"));
        assert!(has_phone("sip:07700900123@carrier.example"));
        assert!(!has_phone("0.4.2+3f9c2ab"));
        assert!(!has_phone("0192f3c1-6a2b-7c3d-8e4f-000000000001"));
        assert!(has_email("sent to pat@example.com."));
        assert!(!has_email("sip:alice@carrier.example"));
        assert!(!has_email("parity.billing.Foo@1a2b3c"));
        assert!(is_masked_phone("***123") && is_masked_phone("***") && !is_masked_phone("***12a"));
    }
}
