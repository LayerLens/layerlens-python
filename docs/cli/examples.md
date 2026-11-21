# CLI — Workflow Examples

Fourteen copy-paste workflows covering the most common CLI tasks.

---

## 1. Quick start: first trace to evaluation

Set up, list traces, inspect one, and evaluate it with a judge.

```bash
# Configure
export LAYERLENS_API_KEY="sk-..."

# See what traces are available
layerlens trace list

# Inspect a specific trace
layerlens trace get <TRACE_ID>

# Create a judge and test it against the trace
layerlens judge create --name "Accuracy" --goal "Rate factual accuracy of the response" --model-id <MODEL_ID>
layerlens judge test --judge-id <JUDGE_ID> --trace-id <TRACE_ID>
```

---

## 2. Run an evaluation and wait for results

```bash
# Run and block until done
layerlens evaluate run \
  --model openai/gpt-4o \
  --benchmark arc-agi-2 \
  --wait

# Or fire and check later
layerlens evaluate run --model openai/gpt-4o --benchmark arc-agi-2
layerlens evaluate list --status in-progress
layerlens evaluate get <EVAL_ID>
```

---

## 3. Compare models on the same benchmark

```bash
layerlens evaluate run --model openai/gpt-4o --benchmark arc-agi-2 --wait
layerlens evaluate run --model anthropic/claude-3-opus --benchmark arc-agi-2 --wait

# List results sorted by accuracy
layerlens evaluate list --sort-by accuracy --order desc
```

---

## 4. Judge workflow: create, test, tune, apply

```bash
# Create a judge
layerlens judge create \
  --name "Helpfulness" \
  --goal "Rate how helpful and actionable the response is on a 1-5 scale" \
  --model-id <MODEL_ID>

# Test against a sample trace
layerlens judge test --judge-id <JUDGE_ID> --trace-id <TRACE_ID>

# Review the result
layerlens --format json judge get <JUDGE_ID>

# Iterate: create a refined version
layerlens judge create \
  --name "Helpfulness v2" \
  --goal "Rate helpfulness on a 1-5 scale with justification" \
  --model-id <MODEL_ID>
```

---

## 5. Trace search and export

```bash
# Search for traces matching a keyword
layerlens trace search "customer support" --page-size 5

# Export a trace to a file
layerlens trace export <TRACE_ID> -o trace_backup.json

# Export as JSON for piping
layerlens --format json trace get <TRACE_ID> | jq '.id'
```

---

## 6. Bulk evaluation from a JSONL file

```bash
# Create a jobs file
cat > jobs.jsonl <<'EOF'
{"model": "openai/gpt-4o", "benchmark": "arc-agi-2"}
{"model": "openai/gpt-4o-mini", "benchmark": "arc-agi-2"}
{"model": "anthropic/claude-3-opus", "benchmark": "arc-agi-2"}
EOF

# Dry-run to preview
layerlens bulk eval --file jobs.jsonl --dry-run

# Execute and wait: exits 12 if an evaluation ends in anything but success,
# and 1 if a job could not run (the other jobs still run)
layerlens bulk eval --file jobs.jsonl --wait
```

---

## 7. Bulk trace evaluation with a judge

```bash
# Create a trace ID file
layerlens --format json trace list | jq -r '.[].id' > trace_ids.txt

# Dry-run to preview
layerlens bulk eval \
  --judge-id <JUDGE_ID> \
  --traces trace_ids.txt \
  --dry-run

# Run trace evaluations for all traces
layerlens bulk eval \
  --judge-id <JUDGE_ID> \
  --traces trace_ids.txt
```

---

## 8. CI/CD pipeline integration

```bash
# In a GitHub Actions step (bash -e): keep the exit status. `evaluate run --wait` exits 12
# when the evaluation ends in anything but success, and 1 when it could not run.
status=0
layerlens --format json evaluate run \
  --model openai/gpt-4o \
  --benchmark arc-agi-2 \
  --wait > evaluation.json || status=$?
EVAL_ID=$(jq -r '.id // empty' evaluation.json)

# Summarise that evaluation on the GitHub job page (appended, so earlier steps' summaries stay),
# even when it failed
if [ -n "$EVAL_ID" ]; then
  layerlens ci report --evaluation-id "$EVAL_ID" -o "$GITHUB_STEP_SUMMARY"
fi

# Or as JSON for custom processing, with a trace evaluation and an environment run
# layerlens --format json ci report --evaluation-id "$EVAL_ID" \
#   --trace-evaluation-id <TRACE_EVALUATION_ID> --env-run support-desk:<RUN_ID> -o report.json

# Fail the step when the evaluation failed or could not run
exit "$status"
```

`ci report` exits `0` whatever the results are: the step fails through `evaluate run`'s exit
status. See [`ci report`](commands.md#ci-report) for a workflow that reports in a step of its own.

---

## 9. Integration monitoring

```bash
# List all integrations
layerlens integration list

# Test a specific integration
layerlens integration test <INTEGRATION_ID>

# JSON output for scripting
layerlens --format json integration list | jq '.[] | select(.status != "active")'
```

---

## 10. Scorer management

```bash
# List existing scorers
layerlens scorer list

# Create a scorer with dry-run
layerlens scorer create \
  --name "Code Quality" \
  --description "Evaluates generated code for correctness, readability, and best practices" \
  --model-id <MODEL_ID> \
  --prompt "Score the following code on a 1-10 scale for quality..." \
  --dry-run

# Create for real
layerlens scorer create \
  --name "Code Quality" \
  --description "Evaluates generated code for correctness, readability, and best practices" \
  --model-id <MODEL_ID> \
  --prompt "Score the following code on a 1-10 scale for quality..."

# Delete with confirmation
layerlens scorer delete <SCORER_ID>

# Delete without prompt
layerlens scorer delete <SCORER_ID> -y
```

---

## 11. JSON output and scripting

```bash
# Pipe trace IDs into a loop (`trace export -o` does not create the folder)
mkdir -p traces
layerlens --format json trace list | jq -r '.[].id' | while read id; do
  echo "Exporting $id..."
  layerlens trace export "$id" -o "traces/${id}.json"
done

# Get evaluation accuracy as a number
ACCURACY=$(layerlens --format json evaluate get <EVAL_ID> | jq -r '.accuracy')
echo "Accuracy: $ACCURACY"

# Filter evaluations by status
layerlens --format json evaluate list | jq '[.[] | select(.status == "success")]'
```

---

## 12. Pagination and sorting

```bash
# Page through traces
layerlens trace list --page 1 --page-size 20
layerlens trace list --page 2 --page-size 20

# Sort evaluations
layerlens evaluate list --sort-by accuracy --order desc --page-size 5
layerlens evaluate list --sort-by submitted_at --order asc

```

---

## 13. Verbose mode and debugging

```bash
# Verbose output: list commands add a count line ("Showing 100 of 3260 traces"), and an
# unexpected error prints its traceback. Requests themselves are not printed.
layerlens -v trace list

# Combine with JSON output
layerlens -v --format json evaluate get <EVAL_ID>

# Debug authentication issues
layerlens -v integration list
```

---

## 14. Clean up resources

```bash
# Delete a trace (with confirmation prompt)
layerlens trace delete <TRACE_ID>

# Delete without prompting
layerlens trace delete <TRACE_ID> -y

# Delete a scorer (dry-run first)
layerlens scorer delete <SCORER_ID> --dry-run
layerlens scorer delete <SCORER_ID> -y

```
