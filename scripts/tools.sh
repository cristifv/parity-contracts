#!/usr/bin/env bash
# Installs what parity-contracts' CI needs (ADR 0001 §2.6): the digest-pinned buf image and
# cargo-deny into ~/.cargo/bin. Never uses sudo, never touches another repository.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=tools.env
source "$ROOT/scripts/tools.env"
if [[ "$(cargo deny --version 2>/dev/null || true)" != "cargo-deny $CARGO_DENY_VERSION" ]]; then
  cargo install --locked cargo-deny --version "$CARGO_DENY_VERSION"
fi
docker pull "$BUF_IMAGE"
(cd "$ROOT/rust" && rustup show active-toolchain >/dev/null)
echo "tools ready"
