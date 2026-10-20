# Changelog

All notable changes to the LayerLens Python SDK (formerly the Stratix Python SDK) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Latest version:** [2.0.0](https://github.com/LayerLens/stratix-python/releases/tag/v2.0.0) — 2026-10-05

## [Unreleased]

Things we're actively working on. Want to help? Check the [issues](https://github.com/LayerLens/stratix-python/issues) or [discussions](https://github.com/LayerLens/stratix-python/discussions).

### Added

### Changed

- **The client is now `LayerLens`.** `Stratix` → `LayerLens`, `AsyncStratix` →
  `AsyncLayerLens`, `StratixError` → `LayerLensError`, and the CLI command is
  `layerlens`. The client is configured by `LAYERLENS_API_KEY` and
  `LAYERLENS_BASE_URL`. **Existing code keeps working:** every old name,
  variable and the `stratix` command still work (see `Deprecated` for the one
  star-import caveat). The README, docs and every sample use the new names, and
  [docs/migrating-from-stratix.md](docs/migrating-from-stratix.md) maps each one.

  | Before | Now |
  | --- | --- |
  | `Stratix`, `AsyncStratix`, `StratixError` | `LayerLens`, `AsyncLayerLens`, `LayerLensError` |
  | `LAYERLENS_STRATIX_API_KEY` | `LAYERLENS_API_KEY` |
  | `LAYERLENS_STRATIX_BASE_URL` | `LAYERLENS_BASE_URL` |
  | `stratix` (CLI) | `layerlens` |
  | `map_agui_to_stratix()` → `{"stratix_event": …}` | `map_agui_to_layerlens()` → `{"layerlens_event": …}` |

- `layerlens whoami` checks an API key against the API and prints the
  organization and project it belongs to. Previously it printed "Authenticated"
  without checking the key at all, and said "Not logged in" when only
  `LAYERLENS_STRATIX_API_KEY` was set.

- **crewai: a crew or agent run from inside your own `Flow` is now part of that Flow's trace.** It used to be uploaded as separate traces (two on crewai 1.14.6, three on 1.15) whose closing events were lost, so they flushed only at `disconnect()`. The crew and the agent now appear as nested spans under the flow, and the trace uploads when the flow finishes or fails. crewai reports a failed flow from 1.15 on; on earlier versions a failed flow emits no end event, so its trace (now including any crew it ran) uploads at `disconnect()`

- **`layerlens[marvin]` caps `genai-prices` below 0.1.** pydantic-ai 1.9-1.94 (the `[marvin]` extra caps pydantic-ai below 1.95) raises a `TypeError` extracting usage once genai-prices 0.1 is installed, swallows it, and reports zero tokens for every run. pydantic-ai 1.107.7 applies the same cap itself; it will be removed once marvin supports pydantic-ai >= 1.95

- **strands: the adapter traces only while it is connected**, as the other framework adapters do. An agent built with `Agent(hooks=[adapter])` used to be traced even if `adapter.connect()` was never called. It is now traced from `connect()` until `disconnect()`. Attaching the adapter while it isn't connected logs a warning (once per adapter)

- **`layerlens[pydantic-ai]` and `layerlens[all-frameworks]` now install pydantic-ai 2.x.** Both extras capped pydantic-ai below 2.0, although the adapter works on 2.x. They now resolve 2.17 or later on Python 3.10+. Python 3.9 stays on 0.8.1, the last release that supports it, and `[marvin]` still keeps pydantic-ai below 1.95. If you pin pydantic-ai 2.0-2.16 yourself, also pin `genai-prices<0.1` or upgrade: with genai-prices 0.1 or later, those releases report zero tokens for responses that include a reasoning-token count, as OpenAI chat completions do (an upstream bug, fixed in 2.17).

### Fixed

- **`LAYERLENS_API_KEY` was rejected by every CLI command.** The CLI sent it as a
  login token (`Authorization: Bearer`) to the login-only router, which returns
  401 for an API key, so `LAYERLENS_API_KEY=… layerlens trace list` failed unless
  `LAYERLENS_STRATIX_API_KEY` was also set. An API key is now always sent as
  `x-api-key`. Tokens from `layerlens login` are still sent as Bearer tokens.
- `PublicClient` and `AsyncPublicClient` now honour `LAYERLENS_ATLAS_API_KEY`, as
  the main clients always have. Every client resolves its configuration the same way.
- The CLI reference, workflow examples, README and `samples/cli/08_spaces.sh` still
  documented the `space` command, which 2.0.0 removed. They no longer do.
- **CLI: `--format`, `--api-key`, `--host`, `--port`, `-v` and `-q` failed after the
  command.** They were options of `layerlens` itself, so the form every `--help`
  example used, `layerlens judge result abc123 --format json`, failed with
  "No such option: --format". Every command now accepts them after its name too, and
  a value given there wins over one given before the command. `layerlens -v <command>`
  printed the version instead of running the command in verbose mode; `-v` alone
  still prints the version.
- **CLI: `--format json` printed text before the JSON.** `judge test`, `judge create`,
  `scorer create`, `integration test`, `evaluate run --wait` and `bulk eval` wrote
  progress and confirmation lines to stdout, and a list with no results printed
  "No judges found." (or similar), so piping to `jq` failed. With `--format json`,
  stdout now holds only the JSON (`[]` for an empty list) and those lines go to stderr,
  as do the delete confirmation prompts. `bulk eval --file` and
  `bulk eval --judge-id --traces` print the evaluations they created, and `whoami` and
  `synthetic templates` print JSON too. The table format is unchanged.
- **CLI: a list command reported "No judges found." when it could not read the API's
  response at all** (empty, not an object, or the wrong shape). It now prints an error
  and exits 1, so neither a person nor a script mistakes a failed call for an empty
  list.
- **CLI: `bulk eval` exited 0 when some of its jobs failed.** `bulk eval --file` and
  `bulk eval --judge-id --traces` now exit 1 when any job is skipped, fails, or (with
  `--wait`) can no longer be fetched, after printing the ones that succeeded and how
  many failed. `bulk eval --model … --benchmark … --wait` printed `None` and exited 0
  when its evaluation disappeared; it now says so and exits 1, as `evaluate run --wait`
  does.
- **CLI: `integration test` exited 0 when the check failed.** It now exits 1, so a
  script or CI step sees the failure.
- **CLI: `--port` without `--host` was silently ignored**, and requests went to the
  default host. It is now a usage error.
- **CLI: statuses were shown as `EvaluationStatus.SUCCESS`** in tables (every Python
  version) and in progress lines (Python 3.11+). They are shown as `success`.
- Shell completion of IDs ignored `--api-key`, `--host` and `--port` typed on the
  command line. It uses them now.
- **Install instructions installed the wrong package.** `layerlens` on public PyPI is
  an unrelated project, so the bare `pip install layerlens` in the CI quality-gate
  sample and across the docs and samples installed it. The `--index-url
  https://sdk.layerlens.ai/package` form failed outright, because that index hosts
  only `layerlens` and pip couldn't find its dependencies. And pip with
  `--extra-index-url` merges the two indexes and installs whichever `layerlens` has
  the higher version, so a higher-versioned release on PyPI would replace this SDK.
  Every install command, in the docs, the samples (the CI quality-gate sample and the
  copilotkit requirements files included) and the SDK's own "install the extra"
  messages, is now `uv pip install --extra-index-url https://sdk.layerlens.ai/package …`:
  uv takes `layerlens` only from the LayerLens index and its dependencies from PyPI.
  The README and the installation guide also give pip users a safe two-step form
  (`pip install --no-deps --index-url …`, then the dependencies). Extras are in
  double quotes (`"layerlens[cli]"`) so the command works in zsh, which rejects an
  unquoted `[...]`, and in Windows cmd.exe, which passes single quotes through.
  Samples no longer name extras that don't exist (`haystack`, `langfuse`,
  `llamaindex`). The installation guide no longer lists `requests`, which the SDK
  doesn't depend on, and the README's "build the docs locally" step, which named a
  `docs` extra and an `mkdocs` setup that don't exist, is gone.
- The VS Code extension README's install steps never built the `.vsix` they then
  installed. They now package it with `npx @vscode/vsce package`.
- The CI quality-gate sample pins its actions to commits, and this repository's
  workflows pin their third-party actions: a tag can be moved to other code, and these
  jobs hold `LAYERLENS_API_KEY` or AWS credentials. Dependabot keeps the repository's
  pins current.

- **crewai >= 1.14.7: one `crew.kickoff()` was uploaded as two traces.** From 1.14.7 crewai runs each agent execution inside an internal `AgentExecutor` Flow. The adapter opened a new trace for it, and its end event closed the crew's trace early, so the crew's inputs landed in one trace and its model call and `cost.record` in another. Everything that starts inside a run is now a nested span placed by crewai's event lineage, and only the event that opened a run ends it. `pip install layerlens[crewai]` resolves crewai 1.15 today, so this affected every new install

- **crewai: concurrent kickoffs through one adapter lost their completion.** crewai stamps `CrewKickoffCompletedEvent` with no parent (only `started_event_id` links it to its kickoff), so while a second kickoff was running the adapter could not tell which run it closed and dropped it. Each trace was then flushed only at `disconnect()`, without the crew's output and `cost.record`. Reproduced on crewai 1.14.6 and 1.15.27

- **crewai: model calls carry their token usage and cost.** crewai 1.x reports a call's usage on the event rather than in the response, so every model call reached the trace with no tokens and no cost; only the crew's total survived, without a price. Each model call now carries its prompt / completion / total tokens and a priced `cost.record`, both naming the calling agent (`agent_role`) so they can be attributed to it. The crew's total becomes a `cost.record` only when its calls reported no usage, so the same tokens aren't counted twice. A failed model call's `agent.error` names its agent too

- **crewai >= 1.15: a failed Flow uploads its trace, with the error, when it fails.** The adapter did not handle `FlowFailedEvent`, so a failed flow's trace stayed open until `disconnect()` (in a process that never disconnects, it was never uploaded), and the executor flow of each failed agent attempt was left without an end

- **pydantic-ai: no `cost.record` without token counts.** When pydantic-ai reports no tokens for a run (a provider that omits `usage`, or pydantic-ai 1.9-1.94 with genai-prices >= 0.1, which loses usage inside pydantic-ai) the adapter still emitted a `cost.record` carrying only the request count: a cost event with nothing to price. It is now emitted only when token counts are present; `agent.output` still reports `model_requests`

- **strands: `disconnect()` stops tracing.** An agent built with `Agent(hooks=[adapter])` kept being traced after `disconnect()` on every strands-agents version. From strands-agents 1.43.0 the same happened with `connect(target=agent)`, and reconnecting registered every hook a second time, so each event fired twice. `disconnect()` now removes the adapter's hooks from every agent it was attached to, either way. If a hook can't be removed, the adapter logs a warning and that hook no longer starts a trace. To trace an agent again after `disconnect()`, attach the adapter to it again. Attaching the adapter twice to the same agent now registers its hooks once

- **strands: a run in progress at `disconnect()` is uploaded.** Before, a run in progress when hooks were removed (on strands-agents 1.42 with `connect(target=agent)`) was dropped. If that run was in a long-lived asyncio task, the task uploaded nothing again, even after the adapter was reconnected. Now `disconnect()` stops new runs at once, and a run already in progress finishes and uploads before the adapter's hooks leave its agent

- **Haystack: model calls through a chat generator had no model, token counts or cost.** Chat generators such as `OpenAIChatGenerator` (on haystack-ai 3, the only OpenAI generator) report the model, token usage and finish reason on each reply, not in the separate `meta` list the adapter read, so their `model.invoke` events had none of the three and no `cost.record` was emitted. The adapter now reads them from the reply, on haystack-ai 2.x and 3.x, so these calls carry their model, token counts and a priced `cost.record`. The haystack-ai 2.x text generator (`OpenAIGenerator`) was not affected

### Deprecated

- `Stratix`, `AsyncStratix`, `StratixError` and the older `Atlas`, `AsyncAtlas`,
  `AtlasError`. Each resolves to the *same object* as its replacement, so
  `isinstance` checks and `except StratixError:` behave exactly as before, and
  each use emits a `DeprecationWarning` naming the replacement. They are no
  longer listed in `layerlens.__all__`, so `from layerlens import *` no longer
  brings them in; code that star-imports and then uses `Stratix` must import it
  explicitly.
- `LAYERLENS_STRATIX_API_KEY` / `LAYERLENS_STRATIX_BASE_URL` and the older
  `LAYERLENS_ATLAS_*` variables. They are still read, with a warning, when the
  `LAYERLENS_*` variable is not set. When both are set, the new one wins.
- The `stratix` command. It runs the same CLI and prints a one-line notice to
  stderr; stdout is unchanged.
- `map_agui_to_stratix()`, which still returns its old `{"stratix_event", "category"}` shape.

All of these will be removed in a future major release.

### Removed

## [2.0.0] - 2026-10-05

Removes the evaluation spaces client surface. **This is the only breaking change:
if your code does not reference `client.evaluation_spaces`, the `space` CLI command
or the evaluation space models, upgrading from 1.11.0 requires no changes.**

### Removed

- **Breaking: evaluation spaces.** The feature has been retired from Stratix, and
  the API routes behind it are gone, so the client surface for it has been removed
  rather than left to return errors:
  - `client.evaluation_spaces` and `async_client.evaluation_spaces`
  - the `stratix space` / `layerlens space` CLI command group
  - the `EvaluationSpace`, `EvaluationSpaceFilters`, `EvaluationSpaceModelFilter`,
    `EvaluationSpaceDatasetFilter` and `EvaluationSpacesResponse` models

  Existing evaluation space documents are retained in Stratix and are not deleted;
  they are simply no longer reachable through the API or this SDK. Code calling any
  of the above should be removed. Nothing else in the client is affected.

## [1.11.0] - 2026-08-10

Data-correctness fixes on the evaluations and results read paths, and on the cost
figures the instrumentation layer reports. **If you read `Result.duration` or
`Result.metrics`, or you have recorded `cost_usd` from an instrumented run, please
read the `Changed` notes below — values you stored may need recomputing.** This
release also ships the OpenInference OTLP envelope decoder.

### Fixed

- **A real, billed LLM call could be recorded as costing exactly $0.00.** The shared pricing formula prices prompt / cached / cache-write / completion tokens and never reads `total_tokens`, so a usage carrying only a total summed four zeroes and returned `0.0`. Because `0.0 is not None`, every downstream "did we get a price?" guard passed and the zero shipped as a *derived* cost. This was not specific to one adapter — the defect lived in the formula, and reproduced through **openinference** (a totals-only `llm.token_count.total`, which OpenInference explicitly permits), **langchain** (`token_usage: {total_tokens: N}` and `usage_metadata: {total_tokens: N}`, both genuine callback shapes) and **langgraph** (inherited from langchain). An unpriceable token shape now yields no `cost_usd` at all. If you have aggregated spend from instrumented runs, totals-only calls were counted as free and your figures understate the bill

- **A malformed span no longer aborts the rest of the batch.** `_record_from_dict` was not exception-wrapped while its sibling was, so one bad dict raised out of `ingest_spans`, abandoning every remaining span and stranding the already-ingested ones in an unflushed collector

- **A span's real error message is no longer replaced by a generic backstop.** A pre-extracted record's `status_message` was dropped, degrading a genuine upstream error to `"span status ERROR"`

- **OTLP span and trace ids encoded as base64 are decoded correctly.** proto3-JSON specifies base64 for a `bytes` field, and `_coerce_id` handled only plain hex. A trace arriving in that encoding was split in two

- **An OTLP `kvlistValue` is now converted to a real mapping** instead of shipping the raw OTLP wrapper structure into the payload, which is also what the Go bridge does

- **`environment.config` no longer loses a trace's service identity.** OTLP carries `service.name` and friends on the Resource block, one level above the spans, which the SDK could not previously see

- **Two behaviours converged with the Go bridge**, which had made the same span parse differently depending on arrival path: whitespace around the span kind is trimmed (`" LLM "` typed as `agent.interaction` in Python and `model.invoke` in Go), and a nameless AGENT/CHAIN `agent_id` is lower-cased. `agent_id` is a graph node id, so the same span rendered as a differently-named node

- **`Evaluation` could not read quality scores the API reports as "not computed", making both the private and public evaluations endpoints unusable.** `readability_score`, `toxicity_score` and `ethics_score` were typed `float` with a default of `0.0`. A pydantic default covers a **missing** key and does nothing for an explicit `null`, so once the API began sending `readability_score: null` for evaluations where the metric was never computed, `evaluations.get_many()`, `evaluations.get_by_id()`, their public-client equivalents, all four async twins, and `Evaluation.wait_for_completion()` all raised — and on the list endpoints a single such evaluation discarded the entire page of up to 500. The three fields are now `float | None`. The SDK reads a number, an explicit `null`, and a missing key. No client action beyond upgrading

- **`results.get_all()` could return a short list with no error.** Every parse failure inside `results.get_by_id()` was swallowed by `except Exception: return None`, and the pagination walk treated that `None` as *end of pages*. A single unparseable row on page 3 of 8 therefore returned pages 1–2 as though they were the complete result set, with nothing to distinguish it from a genuinely complete one. Parse failures now raise `APIResponseValidationError` carrying the offending page and the field paths that failed, and the walk refuses to return a partial list

- **A single malformed row no longer discards a whole page of evaluations.** `evaluations.get_many()` and its public equivalent built rows in a list comprehension outside any error handling, so one bad row raised a raw `pydantic_core.ValidationError` — not catchable as a `layerlens` exception, and with no indication of which of up to 500 rows was at fault. Rows are now validated individually and failures raise `APIResponseValidationError` naming the row index and field

- **`"results": null` and `"evaluations": null` are read as empty lists.** The API emits `null` rather than `[]` for a page with no matching rows (a nil Go slice with no `omitempty`). This previously failed validation on `/results` and raised a bare `TypeError` on `/evaluations`

- A 2xx response whose body is not valid JSON, or is not the documented JSON object, now raises `APIResponseValidationError` with the body attached instead of returning `None`

### Changed

- **`cost.record` may now arrive with no `cost_usd` where it previously carried `0.0`.** A reader that treats a missing `cost_usd` as zero will silently reproduce the bug this release fixes. Two new markers say *why* a cost is absent or approximate, so an honestly-withheld figure stays distinguishable from a dropped price: `cost_status="unpriceable_token_shape"` means the model resolves to a rate but the payload carries no dimension the formula can price — the cost is unknowable, not zero, and this marker never accompanies a `cost_usd`; `cost_status="partial_token_shape"` means a cost *was* computed but the provider reported more billed tokens than any rate was applied to, so the figure **understates** the bill, and it always accompanies a `cost_usd` plus an `unpriced_tokens` count. The canonical `partial_token_shape` case is Gemini, which reports `thoughtsTokenCount` outside `candidatesTokenCount` while the total includes it. Neither marker changes any money — attributing the residual to an unobserved rate would be a guess billed to a customer

- **A vendor-reported charge is exempt from the under-report check.** Only our own arithmetic is audited: a figure carrying `cost_source` (langfuse's billing figure, OpenRouter's usage accounting) is a billed fact rather than an estimate, so a token gap against it says nothing about its accuracy. The `langfuse` adapter now declares `cost_source="langfuse"` — it is the one adapter whose zero can be honest, and without the marker the new invariant would have rejected truthful data

- **`Result.duration` values change by a factor of 10⁹. This is a correctness fix, not a rescaling you can ignore.** The API sends this field as an int64 **nanosecond** count (a Go `time.Duration`), and the SDK was reading the raw integer as **seconds**. A 2.5-second response was reported as `28935 days, 4:26:40` — roughly 79 years — with no exception raised. `duration` now converts correctly. Any duration you stored, logged, compared, or aggregated from an earlier version is wrong by 10⁹ and needs recomputing. Constructing a `Result` with a real `timedelta` in Python is unaffected

- **`Result.metrics` is now `Dict[str, float | ScorerResult | None] | None`, up from `Dict[str, Optional[float]]`.** The declared type only described built-in metrics. An evaluation run with **custom scorers** also carries one scorer-outcome object per scorer, keyed by scorer ID — `{"<scorerID>": {"score": 0.8, "status": "success"}}` — and a single `metrics` map can mix both forms. The old type rejected the object form, and because the failure was swallowed (see the `results.get_all()` entry above) **the effect was that `results.get()` returned `None` and `results.get_all()` returned an empty list for custom-scorer evaluations that in fact had thousands of rows** — silently, with no error to indicate it. If you have ever seen an unexpectedly empty result set from a custom-scorer evaluation, this was why. The new `ScorerResult` model (exported from `layerlens.models`) carries `score`, `status` and `error`; a scorer that failed reports `score=None`, meaning "did not run", not a score of zero. Code that indexes `result.metrics["toxicity"]` for built-in metrics is unaffected; code that iterates all values should narrow on `isinstance(metric, ScorerResult)`

- **`readability_score` / `toxicity_score` / `ethics_score` widen from `float` to `float | None`, which is a typing break for static analysis.** Running mypy or pyright, you will newly see errors on `eval.readability_score + 1` or `f"{eval.readability_score:.2f}"`, and at runtime code that previously received `0.0` for a missing key now receives `None`, so arithmetic raises `TypeError`. This is shipped as a minor rather than a major release because **it breaks no working code**: against the currently-deployed API these fields are unreadable, so every affected call already raises before a caller can touch the value. `None` means the metric was not computed — rendering it as `0.0` would report a perfect toxicity score for an evaluation that was never scored

- `results.get_by_id()` / `get_all()` and `evaluations.get_many()` now raise on a malformed response instead of returning `None`. If you branch on a `None` return to mean "the request failed", switch to catching `layerlens.APIResponseValidationError` (or `StratixError` for everything)

### Added

- **An OTLP envelope decoder for OpenInference.** The adapter already understood per-span OpenInference attributes, but there was no way to hand it a real OTLP export — the `resourceSpans` / `scopeSpans` wrapper had to be walked by the caller. Exported from `layerlens.instrument.adapters.frameworks`: `OpenInferenceOTLPBridge`, `otlp_json_to_span_records`, `otlp_json_to_resource_groups`, `otlp_protobuf_to_span_records`, `otlp_protobuf_to_resource_groups`. Both proto3-JSON and protobuf inputs are accepted. **Scope is decoding only** — there is deliberately no dedup and no `max_spans`, and nothing binds an OTLP port; receiving spans over the wire is the collector's job, not the SDK's

- **Resource attributes are lifted into `environment.config`**, at most once per Resource block per trace, via `environment_config_from_resource()` (exported alongside the decoder). The key set is **curated** (`service.name`, `service.namespace`, `deployment.environment`, `cloud.region` and similar) rather than copied wholesale: a Resource block can carry credentials, so a blanket copy would be an exfiltration path

- `ScorerResult` model, exported from `layerlens.models`
- A response-contract test suite (`tests/contract/`) that parses response bodies recorded from the API's own Go structs, then re-parses each one with every null-capable key forced to `null`. That second pass is what would have caught this class of break before a customer did; the corresponding generator and the written `/api/v1` response-compatibility rule live in the API repo

## [1.10.0] - 2026-08-06

### Added

- **Token usage on evaluations and per-prompt results.** `Evaluation` gains `total_input_tokens`, `total_output_tokens`, `avg_input_tokens_per_prompt`, and `avg_output_tokens_per_prompt`; `Result` gains `input_tokens` and `output_tokens`. Aggregates count the evaluated model's successful attempts only (no failed retries, judge/grader calls, or prompt-cache tokens) and are `None` — not `0` — for runs that predate token capture
- README: `Adapters`, `Requirements`, `Versioning and Compatibility`, and `Data Handling` sections. The adapters section documents `auto()` / `discover_installed()`, explicit per-framework wiring, and offline hash-chain verification via `layerlens.attestation`

### Changed

- README: corrected the reference-data claim to 172 models and 78 benchmarks, replaced the PyPI badges (they pointed at an unrelated `layerlens` project on PyPI, not the private index), and refreshed the architecture tree to the real package layout

## [1.9.0] - 2026-07-30

The instrumentation release: `layerlens.instrument` captures agent traces from the frameworks, LLM providers, and agent protocols you already use, and ships them to LayerLens for trace evaluation.

### Added

- **`layerlens.instrument` — agent instrumentation and tracing engine.** Public API: `auto()`, `trace()`, `span()`, `emit()`, `TraceCollector`, `CaptureConfig`, `BaseAdapter`, `AdapterInfo`, and `discover_installed()`. `auto()` detects the agent libraries installed in the environment and wires up the matching adapters; `trace()` / `span()` mark run and step boundaries by hand when you want explicit control
- **39 adapters** across three layers:
  - 8 LLM providers — OpenAI, Anthropic, Azure OpenAI, Bedrock (including Amazon Nova `invoke_model`), Google Vertex, LiteLLM, Ollama, OpenRouter
  - 25 frameworks — LangChain, LangGraph, LlamaIndex, CrewAI, AutoGen, Agno, Haystack, Semantic Kernel, OpenAI Agents, Google ADK, Bedrock Agents, Agentforce, Strands, smolagents, Pydantic AI, Microsoft Agent Framework, DSPy, Instructor, Marvin, Mirascope, OpenInference, browser-use, Langfuse, embedding, vector store
  - 6 protocols — MCP, A2A, AG-UI, A2UI, AP2, UCP
- **W3C trace context propagation** — `inject_headers()`, `extract_headers()`, `get_trace_context()`, `new_traceparent()`, and the `trace_context` context manager, so a trace survives a hop across services and protocol boundaries
- **Agent-graph contract** — adapters emit `agent_name` and handoff edges, so multi-agent runs reconstruct as a real DAG instead of a flat span list
- **Attestation** — every wire event carries a per-event hash for OTLP conformance and tamper evidence
- **Cost tracking** — a spend ledger and provider pricing tables attach `cost_usd` to captured runs, including costs the framework reports itself
- **Upload data-loss observability** — `set_upload_loss_callback()` and `get_upload_loss_stats()` surface dropped events instead of failing silently
- **`strict` flag on `traces.get()` / `traces.get_many()`** (sync + async, default `False`). When `True`, a 200 response with an empty or unparseable body raises `StratixError` instead of returning `None`, distinguishing contract drift from a genuine miss. A real 404 still raises `NotFoundError`
- CLI: `judge result` command; trace-evaluation IDs no longer get routed to `evaluate get`

### Changed

- **BREAKING (`capture_content=False` only):** `model.invoke.parameters` redaction is now deny-by-default. The collector previously stripped a fixed deny-list, so any parameter the SDK had not seen before passed through and could carry prompt or response content. It now keeps only a vetted allowlist of non-content metrics (sampling and limit parameters), recursing into nested containers like `generation_config` and `options` so safe sub-keys survive and content sub-keys are dropped. Impact is metadata loss on custom or provider-specific parameters, never a content leak. The default `capture_content=True` path is unchanged
- Every resource method now raises from the SDK exception taxonomy. `_request_cast` previously mapped only `httpx.HTTPStatusError`, letting raw transport and decode failures escape; timeouts now surface as `APITimeoutError`, transport errors as `APIConnectionError`, and response decode/validation failures as `APIResponseValidationError`
- Per-event byte cap on captured events, and upload filenames are sanitized fail-fast
- Dropped the `browser-use` extra — its `openai` pin conflicts with the SDK's. The browser-use adapter still works when you install the package yourself

### Fixed

- **Privacy — `capture_content=False` leaks closed across all adapters**. Follow-ups: Google ADK system prompts and CrewAI task descriptions, protocol-layer content gating and redaction, and arbitrary user-supplied trace metadata in the Langfuse adapter
- **SSRF guard on presigned uploads** — the upload target is validated before the request goes out
- Async provider clients are routed onto the async wrap path instead of the sync one
- Per-adapter concurrent-run isolation — parallel runs no longer bleed spans into each other
- Provider-only traces emit a real captured root span rather than a synthesized placeholder
- `agent.identity` is captured canonically at flush time
- LangGraph and LangChain event serialization
- Agentforce importer rewritten against the real Salesforce STDM, and `bedrock_agents` rewritten against the real `InvokeAgent` completion EventStream
- `autogen`, `crewai`, and `llamaindex` now honor a caller-bound collector instead of falling back to the global one
- A2A: protobuf `TaskStatus.state` enum now maps to the canonical status string
- Telemetry fidelity for LiteLLM streaming; adapter schema-lock re-arm, Vertex/Azure coverage, vector-store and provider linkage

## [1.8.0] - 2026-05-26

### Added

- `benchmark_key`, `model_key_1`, and `model_key_2` parameters on `comparisons.compare_models` (sync + async). Address the benchmark and the two models by their unique key (e.g., `aime2024`, `openai/gpt-4o`) instead of by UUID; the existing `*_id` parameters keep working. Exactly one of `*_id` or `*_key` must be provided per entity — passing both, or neither, raises `ValueError`. Unknown keys raise `ValueError` with the offending key in the message.

## [1.7.0] - 2026-05-20

### Added

- `extra_payload` parameter on `models.create_custom` and `models.update_custom` (sync + async). Optional JSON object merged into every outgoing chat-completions request body; customer values win on conflict with our hardcoded defaults. Lets customers add provider-specific fields (`top_p`, `max_completion_tokens`) or override values like `temperature` for providers that reject our defaults.

## [1.6.1] - 2026-05-15

### Added

- CLI authentication command (`layerlens auth`) (#72)
- `models.update_custom(model_id, *, api_url, api_key, max_tokens)` (sync + async) — repoint a custom model's mutable fields without recreating it (#169)
- `models.delete_custom(model_id)` (sync + async) — full teardown that disables the record, strips it from `Project.Models`, and releases the name for reuse (#169)
- 70+ production-ready SDK samples across 12 categories: core, industry, cowork, modalities, integrations, cicd, cli, openclaw, mcp, copilotkit, claude-code, data (#73)
- MCP server sample exposing LayerLens as tools
- CopilotKit sample with LangGraph CoAgents, React components, and hooks
- New trace samples (#144)

### Changed

- `models.add()` / `models.remove()` now operate on the full project model list (public + custom). The previous `type="public"` filter silently dropped custom-model IDs from `Project.Models` on every call (#169)
- Expanded SDK documentation and README (#139, #167)

### Fixed

- Trace evaluations bug (#74)
- CopilotKit evaluator graph now compiles with a checkpointer so `interrupt()` works over AG-UI. Includes a `RunIdPreservingAgent` workaround for the upstream `ag-ui-langgraph` runId-overwrite bug ([ag-ui-protocol/ag-ui#1582](https://github.com/ag-ui-protocol/ag-ui/issues/1582)) (#92)

## [1.6.0] - 2026-03-25

### Added

- Prompts exposed on the private client (#70)

## [1.5.0] - 2026-03-23

### Added

- Full-featured command-line interface via `layerlens` / `stratix`
- `client.scorers` resource with full CRUD: create, get, list, update, delete
- `client.evaluation_spaces` resource with get, list, create, update, delete
- `client.integrations` resource with get, list, create, update, delete, and test
- CLI getting started guide, command reference, and examples
- Scorers API reference documentation

### Changed

- Updated evaluations, models & benchmarks, and public client docs with new parameters

### Fixed

- `filter` by categories/languages/companies/regions/licenses now returns correct results

## [1.4.0] - 2026-03-17

### Added

- `unique` parameter on `evaluations.get_many()` and `public_evaluations.get_many()` that deduplicates results by model+dataset pair, keeping only the latest evaluation per pair

### Fixed

- Model comparison now passes `unique=True` when fetching evaluations, ensuring the correct (latest) evaluation is used for each model+benchmark pair instead of potentially picking up duplicates

## [1.3.3] - 2026-03-17

### Added

- Missing methods on `benchmarks` and `models` resources

### Fixed

- Inconsistent API naming across the SDK now follows a unified convention. Affected resources: comparisons, evaluations, judges, results, trace evaluations, traces, public benchmarks/evaluations/models (#61)
- `SUMMARY.md` structure and examples updated to match new naming

## [1.3.2] - 2026-03-13

### Added

- Documentation pages for GitBook: getting-started, troubleshooting, security

### Fixed

- `trace_evaluations.get_results()` no longer returns empty/None results. The API returns evaluation data (score, passed, reasoning, steps) directly, but the SDK was looking for a non-existent results array. `TraceEvaluationResultsResponse` now correctly maps to the API response shape and inherits from `TraceEvaluationResult`
- `TraceEvaluationStep` model now matches actual API fields (`tool`, `args`, `result`) instead of the incorrect (`step`, `reasoning`)

## [1.3.1] - 2026-03-13

### Added

- Automatic retry with exponential backoff for transient errors (HTTP 429, 500, 502, 503, 504) in both sync and async clients (up to 2 retries, respects `Retry-After` header, max 8s delay)
- Expanded documentation: updated README, examples for models/benchmarks, public API, and retrieving results

## [1.3.0] - 2026-03-13

### Changed

- Expanded model and benchmark result models with additional fields

### Fixed

- CI/CD publish workflows

## [1.2.0] - 2026-03-13

### Added

- `Stratix` / `AsyncStratix` clients (rebrand from Atlas)
- Judges resource with full CRUD
- Trace upload (JSON/JSONL up to 50 MB via presigned S3) and `trace_evaluations` resource
- Judge optimizations resource for tuning judge configurations
- `PublicClient` — a dedicated client for public endpoints (models, benchmarks, evaluations, comparisons), also accessible via `client.public`
- `get_by_key`, `add`, `remove`, `create_custom`, `create_smart` methods on Model & Benchmark resources
- `comparisons` resource for comparing evaluation results
- Apache 2.0 license

### Changed

- Expanded benchmark and model models with additional fields

### Deprecated

- `Atlas` client name — use `Stratix` instead (legacy `Atlas` aliases kept for backward compatibility)

### Fixed

- Evaluation status enum values

## [1.0.2] - 2026-03-13

### Changed

- Updated publish-to-AWS packaging job

## [1.0.1] - 2026-03-13

### Fixed

- Version bump

## [1.0.0] - 2026-03-13

### Added

- Initial release of the LayerLens evaluation SDK
- Sync and async clients for the LayerLens evaluation API
- `evaluations`, `results`, `models`, and `benchmarks` resources
- Typed exception hierarchy for API errors

[Unreleased]: https://github.com/LayerLens/stratix-python/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/LayerLens/stratix-python/compare/v1.11.0...v2.0.0
[1.9.0]: https://github.com/LayerLens/stratix-python/compare/v1.8.0...v1.9.0
[1.8.0]: https://github.com/LayerLens/stratix-python/compare/v1.7.0...v1.8.0
[1.7.0]: https://github.com/LayerLens/stratix-python/compare/v1.6.1...v1.7.0
[1.6.1]: https://github.com/LayerLens/stratix-python/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/LayerLens/stratix-python/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/LayerLens/stratix-python/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/LayerLens/stratix-python/compare/v1.3.3...v1.4.0
[1.3.3]: https://github.com/LayerLens/stratix-python/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/LayerLens/stratix-python/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/LayerLens/stratix-python/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/LayerLens/stratix-python/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/LayerLens/stratix-python/compare/v1.0.2...v1.2.0
[1.0.2]: https://github.com/LayerLens/stratix-python/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/LayerLens/stratix-python/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/LayerLens/stratix-python/releases/tag/v1.0.0
