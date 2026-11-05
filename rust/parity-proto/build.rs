//! Compiles parity-contracts' protos with protox (pure Rust) and generates prost messages and
//! tonic clients (servers with the `server` feature). The descriptor set is kept for prost-reflect.

use std::path::{Path, PathBuf};

const FILES: [&str; 2] = [
    "parity/routing/v1/routing.proto",
    "parity/telephony/v1/telephony.proto",
];

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../proto");
    for f in FILES {
        println!("cargo:rerun-if-changed={}", root.join(f).display());
    }
    let fds = protox::compile(FILES, [&root])?;
    let out = PathBuf::from(std::env::var("OUT_DIR")?);
    std::fs::write(
        out.join("contracts.binpb"),
        prost::Message::encode_to_vec(&fds),
    )?;
    tonic_prost_build::configure()
        .build_server(std::env::var_os("CARGO_FEATURE_SERVER").is_some())
        .build_client(true)
        .compile_fds(fds)?;
    Ok(())
}
