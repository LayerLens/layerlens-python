#!/usr/bin/env bash
# Scorer lifecycle: create, list, inspect, delete
#
# Usage: ./07_scorer_lifecycle.sh <MODEL_ID>
set -euo pipefail

MODEL_ID="${1:?Usage: $0 <MODEL_ID>}"
SCORER_NAME="CLI Demo $(date +%s)"

# Create (dry-run)
echo "==> Dry-run create:"
layerlens scorer create \
  --name "$SCORER_NAME" \
  --description "Evaluates generated code for correctness and readability" \
  --model-id "$MODEL_ID" \
  --prompt "Rate the following code on a 1-10 scale for correctness, readability, and adherence to best practices." \
  --dry-run

# Create for real, and take the new scorer's id from the JSON output
echo ""
echo "==> Creating scorer..."
# When the CLI fails, it has printed why and `set -e` stops here, before anything parses its
# empty output.
CREATED=$(layerlens --format json scorer create \
  --name "$SCORER_NAME" \
  --description "Evaluates generated code for correctness and readability" \
  --model-id "$MODEL_ID" \
  --prompt "Rate the following code on a 1-10 scale for correctness, readability, and adherence to best practices.")
SCORER_ID=$(printf '%s' "$CREATED" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "==> Scorer ID: $SCORER_ID"

# List (the first page; scorers and judges share the list)
echo ""
echo "==> Scorers:"
layerlens scorer list --page-size 5

# Inspect
layerlens scorer get "$SCORER_ID"

# Delete
echo ""
echo "==> Cleaning up..."
layerlens scorer delete "$SCORER_ID" -y
echo "==> Done."
