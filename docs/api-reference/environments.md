# Environments

`client.environments` drives the environments **Automation API**: create and mint an
environment, manage its task sets, and run graded evaluations of your own agent or of a
model, for example from CI. The [`layerlens env`](../cli/env.md) commands are built on it.

It needs an API key with **Automation** access (Organization Settings → API Keys). A key
with Standard access reaches only what an agent uses: `list()`, `get()`, `tools()` and
`reset_state()`. It gets `PermissionDeniedError` (403) from every other method, including
`build_status()`, `world_types()`, `task_sets` and `runs`. A `layerlens login` session cannot
reach these routes.

```python
from layerlens import LayerLens

client = LayerLens()  # reads LAYERLENS_API_KEY
```

Wherever a method takes `environment`, pass the environment's id (24 lower-case hex
digits) or its name. Names are unique within an organization.

## Create and wait

```python
env = client.environments.create(
    "support-desk",
    "zendesk",
    description="A mid-size outdoor-gear retailer's support desk: 40 open tickets, "
    "mostly returns and late deliveries, two agents on shift.",
    if_not_exists=True,
)
ready = client.environments.wait_until_ready(env.id, timeout_seconds=1800)
print(ready.id, ready.environment_version, ready.tool_count)
```

Every environment starts from a description: what the world should contain, in plain
words, up to 2,000 characters. `create` refuses a blank one before sending it.

With `if_not_exists=True`, `create` returns an environment of that name created from the
same type, description, config and seed (`env.reused` is `True`, and a build that failed
is started again). If the existing one differs, it raises `EnvironmentConflictError`,
whose `differences` names the inputs that differ (never their values).

### Worlds an agent builds

An agent builds the worlds of some types (today `linear` and `jira`;
`WorldType.built_by_agent` says which) from the description: it reads the company the
description is about, then builds that company's workspace. Such an environment is a
composite of one world, as when you create it in the app:

- `type` is `composite`, and its tools are named `<app>__<tool>`, such as
  `linear__list_issues`.
- The world itself is a second environment, a member of the composite, named
  `<name> — <Title>` (`support-desk — Linear`). `list(hide_members=True)` leaves members
  out. The member's name has to fit in 120 characters too.
- `config` and `seed` are not used, and `CreatedEnvironment.unused` says so when you
  sent either; `if_not_exists` compares only the type and description. A `config` that
  sets `org_seed` is refused.
- A failed build is not started again: with `if_not_exists=True`, `create` raises
  `EnvironmentConflictError` (with empty `differences`) until you delete the environment
  and create it again.

`wait_until_ready` raises `EnvironmentBuildError` with the generator's reason
(`refused_because`) when the build fails, and `TimeoutError` when it is still building at
the deadline.

Every wait (`wait_until_ready`, `jobs.wait`, `runs.wait`) keeps polling through a 429, a 5xx
or a lost connection. It sends each poll once and does the retrying itself: the next poll
follows half a second later, then after twice as long each time it fails again, up to the
poll interval, and never sooner than the reply's `Retry-After`, up to 5 minutes. When its
`timeout_seconds` passes while the polls are still failing, it raises `TimeoutError`,
carrying the last error as its `__cause__`; any other error, such as a 403, is raised at
once.

| Method | |
| --- | --- |
| `create(name, type, *, description, config=None, seed=None, if_not_exists=False)` | Create and start the mint. Returns `CreatedEnvironment` (`id`, `environment`, `job`, `reused`, `unused`) |
| `get(environment)` | One `Environment` |
| `list(*, name=None, search=None, hide_members=False)` | The project's environments; `hide_members` leaves out a composite's members |
| `resolve(environment)` | The id of an environment given by id or name |
| `build_status(environment)` | `EnvironmentBuild`: `status` (`building`, `ready`, `failed`), `steps`, counts, `refused_because` |
| `wait_until_ready(environment, *, timeout_seconds=1800)` | Block until the build finishes; returns the `Environment` |
| `world_types()` | The types an environment can simulate, with their config knobs and `built_by_agent` |
| `tools(environment)` | The environment's tools |
| `reset_state(environment)` | Clear the live state, the one Agent API calls write to |
| `revert(environment, *, to_version)` | Serve an earlier version |
| `delete(environment)` | Delete |

Build step names are informational and can change; read `status`.

## Task sets

```python
job = client.environments.task_sets.generate(env.id, "generated", count=20, budget_usd=2.0, target="hard")
job = client.environments.jobs.wait(env.id, job.id)
print(job.status, job.result)

for task in client.environments.task_sets.get(env.id, "generated").tasks:
    print(task.name, task.grader.kind)
```

| Method | |
| --- | --- |
| `task_sets.list(environment)` | The sets, with the number of tasks that are not retired |
| `task_sets.get(environment, set_name)` | One set and all of its tasks |
| `task_sets.generate(environment, set_name, *, count, budget_usd, prompt=None, target=None, model=None)` | Generate tasks from the environment's answer key; returns a `Job` |
| `task_sets.delete(environment, set_name)` | Delete the set's tasks; returns how many |
| `jobs.get(environment, job_id)`, `jobs.wait(environment, job_id, *, timeout_seconds=1800)` | A background job; `wait` returns it whatever its `status` |

`target` is `discriminating` (the default), `hard` or `any`. `prompt` defaults to the
environment's description.

A run can also take its tasks inline, from a list or a JSONL file, without storing a set:
one task per line, each with `name`, `prompt` and `grader`. Inline tasks take every grader
kind except `plot` and `criteria`, which grade a task a world's plot made and a conversation
task.

```json
{"name": "longest-wait", "prompt": "Which ticket about a late parcel has waited longest without a reply? Answer with its id.", "grader": {"kind": "answer", "config": {"expected": "9900001017", "match": "exact"}}}
```

## Run your own agent

`external_run` hands out one attempt per task. Each attempt has its own `base_url` and
`token`, serves its own copy of the world, and is graded when you submit it.

```python
import httpx


def run_agent(prompt: str, base_url: str, token: str) -> str:
    env = httpx.Client(base_url=base_url, headers={"x-api-key": token}, timeout=60)
    tools = env.get("/tools").json()["data"]["tools"]
    reply = env.post("/call", json={"tool": tools[0]["name"], "args": {}}).json()["data"]
    ...
    return "9900001017"


with client.environments.external_run(
    env.id, tasks_file="ci/tasks.jsonl", agent_name="support-agent", label=git_sha
) as run:
    for attempt in run.attempts:
        attempt.submit(run_agent(attempt.prompt, attempt.base_url, attempt.token))

result = run.wait()
print(f"{result.passed}/{result.total} passed")
```

- **Tasks:** `tasks_file=`, `tasks=[...]` (dicts or `TaskSpec`), or `set_name=`, plus
  `task_names=[...]` to run part of them. Up to 300 tasks per run. A set's conversation
  tasks and tasks graded on a world's plot can't be graded for your own agent, and starting
  a run with them is refused (`BadRequestError`): leave them out with `task_names`.
- **The attempt's address** serves `GET {base_url}/tools` and `POST {base_url}/call`. The
  token works as `x-api-key`, as `Authorization: Bearer`, as a bare `Authorization` value,
  or as the HTTP Basic password. It reaches that attempt's tools and nothing else, and stops
  working once the attempt is submitted, the run ends, or 24 hours pass. It is left out of
  the `repr()` of `ExternalRunStart` and `ExternalRunAttempt`, so logging them does not leak
  it; `model_dump()` still has it.
- **Submitting:** `attempt.submit(answer, *, trace_id=None)` returns once the answer is
  graded. `attempt.submit_error(error=None, *, timeout=False, trace_id=None)` records an agent
  that gave no answer, as verdict `error` or `timeout`. Each attempt is submitted once.
  `trace_id` links the agent's own trace, which the results carry as `agent_trace_id`: the
  id of a trace in the organization, or the SDK or OpenTelemetry trace id of one in the
  project. Any other is not linked; the answer is still graded, and the returned
  `SubmitResult.trace_id_unlinked` says why (`None` when linked). One over 256 bytes raises
  `ValueError` before anything is sent. So does an answer, or an
  `error`, over 64 KiB (65,536 bytes), the API's limit; each is measured without the
  whitespace around it, as the API measures it.
- **Finishing:** the run finishes on its own when its last attempt is graded. Leaving the
  `with` block finishes it, grading unsubmitted attempts `error`; a run in which no attempt
  was submitted at all ends `failure`. Leaving the block on an exception cancels the run
  instead, and says what came of it: `run.cancelled_run` is the run the cancel left,
  `run.finished_before_cancel` is `True` when every attempt had been graded already (nothing to
  cancel, so nothing is logged), and `run.cancel_error` is why a cancel failed (logged as a
  warning). `run.finish()`, `run.cancel()` and `run.wait()` are there for a run
  started with `run.start()` instead of `with`.
- **Limits:** 20 runs an hour per key on one environment, and 100 per environment
  (`RateLimitError`, 429). `agent_name` takes up to 64 bytes and `label` up to 128. A run
  closes 24 hours after it starts, and an answer submitted after that is refused
  (`ConflictError`). The start's reply holds every attempt's prompt, address and token, up
  to 5 MiB; a start that would answer with more is refused (413): run fewer tasks at once
  with `task_names`.
- **Grading** uses LayerLens's grading model for `judge` graders. Only `judge` graders call
  a model.

For concurrency, submit from threads, or use `AsyncLayerLens`, whose `external_run` is an
`async with`:

```python
import asyncio

from layerlens import AsyncLayerLens


async def main() -> None:
    client = AsyncLayerLens()

    async def attempt_once(attempt) -> None:
        await attempt.submit(await my_agent(attempt.prompt, attempt.base_url, attempt.token))

    async with client.environments.external_run("support-desk", set_name="ci", agent_name="support-agent") as run:
        await asyncio.gather(*(attempt_once(a) for a in run.attempts))
    print((await run.wait()).pass_rate)
```

The low-level calls behind `external_run` are `runs.start_external(...)`,
`runs.submit(environment, run_id, attempt_id, answer, *, outcome="answered", error=None, trace_id=None)`
and `runs.finish(environment, run_id)`.

## Run a model or an endpoint

```python
run = client.environments.runs.run_model(env.id, set_name="ci", model="openai/gpt-5", budget_usd=5.0)
result = client.environments.runs.wait(env.id, run.run_id)
```

`model` is a catalog model or `custom:<key>` for one of your project's custom models. Pass
`endpoint={"base_url": ..., "model": ..., "api_key": ...}` instead to run an agent behind an
OpenAI-compatible chat-completions endpoint. `budget_usd` is required, up to 100; the run
stops when it is spent. Inline tasks (`tasks=` or `tasks_file=`) take up to 25 per run.
`system_prompt`, `max_steps` (up to 50), `agent_name` and `label` are optional.

## Results

`runs.get(environment, run_id)` and every `wait` return a `RunResult`:

| Field | |
| --- | --- |
| `run_id`, `kind` | `external` (your agent) or `rollout` (a model or endpoint) |
| `status` | `pending`, `in_progress`, `success`, `failure` or `cancelled` |
| `stop_reason`, `error` | Why it stopped; `error` says why a `failure` failed |
| `total`, `passed`, `failed`, `errored`, `timed_out`, `pending` | One row per task |
| `counted`, `pass_rate` | `pass_rate` is `passed / counted`. `counted` is the graded rows whose verdict counts, leaving out plays a cancel cut short and tasks a cancel or the budget stopped before they ran. `pass_rate` is `None` until one is graded |
| `cost_usd` | `None` when the cost is not known (an endpoint, `own:` or `custom:` model) |
| `attempts` | Each with `task_name`, `task_version`, `verdict` (`pass`, `fail`, `error`, `timeout`; `None` while ungraded), `grader`, `evidence`, `error_source`, `agent_answer`, `agent_answer_truncated`, `trace_id` and `agent_trace_id`. `agent_answer` is the first 4096 bytes of the agent's answer, cut between characters, and `agent_answer_truncated` is `True` when it was cut; the whole answer (up to 64 KiB) is what was graded |
| `uncounted` | Rows whose verdict says nothing about the agent: `off_script` (a conversation task's persona broke its script) or `not_graded`. They are neither passes nor failures: `failures()` leaves them out, and JUnit reports them as skipped |

A task your agent never submitted is a row with verdict `error` and `error_source`
`agent`. It counts against the pass rate, unless the run was cancelled. Its `attempt_id`
is the one the run handed out, though nothing was stored under it.

A run the budget or a cancel stopped is `failure` or `cancelled`, and its `pass_rate`
covers only the tasks that ran: read `status` before `pass_rate`, as `--min-pass-rate`
does (it exits `11` before comparing).

A run is `failure` when it ran out of budget, could not start, any task ended in an error
on the platform's side, or, for your own agent, no attempt was submitted. Errors the agent
causes (a non-zero exit, a timeout, an attempt never submitted) are verdicts: they count
against the pass rate and do not fail the run.

```python
result = client.environments.runs.get(env.id, run_id)
for failed in result.failures():
    print(failed.task_name, failed.verdict, failed.evidence)
result.to_junit("results.xml")  # one test case per task
print(result.to_markdown())  # a summary for a CI job page
```

## Finding runs

`runs.list(environment, ...)` returns one page of the environment's runs, newest first, as a
`RunPage` (`runs`, `next_cursor`); `runs.iter(environment, ...)` reads every page as you go.
Each run is a `RunSummary`: a `RunResult` without `attempts`, plus `created_at`,
`started_at` and `finished_at`.

| Argument | |
| --- | --- |
| `label`, `agent_name` | Exact matches on what the run was started with. A blank one raises `InvalidArgument` |
| `kind` | `"external"` or `"rollout"` |
| `status` | The status a run shows, as `runs.get` returns it |
| `limit` (`page_size` on `iter`) | Up to 50 a page; 20 by default |
| `cursor` | A page's `next_cursor`, to read the page after it |

A page filtered by `status` can hold fewer runs than `limit`, or none, and still have a
`next_cursor`: the API reads at most 200 runs per request looking for ones that show it.

```python
# The newest run on main that finished.
page = client.environments.runs.list("support-desk", label="main", status="success", limit=1)
baseline = client.environments.runs.get("support-desk", page.runs[0].run_id)
```

## Comparing two runs

`RunComparison.between(baseline, candidate)` compares two finished `RunResult`s task by
task (it raises `ValueError` for a run still going). A task regressed when it passed on the
baseline and fails on the candidate (`fail`, `error` or `timeout`); `regression` is true when
any did. Tasks match by `task_id`, and a task in one run only is listed, never a regression.

| Field | |
| --- | --- |
| `baseline`, `candidate` | The two runs, as `RunSummary` |
| `pass_rate_delta` | `candidate.pass_rate - baseline.pass_rate`; `None` when either is `None` |
| `regressed`, `recovered` | `TaskChange`s: `task_id`, `task_name`, and the task's row in each run (`baseline`, `candidate`) |
| `only_in_baseline`, `only_in_candidate` | The rows of tasks in one run only |
| `regression` | Whether any task regressed |
| `environment_versions_differ` | The runs were served different versions of the environment |

```python
comparison = RunComparison.between(baseline, candidate)
for change in comparison.regressed:
    print(change.task_name, change.candidate.verdict, change.candidate.evidence)
print(comparison.to_markdown())  # a summary for a CI job page
```

`layerlens env run compare` is the same comparison from a CI step.

## Retries

The SDK sends an `Idempotency-Key` with each call that creates or starts something
(`create`, `task_sets.generate`, `runs.run_model`, `runs.start_external`), so its own
retries never create a second environment, job or run. Such a call is also sent again,
under the same key, when its reply is lost (a timeout, a reset connection), as often as the
client retries (`max_retries`, default 2), and when the API answers 503 with `Retry-After`
(too many jobs being started with the organization's keys at once) it waits as told and
sends it again, for up to 30 seconds in all. Pass `idempotency_key=` to make a retry from
another process safe too: the same key for the same one run you mean to start. The API keeps
a key forever and returns what it created the first time, on the same route, even if the new
request differs: it does not compare them. So never reuse a key for a different run, and
never derive one from a CI variable shared by several jobs or reruns.

A request body is at most 5 MiB (5,242,880 bytes) of JSON, and at most 5.75 MiB (6,029,312
bytes) once the API's gateway escapes it as text for Lambda, where every quote and backslash
counts twice. The SDK refuses a larger one (`ValueError`) before sending it, since one past
the gateway's limit would come back as a bare 5xx. The refusal says what to send less of: for
a run, fewer or shorter inline tasks, or a set run with `set_name`; for a submit, the answer
without the whitespace around it. A 413 from the API is never retried.

A submit that times out, or fails on the server's side, may still have been graded. The
SDK retries it and reads the run to settle it, within about 7 minutes. Submitting an
attempt that is already graded raises `ConflictError`.

## Errors

| Exception | When |
| --- | --- |
| `EnvironmentConflictError` (a `ConflictError`) | `create`: the name is taken by an environment this call cannot reuse. `differences` and `project_id` say why; when both are empty, the message does |
| `EnvironmentBuildError` | `wait_until_ready`: the build failed. `refused_because` and `build` |
| `TimeoutError` | A `wait` reached its `timeout_seconds`, with what it was waiting for still going, or with its polls still failing (429, 5xx, no connection); then `__cause__` is the last error |
| `PermissionDeniedError` | The key has Standard access |
| `RateLimitError` | An external-run limit, or the organization's limits on work started with API keys: 10 jobs in progress, and $200 a UTC day |
| `APIResponseValidationError` | A reply the SDK could not read, such as a task set page whose `next_offset` does not move forward. Never `None` |
| `ValueError` | An argument the SDK refuses before sending, such as two sources of tasks, or a body over 5 MiB (5.75 MiB escaped) |

## See also

* [`layerlens env`](../cli/env.md)
* [Error Handling](errors.md)
