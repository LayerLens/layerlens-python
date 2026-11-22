#!/usr/bin/env bash
# Judge workflow: create, test, review
#
# Usage: ./03_judge_workflow.sh <TRACE_ID> [MODEL_ID]
# MODEL_ID is the judge's model; it defaults to GPT-4o-mini (openai/gpt-4o-mini).
# A judge on a model the catalog marks deprecated is created, but refused when it runs.
set -euo pipefail

TRACE_ID="${1:?Usage: $0 <TRACE_ID> [MODEL_ID]}"
MODEL_ID="${2:-6a0b692ced57a8fd01d1433a}"

# Create a judge (capture ID from the "Judge created: <id>" line)
echo "==> Creating judge..."
JUDGE_ID=$(layerlens judge create \
  --name "Response Quality $(date +%s)" \
  --goal "Rate the response for accuracy, completeness, and clarity on a 1-5 scale" \
  --model-id "$MODEL_ID" \
  | grep "^Judge created:" | awk '{print $NF}')
echo "==> Judge ID: $JUDGE_ID"

# Test against a trace
echo "==> Testing judge..."
layerlens judge test --judge-id "$JUDGE_ID" --trace-id "$TRACE_ID"

# Review judge details
echo "==> Judge details:"
layerlens judge get "$JUDGE_ID"
