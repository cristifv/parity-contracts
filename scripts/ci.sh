#!/usr/bin/env bash
# parity-contracts local CI (ADR 0001 §8.2). Stops at the first failing stage.
#   CI_OFFLINE=1   cargo --offline, cargo-deny without fetching its advisory database
#   CI_JAVA=1      also run the optional networknt 3.0.8 stage (needs Maven)
#   CI_ALLOW_BREAK=1  report breaking changes without failing. Honoured only from
#                     `release.sh --reset-baseline` while VERSION is 0.x (G3): release.sh
#                     passes a one-time token that must match build/release.token.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=tools.env
source "$ROOT/scripts/tools.env"
step() { printf '== %s\n' "$*"; }
die() { printf 'ci: %s\n' "$*" >&2; exit 3; }
if [[ "${CI_ALLOW_BREAK:-0}" == 1 ]]; then
  token_file="$ROOT/build/release.token"
  [[ -n "${PARITY_RELEASE_TOKEN:-}" && -f "$token_file" && "$(cat "$token_file")" == "$PARITY_RELEASE_TOKEN" ]] \
    || die "CI_ALLOW_BREAK is only honoured from release.sh --reset-baseline"
  [[ "$(cut -d. -f1 "$ROOT/VERSION")" == 0 ]] || die "CI_ALLOW_BREAK is only honoured during 0.x"
fi
cargo_flags=(--locked)
deny_flags=()
if [[ "${CI_OFFLINE:-0}" == 1 ]]; then cargo_flags+=(--offline); deny_flags+=(--disable-fetch); fi
gate() { # a breaking-change gate: fails unless CI_ALLOW_BREAK=1
  if "$@"; then return 0; fi
  [[ "${CI_ALLOW_BREAK:-0}" == 1 ]] || exit 1
  echo "ci: breaking change allowed for a 0.x baseline reset"
}

step "tools"
[[ ! -e "$ROOT/.baseline.old" ]] || die "an interrupted release left .baseline.old: restore it as .baseline"
if [[ "$(cat "$ROOT/VERSION")" != 0.0.0 && ! -f "$ROOT/.baseline/buf.binpb" ]]; then
  die "VERSION is $(cat "$ROOT/VERSION") but .baseline/ is missing: the break gates can't run"
fi
command -v docker >/dev/null || die "docker is missing"
command -v cargo >/dev/null || die "cargo is missing"
[[ "$BUF_IMAGE" == *@sha256:* ]] || die "BUF_IMAGE in tools.env must carry a digest"
digests=$(docker image inspect --format '{{join .RepoDigests " "}}' "$BUF_IMAGE" 2>/dev/null) \
  || die "the buf image $BUF_IMAGE isn't present: run scripts/tools.sh"
[[ " $digests " == *"@${BUF_IMAGE##*@} "* ]] || die "the local buf image has digests '$digests', not ${BUF_IMAGE##*@}"
deny_version=$(cargo deny --version 2>/dev/null || true)
[[ "$deny_version" == "cargo-deny $CARGO_DENY_VERSION" ]] \
  || die "need cargo-deny $CARGO_DENY_VERSION, found '${deny_version:-none}': run scripts/tools.sh"
echo "buf ${BUF_IMAGE#*:}; $deny_version"

step "proto format"
"$ROOT/scripts/buf.sh" format --diff --exit-code
step "proto lint"
"$ROOT/scripts/buf.sh" lint
step "proto breaking"
if [[ -f "$ROOT/.baseline/buf.binpb" ]]; then
  gate "$ROOT/scripts/buf.sh" breaking --against .baseline/buf.binpb
else
  echo "no baseline yet (before the first release)"
fi
step "descriptor set"
"$ROOT/scripts/buf.sh" build --as-file-descriptor-set -o build/contracts.binpb

step "tool checks"
(
  cd "$ROOT/rust"
  cargo fmt --all -- --check
  cargo clippy "${cargo_flags[@]}" --all-targets -- -D warnings
  cargo test "${cargo_flags[@]}" --quiet
  cargo deny --locked check --hide-inclusion-graph "${deny_flags[@]}" advisories bans licenses sources
)

step "fixtures"
check=("$ROOT/rust/target/debug/contract-check" --root "$ROOT" --fds "$ROOT/build/contracts.binpb")
(cd "$ROOT/rust" && cargo build "${cargo_flags[@]}" --quiet -p contract-check)
"${check[@]}"

step "forecaster scenarios (the training catalogue's ranges; the OOD suite stays outside them)"
python3 -I "$ROOT/scripts/forecast_scenarios.py" check --catalogue "$ROOT/scenarios/forecast-training/catalogue.json"
python3 -I "$ROOT/scripts/forecast_scenarios.py" leakage --catalogue "$ROOT/scenarios/forecast-training/catalogue.json" \
  --ood "$ROOT/scenarios/forecast-ood" --samples 60

step "schema break gate"
if [[ -d "$ROOT/.baseline/fixtures" ]]; then
  gate "${check[@]}" --baseline "$ROOT/.baseline/fixtures"
else
  echo "no baseline yet (before the first release)"
fi

if [[ "${CI_JAVA:-0}" == 1 ]]; then
  step "java (networknt 3.0.8)"
  command -v mvn >/dev/null || die "CI_JAVA=1 needs Maven"
  mvn -q -f "$ROOT/java-check/pom.xml" -Dparity.contracts.dir="$ROOT" verify
fi

step "parity-contracts ok"
