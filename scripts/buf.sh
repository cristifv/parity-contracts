#!/usr/bin/env bash
# Runs buf in the hardened container (ADR 0001 §8.2). Only build/ is writable; release.sh
# stages the new baseline there.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=tools.env
source "$ROOT/scripts/tools.env"
mkdir -p "$ROOT/build" "$ROOT/.baseline"
exec docker run --rm --network none --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges --security-opt label=disable \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$ROOT/buf.yaml:/workspace/buf.yaml:ro" \
  -v "$ROOT/proto:/workspace/proto:ro" \
  -v "$ROOT/.baseline:/workspace/.baseline:ro" \
  -v "$ROOT/build:/workspace/build" \
  -w /workspace "$BUF_IMAGE" "$@"
