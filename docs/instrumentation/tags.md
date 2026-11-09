# Trace tags

Tags label the traces your agent sends so that you can find them again. LayerLens filters the
traces list by tag (`layerlens trace list --tag`, `client.traces.get_many(tags=[...])`, and the
app's tag filter), runs a grader rule on every trace that carries given tags, and can aim a
batch trace evaluation at tags instead of trace ids.

## Tag a trace in code

`trace(...)`, `trace_context(...)` and `TraceCollector(...)` take `tags`, a list of strings:

```python
from layerlens import LayerLens
from layerlens.instrument import trace, trace_context

client = LayerLens()


@trace(client, name="support-agent", tags=["checkout", "v2"])
def handle(question: str) -> str:
    ...


with trace_context(client, tags=["batch-7"]):
    run_pipeline()
```

The tags of a `trace` or `trace_context` block also go on every trace started inside it. That
includes the traces some adapters open of their own even inside a block (OpenAI Agents,
LlamaIndex, OpenInference), so wrapping a run in `trace_context(client, tags=[...])` tags
whatever it records. The block's tags reach code running in the same thread or asyncio task,
and in threads started with `contextvars.copy_context().run(...)`.

## Tag every trace a process records

`LAYERLENS_TRACE_TAGS` tags every trace the process records, with no code change, which suits
CI:

```bash
export LAYERLENS_TRACE_TAGS="ci-$GITHUB_RUN_ID,$GITHUB_SHA"
python agent.py
layerlens trace list --tag "ci-$GITHUB_RUN_ID"
```

The value is a comma-separated list, or a JSON array of strings for a tag that holds a comma:
`["ci", "release 1.2, hotfix"]`. A value that starts with `[` must be a JSON array of strings;
otherwise no trace is tagged from it, and a warning says why.

[`layerlens env run exec --label`](../cli/env.md#tagging-the-agents-own-traces) adds the run's
label to this variable for the agent it runs, so the agent's traces carry the label as
LayerLens's own attempt traces do.

## How tags combine

A trace carries, in this order:

1. the tags of `LAYERLENS_TRACE_TAGS`;
2. the tags of each `trace`/`trace_context` block it started in, outermost first;
3. its own `tags`.

A tag that appears twice is sent once, in its first place. The tags are fixed as the trace
starts. Code cannot drop a tag the variable sets: whoever runs the process decides those, and CI
relies on finding every trace of its run.

`get_trace_context()` does not carry tags. A trace continued in another process with
`trace_context(client, from_context=...)` takes the tags given there, so a caller cannot tag the
traces of the service it calls.

## What a tag can be

- Each tag is trimmed, as the API's tag filter trims what it is asked for. Blank tags are dropped.
- A tag holds at most 128 bytes of UTF-8, and a trace at most 32 tags: the limits LayerLens
  applies to tags sent over OpenTelemetry too. A longer tag is dropped rather than cut, since a
  cut tag is a value nobody chose; past 32, the rest are dropped. Each logs a warning on the
  `layerlens.instrument._tags` logger, once. A run's `--label` (at most 128 bytes) always fits.
- A tag with a control or format character, such as a newline, a tab, a right-to-left override
  or a zero-width space, is dropped with a warning.
- A secret-shaped value, such as an API key, is redacted as it is in event text:
  `deploy-sk-proj-…` becomes `deploy-[REDACTED-SECRET]`.
- `tags` is a list of strings. One string, `tags="ci"`, raises `TypeError`, as does a list with
  anything but strings in it. They are raised where `tags` is given: for `@trace`, when the
  function is decorated.

None of the rest raises: tracing does not break the program it observes.

## Tags are labels, not evidence

- Tags sit beside the trace's attestation, not in it: the hash chain covers the events alone.
  Owners and admins can add tags to a trace in the app later.
- Tags are sent whatever the capture configuration, with `capture_content=False` and
  `CaptureConfig.minimal()` too, like a trace's name. Keep content and personal data out of them.
- Prefer a few stable values (a CI run, a release, an experiment) to one per request. Every
  distinct tag appears in the app's tag filter.
