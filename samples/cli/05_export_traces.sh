#!/usr/bin/env bash
# Export all traces to individual JSON files
#
# Usage: ./05_export_traces.sh [OUTPUT_DIR]
set -euo pipefail

OUTPUT_DIR="${1:-./exported_traces}"
mkdir -p "$OUTPUT_DIR"

echo "==> Exporting traces to $OUTPUT_DIR/"

# List first: when the CLI fails (a wrong key, LayerLens unreachable), it has printed why and
# `set -e` stops here, before anything parses its empty output.
TRACES_JSON=$(layerlens --format json trace list)
printf '%s' "$TRACES_JSON" | python3 -c "
import sys, json
for t in json.load(sys.stdin):
    print(t['id'])
" | while read -r id; do
  echo "  Exporting $id..."
  # A key with Standard access may list traces it cannot read (an environment
  # run's attempt traces need an Automation key): skip a trace the API refuses
  # (HTTP 403) or can't find (HTTP 404), and stop on any other error, such as an
  # answer the CLI could not read.
  if ! err=$(layerlens trace export "$id" -o "$OUTPUT_DIR/${id}.json" 2>&1 >/dev/null); then
    case "$err" in
      *"(HTTP 403)"* | *"(HTTP 404)"*) echo "  Skipped $id: $err" ;;
      *) echo "$err" >&2; exit 1 ;;
    esac
  fi
done

echo "==> Done. Files in $OUTPUT_DIR/"
ls -la "$OUTPUT_DIR/"
