#!/usr/bin/env bash
# Cuts a contracts version (ADR 0001 §8.2): runs CI, then writes the baseline
# (.baseline/buf.binpb, the valid Kafka fixtures, the valid telephony.v1 scripts and, since 0.4.0,
# the valid tenant maps fixtures and the shared test tenants), then VERSION and the changelog
# heading.
#   scripts/release.sh <x.y.z> [--reset-baseline]
# --reset-baseline (0.x only) accepts a breaking change; the changelog's Unreleased section
# must then contain a "- Breaking:" line and a "- Consumers:" line naming who is affected.
#
# Order (G4): everything is staged in build/release/ first; the baseline is swapped in, then
# VERSION, then CHANGELOG.md, each by a rename. A failure before the swap changes nothing.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
die() { printf 'release: %s\n' "$*" >&2; exit 2; }
version=${1:-}
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "usage: release.sh <x.y.z> [--reset-baseline]"
reset=0
[[ "${2:-}" == "--reset-baseline" ]] && reset=1
current=$(cat "$ROOT/VERSION")
unreleased=$(awk '/^## Unreleased/{f=1;next} /^## /{f=0} f' "$ROOT/CHANGELOG.md")
[[ -n "$(echo "$unreleased" | tr -d '[:space:]')" ]] || die "CHANGELOG.md has nothing under '## Unreleased'"

mkdir -p "$ROOT/build"
if (( reset )); then
  [[ "${current%%.*}" == 0 && "${version%%.*}" == 0 ]] || die "--reset-baseline is only allowed during 0.x"
  grep -q '^- Breaking:' <<<"$unreleased" || die "a reset needs a '- Breaking:' line under Unreleased"
  grep -q '^- Consumers:' <<<"$unreleased" || die "a reset needs a '- Consumers:' line naming the affected consumers"
  # G3: the one-time token that lets ci.sh honour CI_ALLOW_BREAK for this run only.
  token=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
  printf '%s' "$token" > "$ROOT/build/release.token"
  trap 'rm -f "$ROOT/build/release.token"' EXIT
  CI_ALLOW_BREAK=1 PARITY_RELEASE_TOKEN="$token" "$ROOT/scripts/ci.sh"
  rm -f "$ROOT/build/release.token"
else
  "$ROOT/scripts/ci.sh"
fi

# Stage.
stage="$ROOT/build/release"
rm -rf "$stage"
mkdir -p "$stage/baseline/fixtures"
"$ROOT/scripts/buf.sh" build -o build/release/baseline/buf.binpb
while IFS= read -r f; do
  # Valid Kafka records, (since 0.2.1) valid telephony.v1 scripts, (since 0.4.0) valid tenant
  # maps files, (since 0.5.0) valid parity-store records, (since 0.10.0) valid parity-billing
  # records, (since 0.13.0) valid log records and (since 0.16.0) valid tenant routing records: the
  # break gates' baseline.
  kind=$(jq -r '.kind + ":" + (.writer_valid|tostring) + ":" + (.package // "")' "$f")
  if [[ "$kind" == kafka:true:* || "$kind" == "grpc:true:parity.telephony.v1" || "$kind" == tenant_maps:true:* || "$kind" == store:true:* || "$kind" == billing:true:* || "$kind" == log:true:* || "$kind" == routing_ops:true:* ]]; then
    rel=${f#"$ROOT/fixtures/"}
    mkdir -p "$stage/baseline/fixtures/$(dirname "$rel")"
    cp "$f" "$stage/baseline/fixtures/$rel"
  fi
done < <(find "$ROOT/fixtures" -name '*.json' | sort)
# The shared test tenants (0.4.0): the break gate keeps every released tenant, number, domain, agent.
mkdir -p "$stage/baseline/fixtures/tenants"
cp "$ROOT/tenants/test-tenants.v1.json" "$stage/baseline/fixtures/tenants/test-tenants.v1.json"
# The shared test entitlements (0.10.0): the break gate keeps every released record.
cp "$ROOT/tenants/test-entitlements.v1.json" "$stage/baseline/fixtures/tenants/test-entitlements.v1.json"
echo "$version" > "$stage/VERSION"
today=$(date +%Y-%m-%d)
awk -v v="$version" -v d="$today" '/^## Unreleased/ && !done {print "## Unreleased\n\n## " v " (" d ")"; done=1; next} {print}' \
  "$ROOT/CHANGELOG.md" > "$stage/CHANGELOG.md"

# Swap in: the baseline first, then VERSION, then the changelog.
rm -rf "$ROOT/.baseline.old"
[[ -d "$ROOT/.baseline" ]] && mv "$ROOT/.baseline" "$ROOT/.baseline.old"
mv "$stage/baseline" "$ROOT/.baseline"
rm -rf "$ROOT/.baseline.old"
mv "$stage/VERSION" "$ROOT/VERSION"
mv "$stage/CHANGELOG.md" "$ROOT/CHANGELOG.md"
rm -rf "$stage"
echo "released $version: baseline written to .baseline/"
