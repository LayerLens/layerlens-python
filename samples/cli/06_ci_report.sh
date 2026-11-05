#!/usr/bin/env bash
# Summarise an evaluation in a CI report (for GitHub Actions, pass "$GITHUB_STEP_SUMMARY")
#
# Usage: ./06_ci_report.sh <EVALUATION_ID> [OUTPUT]
set -euo pipefail

EVALUATION_ID="${1:?Usage: $0 <EVALUATION_ID> [OUTPUT]}"
OUTPUT="${2:-summary.md}"

echo "==> Generating CI report..."
layerlens ci report --evaluation-id "$EVALUATION_ID" -o "$OUTPUT"

echo "==> Report appended to $OUTPUT"
cat "$OUTPUT"
