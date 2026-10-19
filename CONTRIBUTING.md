# Contributing to stratix-python

Thanks for your interest in contributing. The fastest path to a merged PR is to open an issue first so we can align on direction before code.

## Before you start

- Browse [open issues](https://github.com/LayerLens/stratix-python/issues), especially anything tagged `good first issue`.
- For non-trivial changes, [open an issue](https://github.com/LayerLens/stratix-python/issues/new) describing the problem and your proposed approach. We'll respond within a few business days.
- For questions and design discussion, join us in [Discord](https://discord.gg/layerlens).

## Repo layout

- `src/layerlens/` is the SDK source (clients, resources, CLI).
- `tests/` is the test suite (unit, integration, sample E2E).
- `samples/` holds runnable code samples organized by topic: `core`, `cicd`, `cli`, `mcp`, `integrations`, `industry`, `modalities`, `claude-code`, `cowork`, `copilotkit`, `openclaw`, `data`.
- `docs/` is the source for the [GitBook docs site](https://layerlens.gitbook.io/stratix-python-sdk).
- `scripts/` holds developer scripts (`bootstrap`, `test`, `lint`, `format`, `test_coverage`, `test_cov_core`).
- `pyproject.toml` is the Python project config and tool settings.
- `uv.lock` pins every dependency of the dev and CI environment. See [Dependencies and the lock](#dependencies-and-the-lock).
- `.husky/` holds Git hooks that run on commit (lint-staged formats and lints staged Python files).

## Local setup

The project uses [uv](https://docs.astral.sh/uv/) to manage Python and dependencies. [Install uv](https://docs.astral.sh/uv/getting-started/installation/) (0.12.23 or newer), then run the bootstrap script:

```bash
git clone https://github.com/LayerLens/stratix-python.git
cd stratix-python
./scripts/bootstrap        # uv sync --locked --all-extras: the Python in .python-version, then exactly uv.lock
source .venv/bin/activate
```

To work on another Python version, give it its own environment (`.venv*` is git-ignored):

```bash
UV_PROJECT_ENVIRONMENT=.venv-3.12 uv run --python 3.12 --all-extras pytest
```

## Dev loop

```bash
./scripts/test           # run the test suite (arguments go to pytest)
./scripts/lint           # ruff, pyright and mypy on src/, then an import check (--all: tests/ too)
./scripts/format         # format and auto-fix
./scripts/test_cov_core  # the core coverage gate that CI runs
```

Anything else runs through `uv run`, for example `uv run pyright --verifytypes layerlens --ignoreexternal`.

A pre-commit hook runs `./scripts/format` and `./scripts/lint` against staged Python files automatically.

## Required CI checks

Every PR runs these workflows. They must pass before review:

- [`run-tests.yaml`](https://github.com/LayerLens/stratix-python/actions/workflows/run-tests.yaml) is the full test suite.
- [`invariants.yaml`](https://github.com/LayerLens/stratix-python/actions/workflows/invariants.yaml) — **Invariant Gates**: the privacy / observability / schema contract guards (`pytest -m invariant`). See [Instrumentation invariants](#instrumentation-invariants--guards).
- [`check-format.yaml`](https://github.com/LayerLens/stratix-python/actions/workflows/check-format.yaml) checks formatting.
- [`check-lint.yaml`](https://github.com/LayerLens/stratix-python/actions/workflows/check-lint.yaml) runs the linter.
- [`check-lock.yaml`](https://github.com/LayerLens/stratix-python/actions/workflows/check-lock.yaml) checks that `uv.lock` is up to date with `pyproject.toml`.

Run them locally before pushing.

## Dependencies and the lock

`uv.lock` pins every package of the dev and CI environment: the runtime dependencies, every extra in `[project.optional-dependencies]`, and the `dev` and `mutation` dependency groups. It is one universal lock, resolved for CPython 3.9–3.12 on Linux (x86_64, aarch64) and macOS (`[tool.uv] environments`). The lock bounds only our own environment. Customers still install the ranges declared in `[project]`, so never add an upper bound there just to make CI green.

**CI installs exactly the lock and never resolves.** Every job that needs the dev environment (tests, coverage, invariants, lint, format, bite-check, mutation) sets it up with `.github/actions/setup-locked-env`, which:

1. installs the uv version in `[tool.uv] required-version` and a uv-managed CPython;
2. runs `uv sync --locked --all-extras`, which installs the lock and fails if it is stale instead of re-locking;
3. runs `uv sync --check` to prove the environment matches the lock exactly, and logs the installed versions.

Build backends are resolved in isolated build environments, outside the lock, so `[tool.uv] build-constraint-dependencies` pins the ones CI uses (hatchling for this package, setuptools for the locked sdist-only packages); `uv.lock` records those pins too. An upstream release can't change what CI installs: a new version reaches CI only through a PR that changes `uv.lock`. The **Check Lock** workflow (`uv lock --check`) fails when `pyproject.toml` changed without re-locking.

**Changing a dependency.** Edit `pyproject.toml` (or use `uv add` / `uv remove`), run `uv lock`, and commit `uv.lock` with the change. `uv lock` keeps every existing pin it can and resolves only what your change needs.

**Refreshing the lock on purpose.** PR CI no longer finds upstream drift for you. Two weekly, advisory canaries do, and neither blocks a PR:

- **Adapter Drift Canary** runs the adapter matrix rows against the latest framework releases.
- **Lock Drift Canary** re-locks to the latest releases of everything (`uv lock --upgrade`, never committed) and runs the full suite on every CI Python version.

Both run every row as `continue-on-error`, so the run itself stays green. A red row instead opens a tracking issue labelled `drift-canary`, one per canary, listing the red rows, their versions and the run link. If that issue is already open, the canary comments on it. A green run makes no GitHub call and never closes the issue: close it yourself once the canary is green. To assign new issues to people, set the repository variable `DRIFT_CANARY_ASSIGNEES` to comma-separated GitHub logins. To test the alert, run either workflow by hand with `force_red_row` set to a row name (a `tests/matrix/frameworks.toml` row, or `re-lock` / `python-3.12` for the Lock Drift Canary). That row reports red without running.

A red canary means a customer installing the latest release may hit the failure, so triage it and fix real breaks test-first. To move the lock forward, open a dedicated PR:

```bash
uv lock --upgrade-package anthropic   # one package, plus whatever it needs
uv lock --upgrade                     # everything
```

Then run the suite on every CI Python version and fix what breaks in the same PR:

```bash
failed=""
for v in 3.9 3.10 3.11 3.12; do
  UV_PROJECT_ENVIRONMENT=.venv-$v uv run --python "$v" --all-extras pytest || failed="$failed $v"
done
[ -z "$failed" ] || { echo "FAILED on:$failed"; false; }
```

To upgrade only as far as a known date, add `--exclude-newer 2026-10-05T10:09:00Z` to the `uv lock --upgrade…` command. The cutoff is recorded in `uv.lock` (and Check Lock fails while it is), so run a plain `uv lock` afterwards: it drops the cutoff and keeps every pin.

**One lock holds every extra, so check what an upgrade picked.** Where extras disagree about a shared dependency, `uv lock --upgrade` trades versions between them, and it can choose a much older release of one package to get a newer release of another. Without the floors in the `dev` dependency group it picks semantic-kernel 1.8.0, dspy 2.6 and crewai 1.6, and the dspy and crewai suites then skip. When you move the lock forward, compare the versions that moved with what `pip install layerlens[<extra>]` gets today, and raise those floors if needed (`tests/test_dev_lock_floors.py` fails when crewai or dspy drop below what their suites need).

Some current releases can't be in this lock at all, because another extra caps them: crewai and litellm cap openai below 3, crewai caps pydantic below 2.13 and mcp at 1.28. The `openai`, `openai_agents`, `mcp` and `core` rows in `tests/matrix/frameworks.toml` pin those releases in their own venvs, so CI still runs them.

A few rules keep the lock trustworthy:

- Never hand-edit `uv.lock`.
- If `./scripts/bootstrap` or a CI job fails with a stale-lock error, run `uv lock` and commit `uv.lock`.
- A new sdist-only dependency whose build backend isn't in `[tool.uv] build-constraint-dependencies` needs that backend pinned there.
- To test a new Python version or platform in CI, extend the `run-tests.yaml` matrix and `[tool.uv] environments` in the same PR, then re-lock.
- Bump `[tool.uv] required-version` only on purpose: CI installs exactly that uv, and a newer uv can write `uv.lock` differently.
- The adapter matrix is separate: `tests/matrix/frameworks.toml` pins framework versions for isolated per-framework venvs, not this lock.

## Pull request guidelines

- One logical change per PR. Smaller PRs merge faster.
- Reference the issue your PR addresses in the description.
- Include a runnable sample under `samples/` when adding a new SDK capability.
- Update `docs/` when changing public API surface.
- Add or update tests under `tests/` when changing behavior.
- Make sure all CI checks are green before requesting review.

## Instrumentation invariants & guards

The SDK is a **trace-capture product**, so a leak of customer content, a silently-dropped failure, or a missing cost is a correctness bug — not a nicety. We learned (LAY-3620 post-mortem) that *finding* leaks isn't enough: bugs of the same class kept reappearing because nothing **enumerated the whole population and checked each member**. So the contracts below are enforced by **standing guards** — the required `Invariant Gates` check (`uv run pytest -m invariant`) plus autouse runtime checks in the full suite. The intent is that a human *can't* reintroduce the class: the build fails.

If you touch `src/layerlens/instrument/**` (an adapter, an `emit()` call-site, an event type, or the capture/redaction machinery), you own these. The PR template has the checklist.

| # | Invariant | Guard (where it bites) |
|---|-----------|------------------------|
| 1 | **No content leaves under `capture_content=False`** — prompts, messages, tool args/results, `error` free-text, code, state, queries, delegation targets, payment detail are stripped at the collector. | `redact_payload` backstop keyed off `_CONTENT_KEYS`; `test_redaction_backstop.py` + `test_no_content_sweep.py` (L7 SENTINEL sweep). |
| 2 | **Content fields are declared** — every content field on an event is in `_CONTENT_KEYS`; every content-bearing type has a strip list or is allowlisted content-free. | `test_content_keys_guard.py` (keys-must-match **and** reverse guard + emit-call-site scan). |
| 3 | **Layer toggles suppress** — every content-bearing type is in `_EVENT_TYPE_MAP` (or `_ALWAYS_ENABLED`), never fail-open, so `minimal()` / a disabled layer suppresses it. | `test_layer_suppression.py`. |
| 4 | **Failures stay observable when content is off** — `agent.error` carries a surviving category (`error_type`/`error_code`/`status`), not just free-text `error` ("redact without going blind"). | Runtime invariant in `_event_schema.validate_event` — runs over **every uploaded event in every adapter suite**. |
| 5 | **Priced calls carry cost** — a framework `cost.record` for a priced model has `cost_usd` (computed in `_emit`, each `_fire`, and langfuse). | `test_cost_usd_fire.py` + per-adapter cost tests. |
| 6 | **No secret reaches a trace** — keys/tokens/connection strings never land in an uploaded event; error strings are scrubbed at the collector chokepoint, independent of `capture_content`. | `scan_for_secrets` autouse guard; `test_secret_scrub.py`. |
| 7 | **New event types are registered** in `KNOWN_EVENT_TYPES`. | `_event_schema.validate_event` + the emitted-constant / emit-literal scans. |

### How to add safely

- **New event type** → register it in `KNOWN_EVENT_TYPES` (`tests/instrument/_event_schema.py`); map it in `_EVENT_TYPE_MAP` to the right L-layer (or `_ALWAYS_ENABLED`); if it carries content add a `_CONTENT_KEYS` entry, else allowlist it content-free in `test_content_keys_guard.py`.
- **New content field on an existing event** → add the field name to that event's `_CONTENT_KEYS` set.
- **New error emit** → set `error_type` (or `error_code`/`status`), not just `error`; pass exception strings that may carry a credential through `safe_error`.
- **New `cost.record`** → emit via `self._emit`/`self._fire`, or call `self._price_cost_record(payload)` before a raw `collector.emit`; set the adapter's `pricing_table` if its models aren't in the default `PRICING`.

### Discipline behind the guards

- **Test-first, and every test must BITE**: revert the fix; the test must fail. A test that passes with the fix reverted guards nothing — that's exactly how vacuous tests and a months-long no-op schema lock shipped before.
- **Prefer population-complete checks** (a runtime guard over every emitted event; a structural map cross-check) over example-based tests and over fragile static analysis. Invariant #4 is the model.
- **A guard you never trip is theater** — exercise it with a seeded positive case (a fake secret, a reverted fix) so green means something.

## Code of conduct

This project follows the [Code of Conduct](./CODE_OF_CONDUCT.md). By participating, you agree to abide by it.

## Reporting security issues

Do not file a public issue for security vulnerabilities. See [SECURITY.md](./SECURITY.md) for the private disclosure process.

## License

By contributing, you agree your contribution is licensed under the [Apache License 2.0](./LICENSE).
