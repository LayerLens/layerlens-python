# Migrating from Stratix

2.0.0 is the first release as LayerLens. The client, its errors, its environment variables and the CLI command were renamed. The old names keep working, with a warning, and will be removed in a future major release.

Code written for 1.11.0 runs on 2.0.0 with the old names. A few things do change, though, and some of them can fail a CI job or change what a script prints. Read [What changes when you upgrade](#what-changes-when-you-upgrade) before you upgrade a CI job or an application.

## What was renamed

| Before                                                       | Now                                                           |
| ------------------------------------------------------------ | ------------------------------------------------------------- |
| `from layerlens import Stratix`                              | `from layerlens import LayerLens`                             |
| `AsyncStratix`                                               | `AsyncLayerLens`                                              |
| `StratixError`                                               | `LayerLensError`                                              |
| `Atlas`, `AsyncAtlas`, `AtlasError` (older names)            | `LayerLens`, `AsyncLayerLens`, `LayerLensError`               |
| `LAYERLENS_STRATIX_API_KEY` (or `LAYERLENS_ATLAS_API_KEY`)   | `LAYERLENS_API_KEY`                                           |
| `LAYERLENS_STRATIX_BASE_URL` (or `LAYERLENS_ATLAS_BASE_URL`) | `LAYERLENS_BASE_URL`                                          |
| the `stratix` command                                        | the `layerlens` command                                       |
| `map_agui_to_stratix()`, returning `{"stratix_event": …}`    | `map_agui_to_layerlens()`, returning `{"layerlens_event": …}` |

The package is still `layerlens`, and `Client` and `AsyncClient` still work.

## How the old names behave

- **They are the same objects.** `Stratix is LayerLens`, so `isinstance(client, Stratix)` and `except StratixError:` around SDK calls behave exactly as before.
- **Each use raises a `DeprecationWarning`** that names the replacement and points at your line of code.
- **`from layerlens import *` no longer brings in the old names.** They are not in `__all__`, so star-imports stay free of warnings. If you star-import and then call `Stratix()`, import it explicitly (`from layerlens import Stratix`) or switch to `LayerLens`.
- **Environment variables:** an old variable is read only when the new one is unset, with a `DeprecationWarning`. When both are set, the new one wins: see [Old and new variables both set](#old-and-new-variables-both-set).
- **The `stratix` command** runs the same CLI. Its stdout is identical to `layerlens`. `stratix --version` prints `layerlens 2.0.0`.
- **CLI notices show only in a terminal.** When stderr is a terminal, the `stratix` command and each old variable print a notice there:

  ```
  warning: the 'stratix' command is deprecated; use 'layerlens' instead
  warning: LAYERLENS_STRATIX_API_KEY is deprecated and will be removed in a future major release; rename it to LAYERLENS_API_KEY
  ```

  When stderr is a file or a pipe, as in a CI job, the CLI prints nothing extra, as 1.11.0 did. So a step that runs `OUT=$(stratix --format json integration list 2>&1)` and parses `$OUT` keeps working. To see what to rename, run a command in a terminal.

## What changes when you upgrade

### The client checks the key on first use

1.11.0 read the key's organization when you built the client, so `Stratix()` raised `AuthenticationError` for a wrong key and `APIConnectionError` when LayerLens was unreachable. In 2.0.0, building the client makes no request. The first call that needs the organization reads it, and raises the same error. So a program that builds its client at import starts while LayerLens is down, and the traces it can't upload are counted as lost.

Code that relied on the constructor to check the key now gets past it, and fails at its first call instead. To fail fast on a wrong key or URL, read `client.organization_id` right after building the client. This works on 1.11.0 too, with `Stratix` in place of `LayerLens`:

```python
import sys

from layerlens import APIError, LayerLens

try:
    client = LayerLens()
    client.organization_id  # reads the organization: raises for a wrong key or URL
except APIError as err:
    sys.exit(f"LayerLens refused this key or URL: {err}")
```

A wrong key raises `AuthenticationError` (401) or `PermissionDeniedError` (a bare 403 `Forbidden`), a wrong URL `NotFoundError` or `APIConnectionError`, and an unreachable API `APIConnectionError`. All of them are `APIError`s. Without any key, `LayerLens()` still raises `LayerLensError` at once.

### `layerlens ci report` needs a selector

`ci report` summarised the project's 10 latest benchmark evaluations, whoever started them. It now reports what you name: `--evaluation-id`, `--trace-evaluation-id`, `--env-run ENV:RUN_ID` (each repeatable), or `--latest N` for the project's N latest benchmark evaluations. Without any of them it exits `2` with a usage error and writes nothing. So a 1.11.0 step such as `stratix ci report -o "$GITHUB_STEP_SUMMARY"` now fails the job.

Name the evaluation the job ran:

```bash
layerlens --format json evaluate run --model gpt-4o --benchmark mmlu --wait > evaluation.json
layerlens ci report --evaluation-id "$(jq -r .id evaluation.json)" -o "$GITHUB_STEP_SUMMARY"
```

Or keep the old report with `--latest 10`. `--limit N` still works, as `--latest N`. Also:

- `-o FILE` now appends the Markdown report, so it no longer erases what earlier steps wrote to `$GITHUB_STEP_SUMMARY`. A JSON report still replaces the file.
- `--format json` no longer has `total_evaluations` and `summary`. Count the `evaluations` list yourself.

See [`ci report`](cli/commands.md#ci-report).

### `Result.score` can be `None`

A prompt the scorer could not grade has `score=None`, and `Result.score_error` says why. 1.11.0 raised `APIResponseValidationError` on any results page that held one. Such prompts don't count toward the evaluation's accuracy. Code that compares or adds up scores must skip them:

```python
scored = [r for r in results if r.score is not None]
passed = sum(1 for r in scored if r.score > 0.5)
```

Treat `None` as "not scored", never as `0`.

### `estimate_cost()` returns the estimate

`client.trace_evaluations.estimate_cost()` returned `None` for every call in 1.11.0. It now returns a `CostEstimateResponse`, and `None` only when the API gives no answer. Check `estimate.cost_available` before you use the cost: when the API can't price the run, `cost_available` is `False` and `estimated_cost` is `None`. An unpriced estimate is falsy, so a 1.11.0 guard such as `if estimate:` still skips it.

```python
estimate = client.trace_evaluations.estimate_cost(trace_ids=trace_ids, judge_id=judge.id)
if estimate and estimate.cost_available:
    print(f"Estimated cost: ${estimate.estimated_cost:.4f}")
```

### Old and new variables both set

1.11.0's SDK read only `LAYERLENS_STRATIX_API_KEY` and `LAYERLENS_STRATIX_BASE_URL`. 2.0.0 reads `LAYERLENS_API_KEY` and `LAYERLENS_BASE_URL` first. If a job sets both, with different values, 2.0.0 uses the new one. This happens to a CI job that also set `LAYERLENS_API_KEY` for the 1.11.0 CLI, which read it as a login token.

When the values differ, the SDK warns once per process, with a `UserWarning`:

```
LAYERLENS_STRATIX_API_KEY is set but ignored: LAYERLENS_API_KEY is set and wins. Unset one of them.
```

The same goes for `LAYERLENS_STRATIX_BASE_URL` and the `LAYERLENS_ATLAS_*` variables. The CLI prints the same sentence on stderr when stderr is a terminal. Keep one variable of each pair, with the value you mean.

### Evaluation spaces are removed

`client.evaluation_spaces`, the `space` CLI command group and the `EvaluationSpace…` models are gone. The API routes behind them were already removed, so 1.11.0's calls failed with `NotFoundError`. Remove the code that uses them.

### CLI tables changed; JSON did not

A script that parses a table can break. `--format json` keeps every key 1.11.0 printed (except `ci report`'s, above), so parse JSON instead.

- `trace list` and `trace search` end with two new columns, Agent and Tags. `awk '{print $NF}'` now gives the tags, not the evaluation count.
- `evaluate get`, `list` and `run` show dates as `2026-11-07 00:37:51 UTC` (they were Unix seconds), the average duration with its unit, such as `1.49 s` (it was nanoseconds), and the status as `success` (it was `EvaluationStatus.SUCCESS`).

### Other CLI changes

- `LAYERLENS_API_KEY` is now the CLI's API key variable, and the CLI sends it as an API key. 1.11.0 sent it as a login token, which the API rejected, so `LAYERLENS_API_KEY=… stratix trace list` failed unless `LAYERLENS_STRATIX_API_KEY` was also set.
- `layerlens whoami` checks an API key against the API and prints the organization and project it belongs to.
- `bulk eval` exits `1` when a job fails. It exited `0`.

The [CHANGELOG](../CHANGELOG.md) lists every change, including the bugs 2.0.0 fixes.

## Finding what to update

Python only shows a `DeprecationWarning` for code run directly as a script or under a test runner such as pytest. To see every warning in an application, run it with:

```bash
python -W default::DeprecationWarning your_app.py
```

Then:

1. **Code:** replace the old names with the new ones from the table above, and check [What changes when you upgrade](#what-changes-when-you-upgrade).
2. **Configuration:** rename the environment variables wherever they are set: shell profiles, `.env` files, CI secrets, container and deployment configs. Keep one variable of each old and new pair.
3. **Scripts and CI jobs:** replace the `stratix` command with `layerlens`, give `ci report` a selector, and parse `--format json` rather than tables.
