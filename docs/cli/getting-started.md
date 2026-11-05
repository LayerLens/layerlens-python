# CLI — Getting Started

The LayerLens CLI provides terminal access to all platform features: traces, judges, evaluations, integrations, scorers, bulk operations, and CI/CD helpers.

## Installation

Install the SDK with the `cli` extra:

```bash
uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[cli]"
```

If you already have `layerlens` installed, add the CLI extra:

```bash
uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[cli]"
```

For local development from a cloned repo:

```bash
pip install -e ".[cli]"
```

Verify the installation:

```bash
layerlens --version
```

## Configuration

### API key

The CLI requires a LayerLens API key. Set it as an environment variable (recommended):

```bash
export LAYERLENS_API_KEY="your-api-key"
```

Or pass it per-command:

```bash
layerlens --api-key "your-api-key" trace list
```

### Custom host

By default the CLI talks to `api.layerlens.ai`. Override with:

```bash
layerlens --host my-instance.example.com trace list
layerlens --host my-instance.example.com --port 8443 trace list
```

## Global options

Every command accepts these options, either right after `layerlens` or at the end
of the command: `layerlens --format json trace list` and
`layerlens trace list --format json` are the same. (Between `trace` and `list` they
are not accepted.) If you give an option in both places, the one at the end wins.

| Option             | Description                                |
| ------------------ | ------------------------------------------ |
| `--api-key`        | API key (or set `LAYERLENS_API_KEY`)       |
| `--api-key-file`   | Read the API key from a file (`-`: stdin)  |
| `--host`           | API host                                   |
| `--port`           | API port                                   |
| `--format`         | Output format: `table` (default) or `json` |
| `--verbose` / `-v` | Enable debug output                        |
| `--quiet` / `-q`   | Don't show the startup banner              |

`layerlens --version` prints the version and exits.

## Output formats

The default output is a human-readable table:

```bash
layerlens trace list
```

```
ID                                   Created              Filename         Evaluations
───────────────────────────────────────────────────────────────────────────────────────
a1b2c3d4-...                         2026-03-15 14:30     traces.jsonl     3
e5f6a7b8-...                         2026-03-14 09:12     batch_02.json    1
```

Switch to JSON for scripting:

```bash
layerlens --format json trace list
```

```json
[
  {
    "id": "a1b2c3d4-...",
    "created_at": "2026-03-15T14:30:00Z",
    "filename": "traces.jsonl",
    ...
  }
]
```

With `--format json`, stdout holds only the JSON (an empty result is `[]`), so
you can pipe it to `jq`. Progress and confirmation messages, such as
`Trace evaluation created: …`, go to stderr. A command that returns no data, such
as `trace delete`, leaves stdout empty. The exceptions are `login`, which prompts
for your email and password, and `completion`, which prints shell code.

## Shell completions

The CLI supports tab-completion for commands, options, and resource IDs.

```bash
# Print setup instructions for your shell
layerlens completion bash
layerlens completion zsh
layerlens completion fish
layerlens completion powershell
```

Follow the printed instructions to enable completions. After setup, you can tab-complete trace IDs, judge IDs, model names, and more.

## First commands

### List your traces

```bash
layerlens trace list
```

### Run an evaluation

```bash
layerlens evaluate run --model openai/gpt-4o --benchmark arc-agi-2 --wait
```

### Create a judge

```bash
layerlens judge create --name "Response Quality" --goal "Rate accuracy and completeness" --model-id <MODEL_ID>
```

### Check integrations

```bash
layerlens integration list
```

### Generate a CI report

Summarise the evaluations and runs a CI step named; `-o` appends, so `$GITHUB_STEP_SUMMARY` works:

```bash
layerlens ci report --evaluation-id <EVALUATION_ID> -o summary.md
```

## Next steps

- [Command Reference](commands.md) — all commands and their options
- [Examples](examples.md) — 15 common workflows as copy-paste shell sessions
