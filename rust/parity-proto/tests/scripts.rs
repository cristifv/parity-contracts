//! IE's way of consuming the scripts: JSON → prost-reflect `DynamicMessage` → the generated type.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use parity_proto::telephony::v1 as tel;
use prost_reflect::{DescriptorPool, DynamicMessage};
use serde_json::Value;
use std::path::Path;

fn scripts() -> Vec<Value> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/telephony.v1");
    let mut out = Vec::new();
    for sub in ["valid", "invalid"] {
        for entry in std::fs::read_dir(dir.join(sub)).unwrap() {
            let text = std::fs::read_to_string(entry.unwrap().path()).unwrap();
            out.push(serde_json::from_str(&text).unwrap());
        }
    }
    out
}

#[test]
fn every_delivered_message_transcodes_into_the_generated_type() {
    let pool = DescriptorPool::decode(parity_proto::FILE_DESCRIPTOR_SET).unwrap();
    let desc = pool
        .get_message_by_name("parity.telephony.v1.WatchResponse")
        .unwrap();
    let mut n = 0;
    for script in scripts() {
        for step in script["steps"].as_array().unwrap() {
            if step["op"] == "deliver" {
                let m = DynamicMessage::deserialize(desc.clone(), step["body"].clone()).unwrap();
                let w: tel::WatchResponse = m.transcode_to().unwrap();
                assert!(w.event.is_some());
                n += 1;
            }
        }
    }
    assert!(n > 50, "only {n} deliveries");
}

#[test]
fn add_leg_calls_transcode_and_a_client_type_exists() {
    let pool = DescriptorPool::decode(parity_proto::FILE_DESCRIPTOR_SET).unwrap();
    let desc = pool
        .get_message_by_name("parity.telephony.v1.AddLegRequest")
        .unwrap();
    let mut n = 0;
    for script in scripts() {
        for step in script["steps"].as_array().unwrap() {
            if step["method"]
                .as_str()
                .is_some_and(|m| m.ends_with("/AddLeg"))
            {
                let m = DynamicMessage::deserialize(desc.clone(), step["body"].clone()).unwrap();
                let r: tel::AddLegRequest = m.transcode_to().unwrap();
                assert!(!r.leg_id.is_empty());
                n += 1;
            }
        }
    }
    assert!(n > 10, "only {n} AddLeg calls");
    // The client IE builds on (tonic's channel transport).
    let _: Option<
        tel::telephony_service_client::TelephonyServiceClient<tonic::transport::Channel>,
    > = None;
}
