# Changelog

All notable changes to the LayerLens Python SDK (formerly the Stratix Python SDK) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Latest version:** [2.0.0](https://github.com/LayerLens/stratix-python/releases/tag/v2.0.0) — 2026-10-05

## [Unreleased]

Things we're actively working on. Want to help? Check the [issues](https://github.com/LayerLens/stratix-python/issues) or [discussions](https://github.com/LayerLens/stratix-python/discussions).

### Added

- **A job can wait for every trace it recorded: `flush(timeout)` and `LAYERLENS_EXIT_TIMEOUT`.**
  At exit the SDK waited at most 5 seconds for queued uploads, at a second or two each, so a
  CI job that asked 10 questions stored 6 traces and one that recorded 20 in a burst stored 2.
  `layerlens.instrument.flush(timeout=None)` waits until every client's queue and the upload in
  flight are done, or the timeout passes, and returns whether nothing is left to send;
  `LAYERLENS_EXIT_TIMEOUT` (seconds, read at exit) sets how long the exit waits. The default
  stays 5 seconds, so an interactive program or a service is not held at exit when LayerLens is
  slow. `layerlens env run exec` sets the variable for the agent to 30 seconds, or half
  `--attempt-timeout` when that is shorter, unless the step sets it, so the agent's trace is
  uploaded, and linked to its attempt, before the agent exits. The warning for traces left
  unsent says how long the exit waited and how to wait longer
- **Find environment runs, and compare two (LAY-4432).** `layerlens env run list ENV
  [--label] [--agent-name] [--kind] [--status]` lists an environment's runs newest first, and
  `client.environments.runs.list(...)` / `runs.iter(...)` read them in Python, with an
  Automation key. `layerlens env run compare ENV --baseline-label main RUN_ID` compares a run
  with the newest run on `main` of the same agent that succeeded (or two run ids): it prints the
  pass-rate change and every task that passed on the baseline and fails on the candidate,
  appends the same to `--markdown FILE`, and exits `1` on a regression and `2` when the runs
  could not be compared, never `1` for an error. `RunComparison.between(baseline, candidate)`
  is the same comparison in Python. Runs now carry `created_at`, `started_at` and
  `finished_at`. Needs the API's `GET /environments/{id}/runs`.
- **Environments from CI: `client.environments` and `layerlens env`.** With an API key that has
  **Automation** access (Manage organization → API keys), create an environment, keep
  task sets, and run graded evaluations of your own agent or of a model, then fail a build on the
  result. An agent builds each environment from a description of a company, as a composite of
  that company's app: its tools are named `<alias>__<tool>` (`zendesk__ZENDESK_LIST_ZENDESK_TICKETS`),
  and `create` takes no settings and no seed, as the API builds every world from the
  description alone since layerlens-app#2881 (LAY-4490). `layerlens env run exec ENV
  --tasks-file ci/tasks.jsonl --agent-name my-agent --min-pass-rate 0.9 -- python agent.py` runs your command once per task, each attempt on its own
  copy of the world with its own short-lived token and without your API key in its environment,
  grades every attempt, writes a JUnit report with `--junit FILE` and a Markdown one with
  `--markdown FILE`, and exits `10` below the pass rate (`11` when the run did not
  finish, `1` on an error, `2` on bad usage). In Python, `client.environments.external_run(...)` is a
  context manager over the same run, also on `AsyncLayerLens`; `runs.run_model` runs a catalog model
  or an OpenAI-compatible endpoint; `RunResult.to_junit()` and `to_markdown()` render results. Calls
  that create or start something send an `Idempotency-Key` and are sent again under it when their
  reply is lost, or when the API is too busy admitting jobs (for up to 30 seconds); when a start's
  outcome stays unknown (no reply, a server error, an interrupt), `run start`, `run model` and
  `tasks generate` print the key, and `--idempotency-key` sends it again to get that run or job
  back instead of starting another. A request body over 5 MiB, or over 5.75 MiB once escaped for
  the API's gateway, is refused before it is sent. A submit that times out is settled from the run
  instead of being reported lost. `run exec` exits `11`, saying why, when every attempt ended in an
  error (a broken command, or an agent that fails every task; timeouts are still gated);
  names the task when the API refuses an answer; cancels its run on Ctrl-C, SIGTERM or
  `--timeout`, as `run model` does (`run wait` only with `--cancel-on-timeout`; a run that finished
  as the timeout passed is gated as it ended); refuses
  `--api-key`, since every user on the machine can read a command line; and runs on Linux and
  macOS (WSL included), not on Windows, which does not end what an agent leaves running when it
  exits (drive the run yourself there, or use `external_run`). It keeps the key out of
  the agent's environment, but an agent running as the same user can still find it: run an agent
  you do not trust in a container (the docs show how in GitHub Actions). New errors:
  `EnvironmentBuildError` and `EnvironmentConflictError`. See
  [docs/api-reference/environments.md](docs/api-reference/environments.md) and
  [docs/cli/env.md](docs/cli/env.md)

- **`layerlens env run exec` links the agent's own trace to its attempt.** Each attempt's
  command gets `LAYERLENS_TRACE_ID_FILE` beside `LAYERLENS_ANSWER_FILE`; the trace id the
  command leaves there is submitted with the attempt, whether it answered, failed or timed
  out, so the run's results carry it as `agent_trace_id`, and the attempt's progress line
  names it. An agent traced with the LayerLens SDK needs no code: the SDK writes the id of the
  first trace it uploads there, unless the file already holds one. A file with more than one
  id, or an id over 256 bytes, is not linked and the progress line says why; the answer is
  graded all the same, where the API's refusal of the submit would have cancelled the run.
  `runs.submit`, `attempt.submit` and `attempt.submit_error` refuse a `trace_id` over 256
  bytes with `ValueError` before sending it. See [docs/cli/env.md](docs/cli/env.md)

- **Filter traces by tag: `client.traces.get_many(tags=[...])` and `layerlens trace list --tag`.**
  A trace matches when it carries every tag, so `layerlens trace list --tag environment --tag
  support-desk --tag "$GIT_SHA"` lists the traces of the environment runs labelled with that
  commit. `--tag` is repeatable, on `trace search` too, and each trace's `tags` are now read
  (`Trace.tags`). An empty tag, such as an unset variable, is refused (`ValueError`; a usage
  error in the CLI) rather than sent, since the API would ignore it and list every trace; one
  string passed as `tags` raises `TypeError`

- **A recorded error keeps the provider's reason code, as `error_code`.** Since openai 3.38 an
  error's text no longer holds the reason (`Error code: 429 - Rate limit reached`, without
  `rate_limit_exceeded`), so traces lost it. Every adapter, and `@trace`, now reads the reason off
  the exception, or the error that caused it when a framework wrapped it in its own: openai's
  `code` (also Azure OpenAI and LiteLLM), a relayed error body's `error.code` (groq,
  pydantic-ai), AWS's `Error.Code`, Google Gen AI's `status`, anthropic's `type`. Like
  `error_type`, it is kept under `capture_content=False`, where the error's text is not, so a
  trace recorded with the default capture config now says why a call failed. Only a short
  token is kept, never text. See
  [docs/api-reference/instrumentation.md](docs/api-reference/instrumentation.md#error-events)
  (LAY-4454)

- **The SDK tags the traces it records: `trace(..., tags=[...])`, `trace_context(..., tags=[...])`
  and `LAYERLENS_TRACE_TAGS`.** A trace is uploaded with the variable's tags, then those of the
  `trace`/`trace_context` blocks it started in, outermost first, then its own; a block's tags also
  reach the traces adapters open of their own inside it (OpenAI Agents, LlamaIndex,
  OpenInference). The variable tags every trace a process records with no code change, so CI can
  find one run's traces with `layerlens trace list --tag`, and grader rules and batch evaluations
  can target them, where searching by agent name missed traces a framework names. It is
  comma-separated, or a JSON array of strings for a tag with a comma. Each tag is trimmed (the
  API's filter trims what it is asked for, so an untrimmed tag could never be found) and
  secret-scrubbed like event text; blanks and repeats are dropped; a tag over 128 bytes or with
  a control or format character, and tags past 32, are dropped with a warning, never cut (the
  limits LayerLens's OpenTelemetry ingest applies).
  `tags` that is not a list of strings raises `TypeError` where it is given. Tags sit outside the
  attestation chain and are sent whatever the capture configuration. See
  [docs/instrumentation/tags.md](docs/instrumentation/tags.md)

- **`layerlens env run exec --label` tags the agent's own traces with the label.** It puts the
  label first in the command's `LAYERLENS_TRACE_TAGS`, before the tags already there (a trace
  keeps its first 32), so an agent traced with the LayerLens SDK tags every trace it records as
  LayerLens tags each attempt's trace: `layerlens trace list --tag "$GIT_SHA"` lists both, and `--tag environment` keeps
  LayerLens's alone. A label with a comma stays one tag. See [docs/cli/env.md](docs/cli/env.md)

- **`--api-key-file PATH` reads the CLI's API key from a file, or from standard input with `-`.**
  It works on every command, like `--api-key`, and keeps the key out of the command line and the
  environment. Giving both is a usage error, and so is `-` on `env delete` or `env tasks delete`
  without `--yes`, whose confirmation would read standard input too

- **Prices for OpenAI's current models.** The bundled price table now has OpenAI's Standard prices, as listed on 2026-10-16, for gpt-6-astra, gpt-6.1-sol, gpt-6-sol, gpt-6-luna, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, gpt-5.5-pro, gpt-5.4, gpt-5.4-mini, gpt-5.4-nano, gpt-5.4-pro, gpt-5.3-codex, gpt-5.2, gpt-5.2-pro, gpt-5.1, gpt-5, gpt-5-mini, gpt-5-nano, gpt-5-pro, gpt-5-search-api and chat-latest, and for the trusted-access gpt-5.6-cyber, gpt-5.5-cyber, gpt-rosalind-research and gpt-rosalind-discovery (gpt-5.6-sol's is a promotional price, which OpenAI offers at least through 2026-11-21). Their `cost.record` events had no `cost_usd` unless you priced them with `LAYERLENS_PRICING_TABLE`; they now include it, with cache reads and, from GPT-5.6 on, cache writes. Two OpenAI charges are not applied: a prompt over 272K input tokens costs twice the input and cached-input prices and 1.5 times the output price on gpt-5.4, gpt-5.5, their Pro models and the GPT-5.6 and GPT-6 models, and regional-processing endpoints add 10%, so the cost of such calls is understated. If you priced any of these models with `LAYERLENS_PRICING_TABLE`, your rows still take precedence over the bundled ones: add a `cached` price to them (see below) or remove them

- **A pricing-table row can carry a `cached` price.** A `LAYERLENS_PRICING_TABLE` or `PricingTable` row now accepts `"cached"`, the price of an input token read from the prompt cache, in USD per 1K tokens like `input` and `output`: `{"my-model": {"input": 0.002, "output": 0.008, "cached": 0.0005}}`. A row without it prices a cache read at half its input price (a tenth for Claude models, a quarter for Gemini models)

### Changed

- **`layerlens evaluate get`, `list` and `run` show dates and a duration with its unit.** A table
  printed Submitted At and Finished At as Unix seconds (`1794011871`) and Average Duration as
  nanoseconds (`1486055168`) with no unit. It now shows UTC dates (`-` before an evaluation
  finishes) and the average time a prompt took as `850 ms`, `1.49 s` or `2 min 5 s`.
  `--format json` still gives the API's numbers. The API reference said `average_duration` was in
  milliseconds; it is in nanoseconds

- **`layerlens trace list` and `trace search` show each trace's agent and tags.** After `--tag`,
  the table gave no hint why a row matched without `--format json`. It now ends with an Agent and
  a Tags column, after the columns it had. `Trace.agent` holds the agent the API names for a
  trace, which the model used to drop; it is `None` when the API names none

- **`layerlens ci report` reports what the CI step names, not the project's latest evaluations.**
  It summarised the 10 most recent benchmark evaluations in the project, whoever started them, so
  a job summary showed other people's evaluations, and never a trace evaluation or an environment
  run. It now reports `--evaluation-id`, `--trace-evaluation-id` and `--env-run ENV:RUN_ID` (each
  repeatable, each reported once) in one Markdown report, or one JSON document with `--format json`.
  The project's latest are reported only with `--latest N`; `--limit N` still works, as
  `--latest N`, with a deprecation warning. Without any of them, or with an empty id, it is a usage
  error (exit `2`), where it used to report the project's latest. If one of them cannot be read it
  exits `1`, naming it, and writes no report. `-o FILE` now appends the Markdown report, so
  `-o "$GITHUB_STEP_SUMMARY"` no longer erases what earlier steps wrote there (a JSON report still
  replaces the file). A benchmark evaluation that has not succeeded shows no accuracy or failed
  prompts, which until then cover only the prompts graded so far; one that has shows its accuracy as
  the percentage the API sends (`76.7%`), where it multiplied it by 100 again (`7666.70%`). The JSON
  report drops `total_evaluations` and `summary`, whose `passed` counted the evaluations that
  completed, whatever they scored. The API reference now says `Evaluation.accuracy` is a percentage
  (0 to 100), not 0.0 to 1.0. See [docs/cli/commands.md](docs/cli/commands.md#ci-report) (LAY-4431)

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

- **`Result.score` is now `Optional[float]`, and `Result` gains `score_error: Optional[str]`.** The API returns `score: null` with a `score_error` reason for a prompt the scorer could not grade (judge unreachable, scorer crash); such prompts are excluded from the evaluation's accuracy. Previously the SDK raised a `ValidationError` on every results page containing one. Treat `None` as "not scored", never as `0`.

### Fixed

- **`layerlens integration test` tells a vendor's refusal from a call LayerLens refused.** It
  read every 401 from the test as the integration's stored credentials refused by its vendor, so a
  key whose user may not test integrations (a member's) and a vendor that could not be reached were
  reported as bad credentials. With layerlens-app's companion change the API answers a vendor's
  refusal with 422 and an unreachable vendor with 502: a 422 prints `the stored credentials of
  integration ID were refused`, and a 401 or a 502 prints `integration ID could not be tested`,
  each with the API's reason. An older API's 401 prints its reason the same way

- **`layerlens env --help` says what a Standard key runs.** It said every command needs an
  Automation key, and so did the error printed when no key is set, also for `env list`. A key
  with Standard access runs `env list`, `env tools` and `env reset`; both now say so

- **`env run exec`, `model` and `wait` exit with the gate's code when a report cannot be
  written.** A `--junit` or `--markdown` file they could not write was an "unexpected error",
  exit `1`, so a run below `--min-pass-rate` (`10`) or one that did not finish (`11`) read as a
  broken pipeline. The failure is now printed as an error naming the file, the other report is
  still written, and the exit code is the gate's; a run that passes the gate exits `1`. `env run
  results` exits `1` naming the file

- **`env run compare` exits `1` for a regression even when `--markdown` cannot be written.** A
  summary file it could not append to (a missing directory, no permission) was an "unexpected
  error" and exited `2`, "the runs could not be compared", after the runs were compared and the
  regression found: a CI gate that reads `2` as a broken pipeline let the regression through. The
  verdict now decides: a regression exits `1` and stderr says the summary was not written; with no
  regression the command exits `2`, naming the file and saying nothing regressed, since the step
  did not do all it was asked

- **`Trace` holds the parts of a trace continued in another process.** Since layerlens-app#2888
  such a trace is stored as one trace per process; the traces list shows it once, through its
  first part, with `segment_count`, and the read of a part names every part (`segments`), its
  place (`part`) and their number (`parts`). `Trace` dropped all four, so the SDK could not reach
  a later part: the list hides it, and only `segments` names it. They are now optional fields,
  with `TraceSegment` (`id`, `segment_id`, `continued_from_span_id`, `start_time`,
  `event_count`, `attestation_status`); `None` for a trace that is not continued and from an
  older API. `layerlens trace get --format json` prints them

- **A stream returned by a `@trace` function that fails ends its trace with the error alone.**
  The trace recorded the stream's `agent.error`, then the function's `agent.output` with
  `status: "ok"`, as if the call had succeeded. It now ends with `agent.error` on the function's
  span, as when the function itself raises, and no `agent.output`; the failed model call keeps its
  own `agent.error`, with the chunks it had read. So does a stream that failed inside the function
  before it was returned

- **`@trace(AsyncLayerLens())` and `trace_context` with an async client upload their traces.**
  The upload worker, a thread, called the client's `traces.upload`, which on `AsyncLayerLens` is
  a coroutine: nobody awaited it, so nothing was sent, Python warned `coroutine … was never
  awaited`, and every trace was counted `rejected`. The worker now uploads an async client's
  traces through a synchronous client with its key, URL, timeout, retries and upload hosts, in its
  organization and project: the async client's connections belong to its event loop, which is
  closed by the time the upload runs when the program used `asyncio.run`. A client of another
  kind whose upload returns an awaitable is counted `upload_error`, as a trace that cannot be
  sent, not `rejected`

- **`layerlens integration test` reads the API's answer.** The API answers a passing test with
  `{"ok": true}`, which `integrations.test()` could not read: it returned `None`, and the CLI said
  "Failed to test integration" for an integration that worked. `TestIntegrationResponse.success`
  now reads `ok`. Credentials the vendor refuses come back as HTTP 401 with the vendor's reason,
  which the CLI printed as "Invalid or missing API key"; it now prints that the integration's
  stored credentials were refused, and why

- **`AsyncLayerLens` reads its organization without blocking the event loop.** Since the
  organization and project are read on first use, the async client read them with a synchronous
  HTTP call made while an async resource built its URL, which blocked the event loop and skipped
  the client's retries. Every async resource call now awaits that read through the client's own
  requests, once for concurrent first calls, before it builds a URL

- **`env run exec` and `env run submit` say when the agent's trace was not linked.** With
  layerlens-app#2888 the API links an agent's trace id only when it names a trace (in the
  organization by its id, or in the project by its SDK or OpenTelemetry trace id); for any other
  it grades the answer, links nothing, and says so in `trace_id_unlinked`. `SubmitResult` carries
  that sentence; `exec`'s progress line for the attempt reads `no trace linked: <sentence>`
  instead of naming the id, and `submit` prints it as a warning on stderr

- **A stream returned by a `@trace` function is recorded in that trace.** The trace was uploaded
  when the function returned, before its caller read the stream, so the call's `model.invoke` and
  `cost.record` were recorded nowhere. A trace (from `@trace` or `trace_context`) whose code opens a
  provider stream and hands it out now stays open until the stream ends, and uploads then with the
  call; the stream's events go to the trace and span the call was made in, wherever it is read. A
  stream closed early, dropped unread, or still open at exit lets the trace upload without the call
  (the exit queues it before it drains). A stream the garbage collector ends is released on a
  background thread, `layerlens-stream-release`, never inside the collection, and `flush()` and the
  exit wait for those releases before they wait for the uploads. The function's `agent.output` records the stream's reply,
  as `model.invoke` records it, once the caller has read it; it recorded the stream object as text
  (`<generator object …>`), and records no output for a stream that ended without a reply.
  Covers calls made with `stream=True` through the OpenAI, Azure OpenAI, OpenRouter, Anthropic,
  LiteLLM, Google Vertex and Ollama adapters, sync and async; not Anthropic's `messages.stream()`
  helper or Bedrock's streams, which are recorded as before

- **`layerlens --format json env create --wait` prints the build as it ended.** It printed the
  create reply, whose `job.status` read `pending`, after the build had finished. With `--wait` it
  now prints the environment and its build job as they are once the wait ends, under the same
  keys (`environment`, `job`, `reused`, `unused`): the served `environment_version` and
  `tool_count`, and the job's final `status` and `cost_usd`. Without `--wait` it prints the create
  reply as before

- **`layerlens evaluations run` marks `--dataset-file` required, as the docs do.** `--help` listed
  it as optional, and a run without it stopped with "remote dataset lookup is not yet
  implemented". Click now reports `Missing option '--dataset-file'` (exit `2`, as before)

- **An OpenAI Responses API stream records its token usage and cost.** `responses.create(...,
  stream=True)` was aggregated as a Chat Completions stream, reading usage, model and deltas that
  a Responses stream's events do not carry: its `model.invoke` held only the request's model,
  with no token counts, no response id and, with content capture, no reply, and there was no
  `cost.record`. It is now read from the stream's last event (`response.completed`,
  `.incomplete` or `.failed`), which holds the whole response, as a call without `stream` is
  read; a stream cut before that event keeps its deltas' text and records no usage (sync and
  async)

- **`benchmarks.add()` and `remove()` work in a project that holds a custom benchmark, and say
  when they did.** They read the project's benchmarks of both kinds and sent every id as the
  project's `datasets`, which lists public benchmarks only: the API answered 500 for the custom
  one. They now read and send public ids only (sync and async). They also read the API's answer
  as the models' PATCH does, so a PATCH that succeeded no longer returns `False`

- **Ctrl-C makes `layerlens env run exec` cancel the run once, and say so once.** Leaving the
  `external_run` block on the interrupt cancelled the run, then exec cancelled it again: two
  `POST …/cancel` per interrupt. When the answers in flight had been graded meanwhile, the run
  had finished, and stderr showed the SDK's raw `could not cancel run … Error code: 409 …`
  before exec's own line. A run that has finished is no longer a failed cancel: leaving the
  block says so in `finished_before_cancel` and logs nothing, and keeps the cancelled run in
  `cancelled_run`. exec reports that one cancel, and cancels again only when a second Ctrl-C
  cut it short

- **`layerlens replay run --trace-id` replays that trace, and a failed replay exits `1`.** Without
  `--trace-file`, the command never read the trace: it replayed an empty one and printed
  `"status": "completed"`, also for an id that named no trace, and a replay whose function raised
  printed `"status": "failed"` and exited `0`. It now reads the stored trace from LayerLens; an
  id that names none exits `2` saying so; a replay that does not complete prints its result,
  says why, and exits `1`. `--replay-fn` is vetted before anything is read. `replay` is now in
  [docs/cli/commands.md](docs/cli/commands.md#replay)

- **`layerlens scorer create` prints the scorer it created.** The API answers a create with the new
  scorer's id; `scorers.create()` read that as a whole scorer, failed for want of a name, and
  returned `None`, so the command printed `Failed to create scorer.` and exited `1` for a scorer
  that existed. `create()` (sync and async) now reads the scorer back by its id and returns it,
  and raises instead of returning `None` when the answer holds no new scorer. A request the API
  refuses now prints the API's sentence with its status, `Error: … (HTTP 400)`, as the `env`
  commands do, in every command, instead of the exception's text (`Error code: 400 - {...}`)

- **A `layerlens login` session renews itself instead of expiring after an hour.** The CLI
  refreshed its token straight with Cognito, with the client id alone; the app client has a
  secret, so Cognito answered `invalid_client_secret` every time and every command said
  `Session expired` an hour after login. It now renews through the LayerLens API's
  `POST /dgklmnr/refresh`, as the web app does, and stores the new token with its own expiry
  (24 hours, not the access token's hour). When LayerLens ends the session, commands say so and
  ask for `layerlens login`, instead of `no credentials found`; a renewal that fails for
  another reason says why

- **An instrumented program starts while LayerLens is down or its key is wrong.** `LayerLens()`
  read the organization over HTTP in its constructor, so a program written as the
  instrumentation docs show (`client = LayerLens()` at module scope, then `@trace(client)`)
  crashed at import with `APIConnectionError` when LayerLens was unreachable, or
  `AuthenticationError` for a wrong or rotated key. The client now reads the organization and
  project the first time a call needs them: a traced function runs, and the trace it cannot
  upload is counted as lost (`upload_error`); a call that needs the organization raises what
  the API said, as before, and a failed read is tried again next time. To check the key at
  start, read `client.organization_id`. `AsyncLayerLens` now raises the SDK's errors for that
  read (`APIConnectionError`, `AuthenticationError`, ...), as `LayerLens` does, instead of
  httpx's. The CLI still checks the key before every command

- **A trace continued in another process keeps both processes' events.** `get_trace_context()`
  handed to another process and continued there with `trace_context(client,
  from_context=...)` uploaded under the same `trace_id` with nothing telling the two uploads
  apart, and LayerLens kept the one uploaded last: the other process's events and tags were
  gone, and the survivor still read `verified`. Each collector now sends its own `segment_id`
  (the same for every upload of it), and a continued one sends `continued_from` with the span it
  continues. Needs the API that keeps one trace per `trace_id` and `segment_id`; an older API
  ignores the fields

- **A program no longer waits 5 seconds at exit for nothing.** At exit the SDK told each upload
  thread to stop with a marker on its queue, and dropped the marker when the queue was full (64
  traces): the thread sent the queued traces, then waited for more, and the exit waited out its
  whole 5-second timeout, once per client. Each thread now stops as soon as its queue is sent, all
  of them are told at once, and the exit waits at most 5 seconds in all. A trace queued after a
  shutdown still starts a new thread. A trace the exit could not wait for used to vanish without
  a word; it is now counted as lost (`exit_timeout`, also through the loss callback) and a warning
  says how many (LAY-4454)

- **`layerlens env run exec` submits nothing after Ctrl-C.** A worker could pass the check for an
  interrupt just before Ctrl-C and send its answer while the run was being cancelled. Whether an
  attempt may submit is now decided under the lock the interrupt takes, and the answers already
  on their way get up to 5 seconds to be graded before the run is cancelled, so the API sees them
  before the cancel (LAY-4454)

- **An answer over the API's 64 KiB limit is refused naming the answer.** `runs.submit`,
  `attempt.submit`, `attempt.submit_error` and `layerlens env run submit` sent it, and one over
  5 MiB was refused with how to start a run ("run fewer or shorter inline tasks..."). An answer or
  an error over 65,536 bytes, trimmed, is now refused before sending, as the API words it
  (`answer is 70000 bytes; the limit is 65536`), and a request too big to send says what to send
  less of for that request (LAY-4454)

- **The instrumentation docs describe the API that exists.** The reference and the guides showed
  `TraceRecorder`, `SpanData`, `span(name, kind=...)` yielding an object to set `.output` on, and
  `provider.connect_client(...)`, all removed long ago: copied, they raised `TypeError` or
  `AttributeError`, as did `samples/integrations/openai_instrumented.py`. They now show `span()`,
  `emit()`, `trace_context()`, error events and uploading as they work, and a test checks every
  instrumentation example in the docs and samples against the code (LAY-4454)

- **`layerlens evaluations compare` exits `2`, not `1`, when it cannot read a run.** A file that
  was not valid JSON, lacked a field or had one of the wrong type raised a Python traceback and
  exited `1`, the code for a regression, so a CI step could not tell "the candidate regressed" from
  "this step is broken". It now prints one `Error:` line naming the file and the field (`Error:
  candidate run.json is not an evaluation run: dataset_version: Field required`) and exits `2`, as
  it does on any other failure. A file that leaves out `items` or `aggregate`, which the run model
  would fill in as a run that scored nothing, or that holds `NaN` or `Infinity`, is refused the same
  way instead of compared. The exit codes are `0` no regression, `1` regression, `2` the runs
  could not be compared; see [docs/cli/commands.md](docs/cli/commands.md#evaluations-compare)
  (LAY-4429)

- **`wait_for_completion` keeps polling through a 429, a 5xx or a lost connection.**
  `client.evaluations.wait_for_completion` and `client.trace_evaluations.wait_for_completion`,
  sync and async, raised the first such error left once the client's two retries were spent,
  ending the wait while the evaluation went on running on the server. Parallel CI jobs sharing an
  API key, which the API throttles at about 5 requests a second, hit this. The waits now poll again
  half a second later, then after twice as long each time a poll fails again, up to
  `interval_seconds`, and never sooner than the reply's `Retry-After` (up to 5 minutes), until
  `timeout_seconds`; then the `TimeoutError` names the last error and carries it as its
  `__cause__`. Any other error is still raised at once. The waits now measure their timeout on the
  monotonic clock, so a change of the system clock no longer cuts one short or stretches it
  (LAY-4429)

- **Every wait sends each poll once, so it raises at its timeout and spares a throttled key.**
  The evaluation waits and the environments waits (`wait_until_ready`, `jobs.wait`,
  `runs.wait`) let the client retry each poll twice, up to 8 seconds apart, inside their own
  polling. A throttled poll cost three requests against the key that was being throttled, and a
  poll started just before the deadline could raise up to 16 seconds after it. The wait now does
  all the retrying, and honours a `Retry-After` longer than the 8 seconds the client stops at
  (LAY-4429)

- **An environments wait whose polls keep failing until its timeout raises `TimeoutError`.** It
  raised the last 429, 5xx or connection error instead, so `layerlens env` exited `1` rather than
  `11`, and `env run exec` and `env run model` left the run they had started going, though they
  cancel it at `--timeout`. They now cancel it and exit `11`, and the `TimeoutError` carries the
  last error as its `__cause__` (LAY-4429)

- **A `Retry-After` header given as a date no longer crashes a request.** The client read the
  header with `float()`, so a 429 or 503 with `Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`, a form
  RFC 9110 allows, raised a `ValueError` from inside the call instead of being retried; so did a
  negative number. The client now reads seconds or an HTTP date, and falls back to its own backoff
  for a header it cannot read. The examples in the errors guide, which called `int()` on the header,
  now read it the same way (LAY-4429)

- **Every error the SDK raises can be caught as `layerlens.<Name>`.** The errors guide has always
  shown `except layerlens.RateLimitError`, `layerlens.APIConnectionError`,
  `layerlens.InternalServerError` and others, but the package exported only seven of its error
  classes, so such an `except` clause itself raised `AttributeError` when the error came. It now
  also exports `APIStatusError`, `APIConnectionError`, `APITimeoutError`,
  `APIResponseValidationError`, `PermissionDeniedError`, `ConflictError`,
  `UnprocessableEntityError`, `RateLimitError` and `InternalServerError`: the classes the SDK
  already raised, and a test now fails if the docs name one it does not export (LAY-4429)

- **The SDK declares the pydantic it needs: `pydantic>=2.0.1`.** It declared `>=1.9.0`, but
  `import layerlens` needs 2.0.1 or later (pydantic 1.x has no `ConfigDict` or `BeforeValidator`,
  and 2.0.0 refuses a field named `model_id`), so an install that resolved an older pydantic
  broke at import instead of being refused at install

- **A key the API refuses reads as the API's sentence.** Every CLI command printed the raw
  error envelope (`Error code: 401 - {'status': 'error', 'error': 'Unknown API key'}`) for a
  wrong or revoked key; it now prints `Error: Unknown API key (HTTP 401)`

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

- **langchain: `tool.call` arguments are an object on every langchain-openai version.** With langchain-openai before 1.7, a tool call the model requested carried its `arguments` as the raw JSON string (`"{\"city\": \"Paris\"}"`); from 1.7 they were already an object (`{"city": "Paris"}`), as they are from the OpenAI, Anthropic, Vertex and Bedrock adapters. They are now parsed whichever version is installed, so the field has one type. LangGraph uses the same handler and gets the same fix. Arguments that cannot be decoded keep the string the model sent and log a warning with their first 200 characters; with `capture_content` off they are not read at all

- **OpenAI: tool-call arguments Python cannot decode no longer make your call fail.** Arguments holding an integer longer than Python's 4300-digit default limit, or nested deeper than Python's JSON decoder goes, made `client.chat.completions.create(...)` and `client.responses.create(...)` raise `ValueError` or `RecursionError` after the request had succeeded (a streamed response raised after its last chunk). They are now kept as the string the model sent and logged as a warning, like malformed JSON. Azure OpenAI, OpenRouter and LiteLLM, which reuse this parsing, are fixed too

- **OpenAI prompt-cache writes are recorded and priced as OpenAI bills them.** OpenAI reports the input tokens a request writes to its prompt cache as `cache_write_tokens`, on Chat Completions and the Responses API, and the SDK ignored them. OpenAI, Azure OpenAI and OpenRouter calls now carry the count on `model.invoke` and `cost.record` (`cache_write_tokens`) and in the OpenTelemetry attribute `gen_ai.usage.cache_creation_input_tokens`. OpenAI counts these tokens inside the prompt tokens, and each is priced once: GPT-5.6 and later models charge 1.25 times the input price for a cache write, and `cost_usd` now includes it whenever the model is priced. Earlier OpenAI models charge nothing extra for a cache write, so their costs are unchanged, and an OpenRouter cost is still the charge OpenRouter reports. The 1.25 times cache-write price now applies only to Anthropic and OpenAI GPT-5.6+ models, the providers that charge it: cache-creation tokens on any other model, such as one priced through a custom pricing table, are priced at that model's input rate

- **MCP: an async task could lose the id of the span it started from.** `mcp.async_task` events carry that span's id as `originating_span_id`. About 1 in 19,000 span ids is all digits and passes the card-number checksum, and the secret scrubber replaced those with `[REDACTED-SECRET]`, so the task could no longer be linked to the tool call that started it. Like `span_id`, `parent_span_id`, `trace_id` and `run_id`, `originating_span_id` is no longer checked for card numbers; every other secret pattern still applies to it
- **Deeply nested data in an event no longer makes your call fail.** A value nested several hundred levels deep (about 500 on Python 3.11 and earlier), such as tool-call arguments that decode to deeply nested JSON, raised `RecursionError` into your own call, for example `client.chat.completions.create(...)`: from the secret scrubber with `capture_content` on, and with it off from the redaction that strips content, unless the value was itself removed as content. Both now keep 64 levels and replace anything deeper with `[REDACTED-DEPTH]`, so the call succeeds and the event is still recorded and uploaded, without the part below that level
- **With content capture off, content inside a tuple was still uploaded for an event type the SDK doesn't know** (your own `emit`, for example). For such a type only fields recognized as metadata are kept, at every level of the payload, but a tuple was passed through unchecked, so every field inside it was uploaded. Tuples are now filtered like lists

- **Haystack 3: `Agent` runs, and components that call an LLM inside their own `run()`, report their model calls and cost.** On haystack-ai 3.x an `Agent` makes its model calls and tool calls outside the pipeline's component spans, and `LLMMessagesRouter`, `LLMRanker`, `QueryExpander`, `LLMMetadataExtractor`, `LLMDocumentContentExtractor`, `LLMEvaluator` and `FallbackChatGenerator` make theirs inside their own `run()`; the adapter saw none of these calls, so their model, token counts and cost were missing. Each provider call now has one `model.invoke` (model, token counts, finish reason) and one priced `cost.record`, also when the component calls the model from a worker thread, and a call is counted once even where haystack reports it at two levels, as it does for a `FallbackChatGenerator`. A failed `FallbackChatGenerator` attempt is its own `model.invoke`, carrying the error and its exception class. An Agent's steps appear as `agent.step` events and its tool calls as `tool.call` / `tool.result` under the tool's own name, and its run reports the Agent's tools, step limit and exit conditions in `environment.config`. Events of an Agent added to a pipeline carry `agent_name`, the name you gave the component, unless that name is generic (such as `agent`); an Agent run on its own has no name

- **Haystack: an `Agent.run()` outside a pipeline uploaded nothing.** Only a `Pipeline.run()` opened a trace, so a standalone Agent's events were dropped, on haystack-ai 2.x and 3.x. Each such run is now a trace of its own, including `Agent.run_async()` calls that run at the same time. An Agent added to a pipeline now appears under its pipeline component, with its own input, output and configuration; on haystack-ai 2.x its model calls hung off a separate root instead

- **Haystack 3: an `Agent` with a `CompactionHook` raised `AttributeError` while the adapter was connected.** The hook tags the current span with `set_tags`, which the adapter's span did not implement, so the run failed before its first model call

- **Haystack: a pipeline whose output the adapter could not serialize was never uploaded.** The failure was logged, but the run stayed open, so its trace was not uploaded and stayed bound to the calling context. The run now ends, and the trace uploads with the events emitted before the failure

- **OpenAI cache reads are priced at each model's cached-input price.** Every OpenAI model's cache reads were priced at half its input price. OpenAI charges a quarter on gpt-4.1, gpt-4.1-mini, gpt-4.1-nano, o3 and o4-mini, so their cache reads were reported at twice the bill: per 1M cache-read tokens, gpt-4.1 is now $0.50 (was $1.00), gpt-4.1-mini $0.10 (was $0.20), gpt-4.1-nano $0.025 (was $0.05) and o4-mini $0.275 (was $0.55). gpt-4o, gpt-4o-mini, o1 and o3-mini charge half and are unchanged. A model with no cached-input price on OpenAI's list (gpt-4, gpt-4-turbo, gpt-3.5-turbo, gpt-4o-2024-05-13, the embedding models and the Pro models) prices a cache read as an ordinary input token

- **o3 is priced at OpenAI's $2 input, $0.50 cached input and $8 output per 1M tokens.** It was priced at $10 input and $40 output, five times the bill, with cache reads at $5

- **o1-pro, o3-pro and gpt-4o-2024-05-13 are priced at their own prices.** With no row of their own they were priced as o1, o3 and gpt-4o: o1-pro at a tenth of its $150 input and $600 output per 1M tokens, o3-pro at half of its $20 and $80, and gpt-4o-2024-05-13 at $2.50 and $10 instead of $5 and $15

- **Fast and Ultrafast calls are priced at their tier.** OpenAI renamed Priority processing to Fast mode, and GPT-6 responses report `service_tier` as `"fast"`, which was priced as Standard: half the bill. `"fast"` is now priced at twice the Standard price, as `"priority"` already was, and `"ultrafast"` (gpt-6-astra and gpt-6.1-sol) at six times

- **An unusable price in `LAYERLENS_PRICING_TABLE` no longer breaks pricing.** A price given as a string or `null` raised `TypeError` while pricing every `cost.record` of that model, and a negative, NaN, infinite or `true` price produced a meaningless cost. Such a row is now skipped with a warning that names it, and the bundled price applies if there is one; `PricingTable` raises `ValueError` for it. A `cached` price above the row's `input` price is refused the same way

- **AP2: before Python 3.11, an expired cart got a receipt when its expiry ended in `Z`.** Python before 3.11 cannot read `cart_expiry` or `intent_expiry` written as `2026-10-16T10:00:00Z` (RFC 3339's UTC form, which AP2's Go reference merchant writes), and an expiry the adapter could not read counted as no expiry. Expiries are now read the same way on every Python version: a `Z` or `z` suffix, an offset such as `+02:00`, no offset (read as UTC), and fractions of a second of any length. Forms that only some Python versions could read are now refused on all of them: week dates (`2026-W42-5`), the basic format (`20261016T100000Z`), offsets written `+0530` or `+05`, and `T24:00`
- **AP2: an expiry the adapter cannot read now refuses the receipt.** When a cart or intent mandate carries an expiry the adapter cannot read as a date-time (for example `"tomorrow"`, `""` or `2026-02-30`), `issue_receipt` refuses the receipt with reason code `MANDATE_EXPIRY_INVALID` and emits a `policy.violation`, as it does for an expired mandate (`MANDATE_EXPIRED`). Before, such a mandate counted as never expiring. A mandate with no expiry at all still never expires. The unreadable value is no longer sent: `payment.intent_mandate` and `payment.cart_mandate` keep `intent_expiry` and `cart_expiry` even with `capture_content` off, because a date is not content, so an expiry that is not a date could carry any text into the trace. Those events now carry `null` there and `intent_expiry_invalid: true` or `cart_expiry_invalid: true`. A readable expiry is sent unchanged
- **AP2: the receipt now checks the intent's expiry, as documented.** A cart bought under an intent mandate whose `intent_expiry` has passed gets no receipt (`MANDATE_EXPIRED`); before, the adapter read `intent_expiry` but never enforced it

- **langchain: a tool call whose arguments are not valid JSON reaches the trace on langchain-openai 1.7 and later.** From 1.7, langchain-openai keeps such a call only in the message's `invalid_tool_calls`, which the adapter did not read, so the model's request produced no `tool.call` at all. It now produces one, as on earlier versions: the tool name and call id and, with `capture_content` on, the `arguments` string the model sent, logged as a warning with its first 200 characters. LangGraph uses the same handler and gets the same fix

- **LiteLLM: tool calls the model requests are traced.** No `litellm.completion` or `litellm.acompletion` call emitted a `tool.call`: the adapter recorded the response's text and token usage but not its tool calls. Each tool call, in a streamed response too, now becomes a `tool.call` with the tool name and call id and, with `capture_content` on, its `arguments` as an object, as from the OpenAI adapter

- **OpenAI, Azure OpenAI, OpenRouter and LiteLLM: tool-call arguments are no longer read with `capture_content` off.** With it off, the trace drops the arguments, but they were still decoded, and arguments that are not valid JSON were logged as a warning with their first 200 characters, so they reached your logs anyway. They are now decoded only with `capture_content` on, and once per call: OpenAI, Azure OpenAI and OpenRouter logged a malformed call's warning twice. What the trace records is unchanged

- **Anthropic and AWS Bedrock: a streamed tool call no longer breaks your loop or vanishes from the trace.** With Anthropic `messages.create(stream=True)`, tool input nested deeper than Python's JSON decoder goes raised `RecursionError` into your loop after the last event, whatever `capture_content` was set to. On Bedrock, the same input in a `converse_stream` turn dropped the call's `model.invoke`, `tool.call` and `cost.record`, and an `invoke_model` response nested that deep raised `RecursionError` from `client.invoke_model(...)`; one such chunk in an `invoke_model_with_response_stream` reply dropped the call's `model.invoke` and `cost.record`, and a request body nested that deep raised `RecursionError` from `client.invoke_model(...)` before the request was sent, or from `client.invoke_model_with_response_stream(...)` after it. A streamed Anthropic tool call also reached the trace with `null` input whenever a text block came before it, as it usually does in Claude's replies. Streamed tool input is now handled like OpenAI's: with `capture_content` on, it is the decoded object, or the string the model sent plus a warning when it does not decode; with it off, it is not read. A stream whose events cannot be aggregated (an OpenAI-compatible server that streams a tool call's arguments as an object raised `TypeError` there) no longer raises into your loop either: the call is recorded without its reply and the error is logged

- **`TraceEvaluation.judge_snapshot` is read from the API, and `status_description` is new.**
  atlas-app sends the snapshot as `judgeSnapshot`, which the model ignored, so `judge_snapshot` was
  always `None` from `client.trace_evaluations.get` and `get_many` (and `judge result`). It is now
  read. `status_description` says why a trace evaluation failed; `layerlens ci report` shows the
  judge's name and version, and that reason (LAY-4431)

- **Markdown reports show text from the API as text, never as markup.** `RunResult.to_markdown()`
  (and so `env run exec --markdown` and `env run results --markdown`) wrote a task's evidence, the
  agent's name and other text from the API into the summary as it came, escaping only `|`. That
  text can quote what an agent or a trace said, so it could put an image (fetched when the CI job
  page is opened), a link or HTML on that page. Every character that would make it markup is now
  escaped, and so is what makes GitHub link a bare `https://…` or `www.…` address; an email address
  still becomes a `mailto:` link to itself. A backtick can no longer close a code span early. `layerlens ci report` does the
  same for model and benchmark names and a judge's reasoning (LAY-4431)

- **A usage error inside a CLI command no longer reads as "Unexpected error".** Outside
  `layerlens env`, a usage error a command found itself, such as `--api-key-file` with an empty
  path, printed `Unexpected error: …` and exited `1`; it now prints click's usage message and exits
  `2`. Declining `trace delete` or `scorer delete` printed `Unexpected error:` with nothing after it;
  it now prints `Aborted!` (LAY-4431)

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
