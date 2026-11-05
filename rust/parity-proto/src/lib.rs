//! parity-contracts' protos for Rust (TC ADR 0001 §7): prost messages and tonic clients for
//! `parity.routing.v1` and `parity.telephony.v1`, generated at build time from `../../proto` by
//! protox (no `protoc`). Servers come with the `server` feature. Consumers take this crate by path
//! until there is git, then by tag.

/// `parity.routing.v1`: interaction-engine ↔ routing-engine.
#[allow(clippy::all, clippy::pedantic, missing_docs)]
pub mod routing {
    /// Version 1.
    pub mod v1 {
        tonic::include_proto!("parity.routing.v1");
    }
}

/// `parity.telephony.v1`: interaction-engine ↔ parity-telephony-controller.
#[allow(clippy::all, clippy::pedantic, missing_docs)]
pub mod telephony {
    /// Version 1.
    pub mod v1 {
        tonic::include_proto!("parity.telephony.v1");
    }
}

/// Every proto's descriptor set, for prost-reflect: fixture runners read a script's JSON into a
/// `DynamicMessage` and transcode it into the generated type.
pub const FILE_DESCRIPTOR_SET: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/contracts.binpb"));
