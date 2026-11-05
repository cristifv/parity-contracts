//! contract-check against the real schemas and fixtures, and against broken copies of them.
//! Needs `build/contracts.binpb` (ci.sh builds it with buf before the tool's tests run).
#![allow(clippy::unwrap_used, clippy::panic)]

use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn fds() -> PathBuf {
    let f = repo().join("build/contracts.binpb");
    assert!(
        f.exists(),
        "run `buf build --as-file-descriptor-set -o build/contracts.binpb` first (scripts/ci.sh does)"
    );
    f
}

fn copy_dir(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let path = entry.unwrap().path();
        let target = to.join(path.file_name().unwrap());
        if path.is_dir() {
            copy_dir(&path, &target);
        } else {
            fs::copy(&path, &target).unwrap();
        }
    }
}

/// A scratch copy of schemas/, fixtures/ and scenarios/ that a test may break; removed when dropped.
struct Scratch(PathBuf);

impl std::ops::Deref for Scratch {
    type Target = Path;
    fn deref(&self) -> &Path {
        &self.0
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn scratch(name: &str) -> Scratch {
    let dir = std::env::temp_dir().join(format!("contract-check-{}-{name}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    copy_dir(&repo().join("schemas"), &dir.join("schemas"));
    copy_dir(&repo().join("fixtures"), &dir.join("fixtures"));
    copy_dir(&repo().join("masking"), &dir.join("masking"));
    copy_dir(&repo().join("tenants"), &dir.join("tenants"));
    copy_dir(&repo().join("scenarios"), &dir.join("scenarios"));
    copy_dir(&repo().join("logging"), &dir.join("logging"));
    Scratch(dir)
}

fn edit(file: &Path, f: impl FnOnce(&mut Value)) {
    let mut v: Value = serde_json::from_str(&fs::read_to_string(file).unwrap()).unwrap();
    f(&mut v);
    fs::write(file, serde_json::to_string_pretty(&v).unwrap()).unwrap();
}

fn errors(root: &Path, baseline: Option<&Path>) -> Vec<String> {
    contract_check::run(root, &fds(), baseline).unwrap().errors
}

#[test]
fn the_real_fixtures_pass_with_full_coverage() {
    let report = contract_check::run(&repo(), &fds(), Some(&repo().join("fixtures"))).unwrap();
    assert!(report.errors.is_empty(), "{:#?}", report.errors);
    assert!(report.checked.values().sum::<usize>() >= 70);
}

#[test]
fn an_undeclared_field_fails_the_schema() {
    let dir = scratch("field");
    edit(
        &dir.join("fixtures/interaction.events/valid/hold_started.json"),
        |v| v["record"]["value"]["extra"] = 1.into(),
    );
    let e = errors(&dir, None);
    assert!(
        e.iter()
            .any(|e| e.contains("hold_started.json") && e.contains("breaks {\"schema\"}")),
        "{e:#?}"
    );
}

#[test]
fn a_wrong_violates_list_fails() {
    let dir = scratch("violates");
    edit(
        &dir.join("fixtures/interaction.commands/invalid/key-is-tenant.json"),
        |v| v["violates"] = serde_json::json!(["schema"]),
    );
    let e = errors(&dir, None);
    assert!(
        e.iter()
            .any(|e| e.contains("key-is-tenant.json") && e.contains("key_mismatch")),
        "{e:#?}"
    );
}

#[test]
fn an_unknown_protobuf_field_fails_the_strict_parse() {
    let dir = scratch("proto");
    edit(
        &dir.join("fixtures/routing.v1/valid/enqueue-new.json"),
        |v| v["steps"][0]["body"]["bogus"] = 1.into(),
    );
    let e = errors(&dir, None);
    assert!(
        e.iter()
            .any(|e| e.contains("enqueue-new.json") && e.contains("bogus")),
        "{e:#?}"
    );
}

#[test]
fn a_type_without_a_fixture_fails_coverage() {
    let dir = scratch("coverage");
    fs::remove_file(dir.join("fixtures/interaction.events/valid/closed.json")).unwrap();
    let e = errors(&dir, None);
    assert!(
        e.iter()
            .any(|e| e == "coverage: no fixture for type closed"),
        "{e:#?}"
    );
}

#[test]
fn the_break_gate_catches_a_newly_required_field() {
    let dir = scratch("break");
    edit(
        &dir.join("schemas/events/interaction-events.v1.json"),
        |v| {
            v["$defs"]["envelope"]["required"]
                .as_array_mut()
                .unwrap()
                .push("conversation_id".into());
        },
    );
    let e = errors(&dir, Some(&repo().join("fixtures")));
    assert!(e.iter().any(|e| e.contains("break gate")), "{e:#?}");
}

#[test]
fn a_phone_number_without_x_pii_fails_the_lint() {
    let dir = scratch("pii");
    edit(
        &dir.join("schemas/commands/interaction-commands.v1.json"),
        |v| {
            let to = v["$defs"]["cmd_originate"]["properties"]["to"]
                .as_object_mut()
                .unwrap();
            to.remove("x-pii");
        },
    );
    let e = errors(&dir, None);
    assert!(e.iter().any(|e| e.contains("property to lacks")), "{e:#?}");
}

#[test]
fn a_reader_action_that_contradicts_the_broken_rules_fails() {
    // G1: the reader action is derived from the rules a fixture breaks, not just allowed.
    let dir = scratch("reader");
    edit(
        &dir.join("fixtures/interaction.events/invalid/missing-tenant-header.json"),
        |v| v["reader"] = "apply".into(),
    );
    edit(
        &dir.join("fixtures/interaction.commands/invalid/reply-to-event-topic.json"),
        |v| v["reader"] = "reply_error:bad_request".into(),
    );
    let e = errors(&dir, None);
    assert!(
        e.iter()
            .any(|e| e.contains("missing-tenant-header.json") && e.contains("call for \"reject\"")),
        "{e:#?}"
    );
    assert!(
        e.iter()
            .any(|e| e.contains("reply-to-event-topic.json") && e.contains("call for \"dlt\"")),
        "{e:#?}"
    );
}

// ---- telephony.v1 (TC ADR 0001 §6) ----------------------------------------------------------

const TEL: &str = "fixtures/telephony.v1";

fn has(e: &[String], file: &str, text: &str) -> bool {
    e.iter().any(|e| e.contains(file) && e.contains(text))
}

#[test]
fn a_marked_field_missing_from_the_masking_cases_fails_the_mark_lint() {
    // Also proves the lint reads the proto's comments: without them nothing would be marked.
    let dir = scratch("mark");
    edit(&dir.join("masking/cases.v1.json"), |v| {
        v["classify"]["phone"]
            .as_array_mut()
            .unwrap()
            .retain(|n| n != "called_number");
        v["classify"]["secret"]
            .as_array_mut()
            .unwrap()
            .retain(|n| n != "dtmf_sequence");
    });
    let e = errors(&dir, None);
    assert!(has(&e, "Arrived.called_number", "PII: phone"), "{e:#?}");
    assert!(
        has(&e, "DtmfActionBinding.dtmf_sequence", "marked secret"),
        "{e:#?}"
    );
}

#[test]
fn a_writer_invalid_script_must_expect_the_derived_error() {
    let dir = scratch("derived");
    edit(&dir.join(TEL).join("invalid/epoch-zero.json"), |v| {
        let steps = v["steps"].as_array_mut().unwrap();
        steps.last_mut().unwrap()["expect"]["parity_error"] = "tenant_mismatch".into();
    });
    let e = errors(&dir, None);
    assert!(
        has(
            &e,
            "epoch-zero.json",
            "call for INVALID_ARGUMENT / invalid_request"
        ),
        "{e:#?}"
    );
}

#[test]
fn a_writer_invalid_script_must_declare_exactly_the_rules_it_breaks() {
    let dir = scratch("declared");
    edit(&dir.join(TEL).join("invalid/epoch-zero.json"), |v| {
        v["violates"] = serde_json::json!(["bad_uuid"]);
    });
    let e = errors(&dir, None);
    assert!(has(&e, "epoch-zero.json", "the last call breaks"), "{e:#?}");
}

#[test]
fn a_valid_script_breaking_a_writer_rule_fails() {
    let dir = scratch("writer");
    edit(&dir.join(TEL).join("valid/arrival-answer.json"), |v| {
        for step in v["steps"].as_array_mut().unwrap() {
            if step["method"]
                .as_str()
                .is_some_and(|m| m.ends_with("/AnswerLeg"))
            {
                step["body"]["command_id"] = "has a space".into();
            }
        }
    });
    let e = errors(&dir, None);
    assert!(has(&e, "arrival-answer.json", "bad_command_id"), "{e:#?}");
}

#[test]
fn a_race_script_must_end_with_an_asserted_outcome() {
    let dir = scratch("outcome");
    edit(
        &dir.join(TEL).join("valid/race-arrival-reoffer.json"),
        |v| {
            v["steps"].as_array_mut().unwrap().pop();
        },
    );
    let e = errors(&dir, None);
    assert!(
        has(&e, "race-arrival-reoffer.json", "pins the outcome"),
        "{e:#?}"
    );
}

#[test]
fn deferred_names_are_exempt_and_must_stay_without_scripts() {
    let dir = scratch("deferred");
    edit(&dir.join(TEL).join("deferred.json"), |v| {
        let d = v["deferred"].as_array_mut().unwrap();
        d.retain(|e| e["name"] != "SetIsolation");
        d.push(serde_json::json!({"kind": "rpc", "name": "AnswerLeg", "step": "never"}));
    });
    let e = errors(&dir, None);
    assert!(
        e.iter().any(
            |e| e == "coverage: no fixture for message parity.telephony.v1.SetIsolationRequest"
        ),
        "{e:#?}"
    );
    assert!(
        e.iter()
            .any(|e| e == "coverage: rpc AnswerLeg is deferred but has a valid script"),
        "{e:#?}"
    );
}

#[test]
fn a_replay_must_be_identical() {
    let dir = scratch("replay");
    edit(
        &dir.join(TEL).join("valid/race-replay-after-reconnect.json"),
        |v| {
            let steps = v["steps"].as_array_mut().unwrap();
            let last_deliver = steps
                .iter_mut()
                .rev()
                .find(|s| s["op"] == "deliver")
                .unwrap();
            last_deliver["body"]["at_ms"] = "1792800009999".into();
        },
    );
    let e = errors(&dir, None);
    assert!(
        has(
            &e,
            "race-replay-after-reconnect.json",
            "replayed with another body"
        ),
        "{e:#?}"
    );
}

#[test]
fn a_step_on_an_undeclared_stream_fails() {
    let dir = scratch("stream");
    edit(&dir.join(TEL).join("valid/arrival-answer.json"), |v| {
        v["steps"][0]["stream"] = "zz".into();
    });
    let e = errors(&dir, None);
    assert!(
        has(&e, "arrival-answer.json", "stream zz isn't declared"),
        "{e:#?}"
    );
}

#[test]
fn a_mark_outside_a_fields_leading_comment_fails_the_lint() {
    use prost_reflect::prost::Message;
    let pool = prost_reflect::DescriptorPool::decode(fs::read(fds()).unwrap().as_slice()).unwrap();
    let field = pool
        .get_message_by_name("parity.telephony.v1.Arrived")
        .unwrap()
        .get_field_by_name("called_number")
        .unwrap();
    let path = field.path().to_vec();
    let mut set = prost_reflect::prost_types::FileDescriptorSet {
        file: pool.file_descriptor_protos().cloned().collect(),
    };
    for file in &mut set.file {
        if let Some(info) = file.source_code_info.as_mut() {
            for loc in &mut info.location {
                if loc.path == path {
                    loc.trailing_comments = loc.leading_comments.take();
                }
            }
        }
    }
    let dir = scratch("trailing");
    let moved = dir.join("moved.binpb");
    fs::write(&moved, set.encode_to_vec()).unwrap();
    let e = contract_check::run(&dir, &moved, None).unwrap().errors;
    assert!(
        e.iter()
            .any(|e| e.contains("mark outside a field's leading comment")),
        "{e:#?}"
    );
}

#[test]
fn a_released_script_that_no_longer_parses_fails_the_break_gate() {
    let baseline = scratch("grpc-break");
    let file = baseline.join("fixtures/telephony.v1/valid/arrival-answer.json");
    edit(&file, |v| {
        let steps = v["steps"].as_array_mut().unwrap();
        let answer = steps
            .iter_mut()
            .find(|s| {
                s["method"]
                    .as_str()
                    .is_some_and(|m| m.ends_with("/AnswerLeg"))
            })
            .unwrap();
        answer["body"]["a_field_this_release_removed"] = 1.into();
    });
    let e = contract_check::run(&repo(), &fds(), Some(&baseline.join("fixtures")))
        .unwrap()
        .errors;
    assert!(
        e.iter().any(|e| e.contains("arrival-answer.json")
            && e.contains("a released script no longer parses")),
        "{e:#?}"
    );
}

// Tenant maps (0.4.0).

#[test]
fn the_shared_test_tenants_are_valid_test_data() {
    let schemas = contract_check::Schemas::load(&repo()).unwrap();
    let file: Value = serde_json::from_str(
        &fs::read_to_string(repo().join(contract_check::tenants::TEST_FILE)).unwrap(),
    )
    .unwrap();
    assert_eq!(contract_check::tenants::violations(&schemas, &file), vec![]);
    let ids: Vec<&str> = file["tenants"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["id"].as_str().unwrap())
        .collect();
    for id in ["acme", "globex", "initech", "initech-de", "initech-uk"] {
        assert!(ids.contains(&id), "{id} missing");
    }
}

#[test]
fn a_dialable_number_in_the_test_file_fails() {
    let dir = scratch("tenants-real-number");
    edit(&dir.join(contract_check::tenants::TEST_FILE), |v| {
        v["tenants"][0]["numbers"][0] = "+493012345678".into();
    });
    let e = errors(&dir, None);
    assert!(
        has(
            &e,
            "test-tenants.v1.json",
            "test_number_not_fictional at /tenants/0/numbers/0"
        ),
        "{e:#?}"
    );
}

#[test]
fn a_tenant_maps_fixture_declaring_the_wrong_rule_fails() {
    let dir = scratch("tenants-declares");
    edit(
        &dir.join("fixtures/tenant-maps.v1/invalid/duplicate-sip-domain.json"),
        |v| v["violates"] = serde_json::json!(["duplicate_number"]),
    );
    let e = errors(&dir, None);
    assert!(has(&e, "duplicate-sip-domain.json", "declares"), "{e:#?}");
}

#[test]
fn a_tenant_maps_fixture_pointing_at_the_wrong_place_fails() {
    let dir = scratch("tenants-at");
    edit(
        &dir.join("fixtures/tenant-maps.v1/invalid/schema-not-e164.json"),
        |v| v["at"] = "/tenants/1/numbers/0".into(),
    );
    let e = errors(&dir, None);
    assert!(
        has(
            &e,
            "schema-not-e164.json",
            "no violation at /tenants/1/numbers/0"
        ),
        "{e:#?}"
    );
}

#[test]
fn a_tenant_rule_without_a_fixture_fails_coverage() {
    let dir = scratch("tenants-coverage");
    fs::remove_file(dir.join("fixtures/tenant-maps.v1/invalid/tier-mismatch.json")).unwrap();
    let e = errors(&dir, None);
    assert!(
        e.iter()
            .any(|e| e == "coverage: no tenant_maps fixture for rule tier_mismatch"),
        "{e:#?}"
    );
}

#[test]
fn removing_a_released_test_tenant_fails_the_break_gate() {
    let dir = scratch("tenants-break");
    let baseline = dir.join("baseline");
    fs::create_dir_all(baseline.join("tenants")).unwrap();
    fs::copy(
        repo().join(contract_check::tenants::TEST_FILE),
        baseline.join("tenants/test-tenants.v1.json"),
    )
    .unwrap();
    edit(&dir.join(contract_check::tenants::TEST_FILE), |v| {
        v["tenants"][1]["numbers"].as_array_mut().unwrap().remove(0);
        let agents = v["test"]["agents"]["acme"].as_array_mut().unwrap();
        agents.remove(0);
    });
    let e = errors(&dir, Some(&baseline));
    assert!(
        e.iter()
            .any(|e| e.contains("break gate: number +15550101100 was removed")),
        "{e:#?}"
    );
    assert!(
        e.iter()
            .any(|e| e.contains("break gate: agent agent-a1 was removed")),
        "{e:#?}"
    );
}

#[test]
fn the_validator_reports_every_rule_with_its_location() {
    let schemas = contract_check::Schemas::load(&repo()).unwrap();
    let file = serde_json::json!({
        "schema_version": 1,
        "tenants": [
            {"id": "initech", "name": "Initech", "tier": "enterprise",
             "sip_domains": ["initech.eu1.platform.example"]},
            {"id": "initech-de", "name": "Initech DE", "parent": "initech", "tier": "small",
             "placement": {"home_zone": "eu1", "zone": "eu1", "cell": "eu1a", "deployment_unit": "bin-a"},
             "sip_domains": ["initech.eu1.platform.example"]}
        ]
    });
    let found: Vec<String> = contract_check::tenants::violations(&schemas, &file)
        .iter()
        .map(ToString::to_string)
        .collect();
    assert_eq!(
        found,
        vec![
            "parent_takes_traffic at /tenants/0",
            "tier_mismatch at /tenants/1/tier",
            "duplicate_sip_domain at /tenants/1/sip_domains/0",
        ]
    );
}

// parity-store's Kafka contracts (0.5.0).

#[test]
fn a_store_write_on_the_read_topic_must_be_forbidden() {
    let dir = scratch("store-reader");
    edit(
        &dir.join("fixtures/store.v1/invalid/request-write-on-read-topic.json"),
        |v| v["reader"] = "apply".into(),
    );
    let e = errors(&dir, None);
    assert!(
        has(
            &e,
            "request-write-on-read-topic.json",
            "reply_error:forbidden"
        ),
        "{e:#?}"
    );
}

#[test]
fn a_store_command_without_a_fixture_fails_coverage() {
    let dir = scratch("store-coverage");
    fs::remove_file(dir.join("fixtures/store.v1/valid/request-get-digest.json")).unwrap();
    let e = errors(&dir, None);
    assert!(
        e.iter()
            .any(|e| e == "coverage: no valid store fixture for cmd get_digest"),
        "{e:#?}"
    );
}

#[test]
fn a_state_record_under_another_key_fails() {
    let dir = scratch("store-key");
    edit(
        &dir.join("fixtures/store.v1/valid/state-tenant.json"),
        |v| v["record"]["key"] = "tenant:globex".into(),
    );
    let e = errors(&dir, None);
    assert!(has(&e, "state-tenant.json", "key_mismatch"), "{e:#?}");
}

#[test]
fn a_write_command_without_x_admin_fails_the_lint() {
    let dir = scratch("store-admin-lint");
    edit(&dir.join("schemas/store/store-requests.v1.json"), |v| {
        v["$defs"]["cmd_add_number"]
            .as_object_mut()
            .unwrap()
            .remove("x-admin");
    });
    let e = errors(&dir, None);
    assert!(
        e.iter()
            .any(|e| e.contains("add_number is a write but lacks")),
        "{e:#?}"
    );
}

// ---- G5: executable routing.v1 scripts ----------------------------------------------------

const ROUTING: &str = "fixtures/routing.v1";

#[test]
fn a_valid_routing_call_breaking_a_writer_rule_fails() {
    let dir = scratch("routing-writer");
    edit(
        &dir.join(ROUTING).join("valid/race-abandon-vs-assign.json"),
        |v| {
            let step = v["steps"]
                .as_array_mut()
                .unwrap()
                .iter_mut()
                .find(|s| {
                    s["method"]
                        .as_str()
                        .is_some_and(|m| m.ends_with("/Dequeue"))
                })
                .unwrap();
            step["metadata"]["tenant-id"] = "globex".into();
        },
    );
    let e = errors(&dir, None);
    assert!(
        has(&e, "race-abandon-vs-assign.json", "tenant_mismatch"),
        "{e:#?}"
    );
}

#[test]
fn a_writer_invalid_routing_script_must_declare_what_it_breaks() {
    let dir = scratch("routing-invalid");
    edit(
        &dir.join(ROUTING).join("invalid/tenant-mismatch.json"),
        |v| {
            v["violates"] = serde_json::json!(["bad_epoch"]);
        },
    );
    let e = errors(&dir, None);
    assert!(
        has(&e, "tenant-mismatch.json", "declares {\"bad_epoch\"}"),
        "{e:#?}"
    );
}

#[test]
fn a_routing_delivery_going_back_fails() {
    let dir = scratch("routing-seq");
    edit(
        &dir.join(ROUTING).join("valid/race-abandon-vs-assign.json"),
        |v| {
            let steps = v["steps"].as_array_mut().unwrap();
            let first = steps.iter().position(|s| s["op"] == "deliver").unwrap();
            let mut early = steps[first].clone();
            early["body"]["delivery_seq"] = "2".into();
            steps.insert(first, early);
        },
    );
    let e = errors(&dir, None);
    assert!(
        has(
            &e,
            "race-abandon-vs-assign.json",
            "delivery_seq 1 goes back"
        ),
        "{e:#?}"
    );
}

#[test]
fn a_routing_replay_carries_the_current_fence_never_an_older_one() {
    // CR-054 (0.9.0): a replay is identical except target.owner_epoch, which may only rise.
    let replay = |raise: i64| {
        move |v: &mut Value| {
            let steps = v["steps"].as_array_mut().unwrap();
            let first = steps.iter().position(|s| s["op"] == "deliver").unwrap();
            let mut again = steps[first].clone();
            let epoch: i64 = again["body"]["assigned"]["target"]["owner_epoch"]
                .as_str()
                .unwrap()
                .parse()
                .unwrap();
            again["body"]["assigned"]["target"]["owner_epoch"] = (epoch + raise).to_string().into();
            steps.insert(first + 1, again);
        }
    };
    let dir = scratch("routing-replay-fence");
    edit(
        &dir.join(ROUTING).join("valid/race-abandon-vs-assign.json"),
        replay(1),
    );
    let e = errors(&dir, None);
    assert!(
        !e.iter().any(|e| e.contains("race-abandon-vs-assign.json")),
        "{e:#?}"
    );
    let dir = scratch("routing-replay-older-fence");
    edit(
        &dir.join(ROUTING).join("valid/race-abandon-vs-assign.json"),
        replay(-1),
    );
    let e = errors(&dir, None);
    assert!(
        has(
            &e,
            "race-abandon-vs-assign.json",
            "replayed with an older owner_epoch"
        ),
        "{e:#?}"
    );
}

#[test]
fn an_injected_agent_must_be_given() {
    let dir = scratch("routing-agent");
    edit(
        &dir.join(ROUTING).join("valid/race-cross-tenant-agent.json"),
        |v| {
            let step = v["steps"]
                .as_array_mut()
                .unwrap()
                .iter_mut()
                .find(|s| s["op"] == "inject")
                .unwrap();
            step["args"]["tenant_id"] = "acme".into();
        },
    );
    let e = errors(&dir, None);
    assert!(
        has(
            &e,
            "race-cross-tenant-agent.json",
            "agent acme/agent-g1 isn't given"
        ),
        "{e:#?}"
    );
}

#[test]
fn an_executable_routing_watch_names_its_stream_and_the_world_holds() {
    let dir = scratch("routing-world");
    edit(
        &dir.join(ROUTING).join("valid/race-duplicate-enqueue.json"),
        |v| {
            v["steps"][0].as_object_mut().unwrap().remove("stream");
            v["given"]["agents"][0]["queues"] = serde_json::json!(["billing"]);
        },
    );
    let e = errors(&dir, None);
    assert!(
        has(&e, "race-duplicate-enqueue.json", "name their stream")
            && has(
                &e,
                "race-duplicate-enqueue.json",
                "serves billing, not a queue of acme"
            ),
        "{e:#?}"
    );
}

#[test]
fn a_routing_given_with_an_unknown_key_fails_the_fixture_schema() {
    let dir = scratch("routing-given");
    edit(
        &dir.join(ROUTING)
            .join("valid/race-offer-timeout-vs-accept.json"),
        |v| v["given"]["skills"] = serde_json::json!([]),
    );
    let e = errors(&dir, None);
    assert!(
        has(
            &e,
            "race-offer-timeout-vs-accept.json",
            "not a valid fixture file"
        ),
        "{e:#?}"
    );
}

#[test]
fn routing_scenarios_are_checked() {
    // Unknown membership, a field the schema doesn't know, a missing canonical scenario.
    let dir = scratch("scenarios");
    edit(&dir.join("scenarios/routing/quiet-day.json"), |v| {
        v["tenants"][0]["agents"][0]["queues"] = serde_json::json!(["nowhere"]);
    });
    edit(&dir.join("scenarios/routing/busy-day.json"), |v| {
        v["surprise"] = serde_json::json!(1);
    });
    fs::remove_file(dir.join("scenarios/routing/burst-30s.json")).unwrap();
    let e = errors(&dir, None);
    assert!(has(&e, "quiet-day.json", "unknown queue"), "{e:#?}");
    assert!(
        has(&e, "busy-day.json", "not a valid routing-scenario.v1"),
        "{e:#?}"
    );
    assert!(
        e.iter().any(|x| x.contains("`burst-30s` is missing")),
        "{e:#?}"
    );
}

#[test]
fn a_max_wait_queue_chain_must_end_and_a_flood_names_its_queue() {
    // CR-057: sales -> sales-overflow -> sales would move a caller forever.
    let dir = scratch("scenarios-cycle");
    edit(&dir.join("scenarios/routing/max-wait-actions.json"), |v| {
        v["tenants"][0]["queues"][1]["max_wait_action"] =
            serde_json::json!({"action": "queue", "queue_id": "sales"});
    });
    edit(&dir.join("scenarios/routing/inbox-flood.json"), |v| {
        v["events"][0]["queue"] = "nowhere".into();
    });
    let e = errors(&dir, None);
    assert!(
        has(
            &e,
            "max-wait-actions.json",
            "chain sales -> sales-overflow -> sales never ends"
        ),
        "{e:#?}"
    );
    assert!(
        has(
            &e,
            "inbox-flood.json",
            "event `request_flood` names an unknown queue"
        ),
        "{e:#?}"
    );
}

#[test]
fn a_given_max_wait_chain_must_end_and_a_dropped_stream_must_be_declared() {
    // 0.9.0: given queues may carry a max-wait action; QUEUE chains never loop (CR-057).
    let dir = scratch("routing-given-max-wait");
    edit(&dir.join(ROUTING).join("valid/wrap-up-ended.json"), |v| {
        v["given"]["queues"][1]["max_wait_action"] = "OVERFLOW_ACTION_QUEUE".into();
        v["given"]["queues"][1]["max_wait_queue_id"] = "sales".into();
    });
    edit(
        &dir.join(ROUTING)
            .join("valid/race-replay-after-reconnect.json"),
        |v| {
            let step = v["steps"]
                .as_array_mut()
                .unwrap()
                .iter_mut()
                .find(|s| s["what"] == "stream_drop")
                .unwrap();
            step["args"]["stream"] = "r9".into();
        },
    );
    let e = errors(&dir, None);
    assert!(
        has(
            &e,
            "wrap-up-ended.json",
            "chain sales -> support -> sales never ends"
        ),
        "{e:#?}"
    );
    assert!(
        has(
            &e,
            "race-replay-after-reconnect.json",
            "stream_drop of undeclared stream r9"
        ),
        "{e:#?}"
    );
}

// ---- parity-billing's contracts (0.10.0) -------------------------------------------------------

#[test]
fn a_web_command_on_the_console_topic_must_be_forbidden() {
    let dir = scratch("billing-wrong-topic");
    edit(
        &dir.join("fixtures/billing.v1/valid/request-start-signup.json"),
        |v| v["record"]["topic"] = "billing.requests".into(),
    );
    let e = errors(&dir, None);
    assert!(
        has(&e, "request-start-signup.json", "wrong_topic"),
        "{e:#?}"
    );
}

#[test]
fn the_web_topic_answers_only_into_replies_parity_frontend() {
    let dir = scratch("billing-reply-to");
    edit(
        &dir.join("fixtures/billing.v1/invalid/request-web-other-reply-to.json"),
        |v| v["reader"] = "reply_error:bad_request".into(),
    );
    let e = errors(&dir, None);
    assert!(
        has(&e, "request-web-other-reply-to.json", "\"dlt\""),
        "{e:#?}"
    );
}

#[test]
fn an_entitlement_under_another_tenants_key_fails() {
    let dir = scratch("billing-key");
    edit(
        &dir.join("fixtures/billing.v1/valid/entitlements-active.json"),
        |v| v["record"]["key"] = "tenant:globex".into(),
    );
    let e = errors(&dir, None);
    assert!(
        has(&e, "entitlements-active.json", "key_mismatch"),
        "{e:#?}"
    );
}

#[test]
fn a_billing_command_without_a_fixture_fails_coverage() {
    let dir = scratch("billing-coverage");
    fs::remove_file(dir.join("fixtures/billing.v1/valid/request-confirm-checkout.json")).unwrap();
    fs::remove_file(dir.join("fixtures/billing.v1/valid/provisioning-get-seat-usage.json"))
        .unwrap();
    let e = errors(&dir, None);
    assert!(
        e.iter()
            .any(|e| e == "coverage: no valid billing fixture for cmd confirm_checkout"),
        "{e:#?}"
    );
    assert!(
        e.iter()
            .any(|e| e == "coverage: no valid billing fixture for provisioning get_seat_usage"),
        "{e:#?}"
    );
}

#[test]
fn a_test_entitlement_for_a_parent_fails() {
    let dir = scratch("billing-test-file");
    edit(&dir.join("tenants/test-entitlements.v1.json"), |v| {
        v["entitlements"][0]["tenant_id"] = "initech".into();
    });
    let e = errors(&dir, None);
    assert!(
        e.iter().any(|e| e.contains("initech is a parent")),
        "{e:#?}"
    );
}

#[test]
fn removing_a_released_test_entitlement_fails_the_break_gate() {
    let dir = scratch("billing-break");
    let baseline = dir.join("baseline");
    fs::create_dir_all(baseline.join("tenants")).unwrap();
    fs::copy(
        dir.join("tenants/test-entitlements.v1.json"),
        baseline.join("tenants/test-entitlements.v1.json"),
    )
    .unwrap();
    edit(&dir.join("tenants/test-entitlements.v1.json"), |v| {
        v["entitlements"].as_array_mut().unwrap().pop();
    });
    let e = errors(&dir, Some(&baseline));
    assert!(
        e.iter()
            .any(|e| e.contains("test entitlements of initech-uk were removed")),
        "{e:#?}"
    );
}

#[test]
fn a_log_fixture_declaring_the_wrong_rule_fails() {
    let dir = scratch("log-wrong-rule");
    let file = dir.join("fixtures/log-record.v1/invalid/xsid-v4.json");
    edit(&file, |f| {
        f["violates"] = serde_json::json!(["missing_xsid"])
    });
    let e = errors(&dir, None);
    assert!(has(&e, "xsid-v4.json", "breaks {\"bad_xsid\"}"), "{e:?}");
}

#[test]
fn captured_ndjson_is_checked_line_by_line_and_a_call_can_be_followed() {
    let root = repo();
    let schemas = contract_check::Schemas::load(&root).unwrap();
    let masking = contract_check::logs::Masking::load(&root).unwrap();
    let flow: Value = serde_json::from_str(
        &fs::read_to_string(root.join("fixtures/log-record.v1/valid/call-across-services.json"))
            .unwrap(),
    )
    .unwrap();
    let files: Vec<PathBuf> = flow["catalogues"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| root.join(p.as_str().unwrap()))
        .collect();
    let cats = contract_check::logs::Catalogues::load(&root, &schemas, &files).unwrap();
    let mut text: String = flow["records"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| format!("{r}\n"))
        .collect();
    let mut checker = contract_check::logs::Checker::new(&schemas, &cats, &masking);
    let (n, found) = contract_check::logs::check_ndjson(&mut checker, &text, false);
    assert_eq!((n, found.len()), (19, 0), "{found:?}");
    // A test runner's own line, then a record repeating the first seq: line numbers are kept.
    text.push_str("[INFO] Tests run: 3\n");
    text.push_str(&format!("{}\n", flow["records"][0]));
    let mut checker = contract_check::logs::Checker::new(&schemas, &cats, &masking);
    let (n, found) = contract_check::logs::check_ndjson(&mut checker, &text, true);
    let codes: Vec<(usize, &str)> = found.iter().map(|(l, v)| (*l, v.code)).collect();
    assert_eq!((n, codes), (20, vec![(21, "seq_repeat")]));
    let timeline = contract_check::logs::flow(&[text], "0192f3c1-6a2b-7c3d-8e4f-5a6b7c8d9e01");
    assert_eq!(timeline.len(), 20);
    assert!(timeline[0].contains("session.arrived"), "{timeline:?}");
}
