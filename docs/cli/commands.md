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
│   └── report        Generate a CI summary report
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
| `--sort-by` | text | Sort field |
| `--sort-order` | asc/desc | Sort order |

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

Test an integration by ID.

```bash
layerlens integration test <ID>
```

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

Generate a markdown summary of recent evaluations, suitable for GitHub Actions job summaries.

```bash
layerlens ci report [OPTIONS]
```

| Option | Type | Description |
| --- | --- | --- |
| `--output`, `-o` | path | Output file (default: stdout) |
| `--limit` | int | Number of evaluations to include (default: 10) |
| `--dry-run` | flag | Preview without fetching data |

---

## env

Create and mint environments, run your own agent or a model against their tasks, and
gate a build on the result. It needs an Automation API key. Every command and option is in
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
