#!/usr/bin/env bash
# Compare multiple models on the same benchmark
#
# Usage: ./10_compare_models.sh [BENCHMARK] [MODEL1] [MODEL2] ...
set -euo pipefail

BENCHMARK="${1:-arc-agi-2}"
shift 2>/dev/null || true

if [ $# -eq 0 ]; then
  MODELS=("openai/gpt-4o" "openai/gpt-4o-mini")
else
  MODELS=("$@")
fi

echo "==> Comparing ${#MODELS[@]} models on $BENCHMARK"

pids=()
for model in "${MODELS[@]}"; do
  echo "  Running: $model"
  layerlens evaluate run --model "$model" --benchmark "$BENCHMARK" --wait &
  pids+=("$!")
done

# Wait for each evaluation. A bare `wait` would return 0 whatever they did; `evaluate run
# --wait` exits 12 when an evaluation ends in anything but success, and 1 when it could not run.
failed=0
for pid in "${pids[@]}"; do
  wait "$pid" || failed=$((failed + 1))
done

echo ""
echo "==> Results (sorted by accuracy):"
layerlens evaluate list --sort-by accuracy --order desc --page-size 10

if [ "$failed" -gt 0 ]; then
  echo "==> $failed of ${#MODELS[@]} evaluations did not succeed." >&2
  exit 1
fi
