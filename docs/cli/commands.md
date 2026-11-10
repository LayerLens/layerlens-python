# CLI — Command Reference

Complete reference for all `layerlens` CLI commands.

## Command tree

```
layerlens [global-options]
├── trace
│   ├── list          List traces
│   ├── get           Get a trace by ID
│   ├── search        Search traces
│   ├── export        Export a trace as JSON
│   └── delete        Delete a trace
├── judge
│   ├── list          List judges
│   ├── get           Get a judge by ID
│   ├── create        Create a new judge
│   └── test          Test a judge against a trace
├── evaluate
│   ├── list          List evaluations
│   ├── get           Get an evaluation by ID
│   └── run           Run a new evaluation
├── evaluations
│   ├── run           Run a dataset through a target locally
│   └── compare       Compare two saved runs; exit 1 on a regression
├── integration
│   ├── list          List integrations
│   └── test          Test an integration
├── scorer
│   ├── list          List scorers
│   ├── get           Get a scorer by ID
│   ├── create        Create a new scorer
│   └── delete        Delete a scorer
├── bulk
│   └── eval          Run evaluations in bulk
├── ci
│   └── report        Summarise the evaluations and runs a CI step names
├── replay
│   └── run           Replay a stored or exported trace with overrides
├── env               Environments: create, run your agent or a model, gate a build (see env.md)
│   ├── world-types, list, get, create, status, wait, tools, reset, revert, delete
│   ├── tasks         list, generate, delete
│   └── run           exec, model, start, submit, finish, status, wait, results, cancel
└── completion        Print shell completion setup
```

---

## Global options

These options are available on every command, either right after `layerlens` or at
the end of the command (`layerlens --format json judge get <id>` or
`layerlens judge get <id> --format json`), but not between `judge` and `get`. If you
give an option in both places, the one at the end wins.

```
--api-key TEXT         API key (env: LAYERLENS_API_KEY)
--api-key-file PATH    Read the API key from this file, or from standard input for -
--host TEXT            API host
--port INTEGER         API port
--format [table|json]  Output format (default: table)
--verbose, -v          Enable debug output
--quiet, -q            Don't show the startup banner
--help                 Show help and exit
```

`layerlens --version` prints the version and exits. With `--format json`, stdout
holds only the JSON (`[]` for an empty list) and messages go to stderr, except for
`login` (interactive) and `completion` (prints shell code).

### Signing in

A command uses the API key from `--api-key`, `--api-key-file` or `LAYERLENS_API_KEY` when
there is one, and otherwise the session `layerlens login` stored in `~/.layerlens/credentials`.
A session renews itself through the LayerLens API when its token expires, so it lasts until
its refresh token does. Once LayerLens ends it, commands say
``Your `layerlens login` session has ended. Run `layerlens login` to sign in again.``
A renewal that fails for another reason (LayerLens unreachable, or busy) says why, and the next
command tries again.

---

## trace

Manage traces.

### `trace list`

List traces with optional filtering and pagination.

```bash
layerlens trace list [OPTIONS]
```

| Option | Type | Description |
| --- | --- | --- |
| `--page` | int | Page number |
| `--page-size` | int | Results per page |
| `--source` | text | Filter by source |
| `--status` | text | Filter by status |
| `--tag` | text | Only traces with this tag. Repeat it for traces with every one; an empty tag is a usage error |
| `--sort-by` | text | Sort field |
| `--sort-order` | asc/desc | Sort order |

The table shows each trace's ID, creation time, file name, evaluation count, agent and tags;
`-` when the API names no agent or the trace has no tags. `--format json` gives every field.

```bash
# The traces of the environment runs labelled with this commit
layerlens trace list --tag environment --tag support-desk --tag "$GIT_SHA"
```

### `trace get`

Get a single trace by ID.

```bash
layerlens trace get <ID>
```

### `trace search`

Search traces by query string.

```bash
layerlens trace search <QUERY> [OPTIONS]
```

| Option | Type | Description |
| --- | --- | --- |
| `--page` | int | Page number |
| `--page-size` | int | Results per page |
| `--source` | text | Filter by source |
| `--status` | text | Filter by status |
| `--tag` | text | Only traces with this tag; repeatable, as on `trace list` |
| `--sort-by` | text | Sort field |
| `--sort-order` | asc/desc | Sort order |

### `trace export`

Export a trace as JSON.

```bash
layerlens trace export <ID> [OPTIONS]
```

| Option | Type | Description |
| --- | --- | --- |
| `--output`, `-o` | path | Output file (default: stdout) |

### `trace delete`

Delete a trace by ID.

```bash
layerlens trace delete <ID> [OPTIONS]
```

| Option | Type | Description |
| --- | --- | --- |
| `--yes`, `-y` | flag | Skip confirmation prompt |

---

## judge

Manage judges.

### `judge list`

```bash
layerlens judge list [OPTIONS]
```

| Option | Type | Description |
| --- | --- | --- |
| `--page` | int | Page number |
| `--page-size` | int | Results per page |

### `judge get`

```bash
layerlens judge get <ID>
```

### `judge create`

Create a new judge.

```bash
layerlens judge create [OPTIONS]
```

| Option | Type | Required | Description |
| --- | --- | --- | --- |
| `--name` | text | yes | Judge name |
| `--goal` | text | yes | Evaluation goal description |
| `--model-id` | text | no | Model ID for the judge |

### `judge test`

Test a judge by running it against a trace. Creates a trace evaluation.

```bash
layerlens judge test [OPTIONS]
```

| Option | Type | Required | Description |
| --- | --- | --- | --- |
| `--judge-id` | text | yes | Judge ID to test |
| `--trace-id` | text | yes | Trace ID to evaluate |

---

## evaluate

Manage evaluations.

### `evaluate list`

```bash
layerlens evaluate list [OPTIONS]
```

| Option | Type | Description |
| --- | --- | --- |
| `--page` | int | Page number |
| `--page-size` | int | Results per page |
| `--status` | text | Filter: pending, in-progress, success, failure |
| `--sort-by` | submitted_at/accuracy/average_duration | Sort field |
| `--order` | asc/desc | Sort order |

### `evaluate get`

```bash
layerlens evaluate get <ID>
```

A table shows Submitted At and Finished At as UTC dates (`-` before the evaluation finishes) and
Average Duration, the average time the model took on a prompt, with its unit (`1.49 s`), as
`evaluate list` and `evaluate run` do. `--format json` gives the API's numbers: Unix seconds, and
nanoseconds for `average_duration`.

### `evaluate run`

Run a new evaluation. Accepts model/benchmark by ID, key, or name.

```bash
layerlens evaluate run [OPTIONS]
```

| Option | Type | Required | Description |
| --- | --- | --- | --- |
| `--model` | text | yes | Model ID, key, or name |
| `--benchmark` | text | yes | Benchmark ID, key, or name |
| `--wait` | flag | no | Wait for evaluation to complete |

`--wait` keeps polling through a 429, a 5xx or a lost connection, as
`wait_for_completion` does.

---

## evaluations

Run a dataset through your own code and compare two runs, locally: nothing is sent to
the API.

### `evaluations run`

```bash
layerlens evaluations run --dataset-id ID --dataset-file FILE --target MODULE:ATTR [OPTIONS]
```

| Option | Type | Required | Description |
| --- | --- | --- | --- |
| `--dataset-id` | text | yes | Name of the dataset in the run |
| `--dataset-file` | path | yes | JSON list of `{input, expected_output}` items |
| `--target` | text | yes | The callable under evaluation, `module:attr` |
| `--scorer` | text | no | A scorer, `name=module:attr` (repeatable). Default: `exact` equality |
| `--out` | path | no | Write the run to this JSON file (default: stdout) |

### `evaluations compare`

Compare a candidate run with a baseline run, both saved by `evaluations run --out`.
The comparison goes to stdout as JSON.

```bash
layerlens evaluations compare BASELINE CANDIDATE [--score-tolerance 0.02]
```

| Option | Type | Description |
| --- | --- | --- |
| `--score-tolerance` | float | How far a scorer's mean may drop before it counts as a regression (default: `0.02`) |

| Exit code | Meaning |
| --- | --- |
| `0` | No regression. |
| `1` | The candidate regressed: a scorer's mean dropped by more than `--score-tolerance`, or the pass rate dropped by more than `0.02`. |
| `2` | The runs could not be compared, and stdout is empty. A file that is not valid JSON or not an evaluation run gets one `Error:` line on stderr naming the file and the field, such as `Error: candidate run.json is not an evaluation run: dataset_version: Field required`. A run must hold `items` (`[]` for an empty dataset) and an `aggregate` with `mean_scores` and `pass_rate`, as `evaluations run --out` writes it, and no `NaN` or `Infinity`. A missing file is a usage error. |

In CI, fail the build on `1` and treat `2` as a broken step: never read `2` as "no
regression". CI shells such as GitHub Actions' `bash -e` stop at the first command that
fails, so catch the exit code with `||` before `case` reads it:

```bash
status=0
layerlens evaluations compare baseline.json candidate.json > comparison.json || status=$?
case "$status" in
  0) echo "no regression" ;;
  1) echo "the candidate regressed"; exit 1 ;;
  *) echo "could not compare the runs"; exit 2 ;;
esac
```

---

## integration

Manage integrations.

### `integration list`

```bash
layerlens integration list [OPTIONS]
```

| Option | Type | Description |
| --- | --- | --- |
| `--page` | int | Page number |
| `--page-size` | int | Results per page |

### `integration test`

Test an integration by ID: the API tries the integration's stored credentials against its vendor
(a Langfuse import source today).

```bash
layerlens integration test <ID>
```

Prints `Integration <ID>: OK` and exits `0` when they work. When the vendor refuses them (the API
answers `422`), it exits `1` with `Error: the stored credentials of integration <ID> were refused:`
and the vendor's reason. When the test could not be run, it exits `1` with `Error: integration <ID>
could not be tested:` and the API's reason: the vendor could not be reached (`502`), or LayerLens
refused the call (`401`: only an organization's owner or admin, or a key one of them created, can
test integrations). Your LayerLens key itself was checked first.

---

## scorer

Manage scorers.

### `scorer list`

```bash
layerlens scorer list [OPTIONS]
```

| Option | Type | Description |
| --- | --- | --- |
| `--page` | int | Page number |
| `--page-size` | int | Results per page |

### `scorer get`

```bash
layerlens scorer get <ID>
```

### `scorer create`

```bash
layerlens scorer create [OPTIONS]
```

| Option | Type | Required | Description |
| --- | --- | --- | --- |
| `--name` | text | yes | Name (3–64 characters) |
| `--description` | text | yes | Description (10–500 characters) |
| `--model-id` | text | yes | Model ID for scoring |
| `--prompt` | text | yes | Scoring prompt |
| `--dry-run` | flag | no | Preview without executing |

### `scorer delete`

```bash
layerlens scorer delete <ID> [OPTIONS]
```

| Option | Type | Description |
| --- | --- | --- |
| `--yes`, `-y` | flag | Skip confirmation prompt |
| `--dry-run` | flag | Preview without executing |

---

## bulk

Bulk operations.

### `bulk eval`

Run evaluations in bulk. Supports two modes:

**Mode 1: JSONL file**

```bash
layerlens bulk eval --file jobs.jsonl [OPTIONS]
```

Each line in the JSONL file is a JSON object with `model` and `benchmark` fields:

```json
{"model": "openai/gpt-4o", "benchmark": "arc-agi-2"}
{"model": "anthropic/claude-3-opus", "benchmark": "arc-agi-2"}
```

**Mode 2: Single model + benchmark**

```bash
layerlens bulk eval --model openai/gpt-4o --benchmark arc-agi-2 --wait [OPTIONS]
```

**Mode 3: Judge + trace IDs**

```bash
layerlens bulk eval --judge-id <JUDGE_ID> --traces trace_ids.txt [OPTIONS]
```

The traces file contains one trace ID per line.

| Option | Type | Description |
| --- | --- | --- |
| `--file` | path | JSONL file with evaluation jobs |
| `--model` | text | Model ID/name (use with --benchmark) |
| `--benchmark` | text | Benchmark ID/name (use with --model) |
| `--judge-id` | text | Judge ID (use with --traces) |
| `--traces` | path | File with trace IDs (one per line) |
| `--dry-run` | flag | Preview without executing |
| `--wait` | flag | Wait for all evaluations to complete |

---

## ci

CI/CD pipeline helpers.

### `ci report`

Summarise what a CI step names, for its job page: benchmark evaluations, trace evaluations and
environment runs, given by id, in one Markdown report (one JSON document with `--format json`).
It reports only what it is given, never the project's latest unless asked with `--latest`.

```bash
layerlens ci report [--evaluation-id ID]... [--trace-evaluation-id ID]... [--env-run ENV:RUN_ID]... [OPTIONS]
```

| Option | Type | Description |
| --- | --- | --- |
| `--evaluation-id` | text | A benchmark evaluation, as `evaluate run` printed it. Repeatable |
| `--trace-evaluation-id` | text | A trace evaluation, as `judge test` printed it. Repeatable |
| `--env-run` | `ENV:RUN_ID` | An environment run: the environment's name or id, a colon, the run's id. Repeatable |
| `--latest` | int | Also report the project's N most recent benchmark evaluations, whoever started them (1-500) |
| `--output`, `-o` | path | Append the Markdown report to this file, so `$GITHUB_STEP_SUMMARY` keeps what earlier steps wrote; with `--format json`, write the JSON report to it, replacing the file (default: stdout) |
| `--dry-run` | flag | Say what would be reported, without fetching it |

Name at least one evaluation or run, or pass `--latest`: otherwise it is a usage error (exit `2`),
and so is an empty id, which is what a variable holds when the step that should have set it
failed. Each is reported once, however many times it is named.
If one of them cannot be read (an unknown id, a refused request, a trace evaluation that
succeeded but whose result is missing), it exits `1` with an `Error:` line naming it, and
writes no report. An environment run needs an API key with **Automation** access, as
[`layerlens env`](env.md) does; one Automation key can read all three kinds.

It reports results without judging them: it exits `0` whatever they are. Gate the build with the
commands that ran them (`env run exec --min-pass-rate`, for example), and run `ci report` even
when they fail. GitHub Actions runs `run:` with `bash -e`, so save the exit status, write the id,
then exit with it:

```yaml
- name: Evaluate
  run: |
    status=0
    layerlens --format json evaluate run --model gpt-4o --benchmark mmlu --wait > evaluation.json || status=$?
    echo "EVAL_ID=$(jq -r '.id // empty' evaluation.json)" >> "$GITHUB_ENV"
    exit "$status"
- name: Summarise
  if: always() && env.EVAL_ID != ''
  run: layerlens ci report --evaluation-id "$EVAL_ID" -o "$GITHUB_STEP_SUMMARY"
```

An environment run gets a section like `env run exec --markdown` writes: its counts, then every
task that did not pass. A benchmark evaluation that has not succeeded shows no accuracy or failed
prompts, which until then cover only the prompts graded so far, and a trace evaluation still
running shows no verdict.

With `--format json`, the report is:

```json
{
  "generated_at": "2026-11-04T18:00:00Z",
  "evaluations": [{"id": "…", "status": "success", "accuracy": 76.67, "...": "…"}],
  "trace_evaluations": [{"id": "…", "status": "success", "...": "…", "result": {"passed": true, "score": 0.9, "...": "…"}}],
  "environment_runs": [{"environment": "support-desk", "run_id": "…", "passed": 9, "total": 10, "...": "…"}]
}
```

A benchmark evaluation has the fields `evaluate get --format json` prints. A trace evaluation
has its `id`, `trace_id`, `judge_id` and `status`, and its `result`, the object `judge result`
prints once it has succeeded (`null` until then). An environment run has the fields
`env run results --format json` prints, and the `environment` as it was given.

---

## replay

Replay traces locally, with overrides.

### `replay run`

Replays one trace through your own function and prints the result: the replayed trace's id, its
`status` (`completed`, `failed` or `timeout`), the difference from the original, and the
overrides applied.

```bash
layerlens replay run --trace-id 6aee8ab0f515e69a5d2cc4e9 --replay-fn my_agent.replay:replay --model-override gpt-4o-mini
layerlens trace export 6aee8ab0f515e69a5d2cc4e9 -o trace.json
layerlens replay run --trace-id 6aee8ab0f515e69a5d2cc4e9 --trace-file trace.json --replay-fn my_agent.replay:replay
```

| Option | Required | Description |
| --- | --- | --- |
| `--trace-id` | yes | The trace to replay. Without `--trace-file`, the stored trace with this id is read from LayerLens |
| `--trace-file` | no | A trace as `trace export` writes it, replayed without reading LayerLens. The result names the file's trace; a file of another trace than `--trace-id` prints a warning naming both |
| `--replay-fn` | no | `module:function` called with the trace and the request (overrides applied), returning the replayed trace. Without it, the trace is returned unchanged |
| `--model-override` | no | The model to replay with |
| `--input-override`, `--prompt-override` | no | `KEY=VALUE`, repeatable |

`--replay-fn` is loaded from your own code: a function from the standard library or another
module the CLI refuses (`os:system`, `subprocess:run`, ...) is not loaded. An id that names no
stored trace exits `2`; a replay that does not complete (the function raised, or ran out of time)
prints its result, says why, and exits `1`.

---

## env

Create and mint environments, run your own agent or a model against their tasks, and
gate a build on the result. It needs an API key with Automation access, except `env list`,
`env tools` and `env reset`, which a Standard key runs too. Every command and option is in
[`layerlens env`](env.md).

```bash
layerlens env create --type zendesk --name support-desk --description-file ci/environment.md --if-not-exists --wait
layerlens env run exec support-desk --tasks-file ci/tasks.jsonl --agent-name support-agent --min-pass-rate 0.9 -- python ci/agent.py
```

---

## completion

Print shell completion setup instructions.

```bash
layerlens completion <SHELL>
```

Where `SHELL` is one of: `bash`, `zsh`, `fish`, `powershell`.
