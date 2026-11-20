#!/usr/bin/env bash
# Run an evaluation and wait for results
set -euo pipefail

MODEL="${1:-openai/gpt-4o}"
BENCHMARK="${2:-arc-agi-2}"

echo "==> Running evaluation: $MODEL on $BENCHMARK"
# --wait exits 12 when the evaluation ends in anything but success, and 1 when it could
# not run: keep the status, list the evaluations, then exit with it.
status=0
layerlens evaluate run \
  --model "$MODEL" \
  --benchmark "$BENCHMARK" \
  --wait || status=$?

echo "==> Recent evaluations:"
layerlens evaluate list --sort-by submitted_at --order desc --page-size 5

if [ "$status" -eq 12 ]; then
  echo "==> The evaluation did not succeed." >&2
fi
exit "$status"
