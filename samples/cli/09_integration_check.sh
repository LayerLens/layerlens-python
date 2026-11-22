#!/usr/bin/env bash
# Check integration health
set -euo pipefail

echo "==> Integrations:"
layerlens integration list

# Test each integration (skip if none found)
echo ""
echo "==> Testing all integrations..."
OUTPUT=$(layerlens --format json integration list 2>/dev/null)
if echo "$OUTPUT" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
  echo "$OUTPUT" | python3 -c "
import sys, json
for i in json.load(sys.stdin):
    print(i['id'], i.get('name', ''))
" | while read -r id name; do
    echo "  Testing $name ($id)..."
    # The API tests Langfuse integrations only: another kind is skipped, not a failure.
    if err=$(layerlens integration test "$id" 2>&1 >/dev/null); then
      echo "  OK: $name"
    else
      case "$err" in
        *"only supported for langfuse"*) echo "  Skipped: $name (the API tests Langfuse integrations only)" ;;
        *) echo "  FAILED: $name: $err" ;;
      esac
    fi
  done
else
  echo "  No integrations to test."
fi
