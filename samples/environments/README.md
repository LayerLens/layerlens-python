# Environments in CI

Evaluate your agent against a simulated service on every pull request: create the
environment once, run the agent once per task on its own copy of the world, and fail the
build when the pass rate drops. Both paths need an **Automation** API key in
`LAYERLENS_API_KEY`.

| Sample | Shows |
| --- | --- |
| [`external_run.py`](external_run.py) | The whole loop from Python with `client.environments` |
| [`../cicd/agent_eval.yml`](../cicd/agent_eval.yml) | A GitHub Actions job with `layerlens env` |

## The tasks

One task per line of a JSONL file: the prompt the agent sees and how it is graded.
Expected values belong to one environment's world, so write them against the environment
you run on, or generate a set from its own answer key with
`layerlens env tasks generate support-desk --set ci --count 20 --budget 2.00 --wait`.

```json
{"name": "longest-wait", "prompt": "Which ticket about a late parcel has waited longest without a reply? Answer with its id.", "grader": {"kind": "answer", "config": {"expected": "9900001017", "match": "exact"}}}
```

## The agent, for `layerlens env run exec`

`exec` runs any command once per task. The command reads the attempt's address and token
from the environment, and writes its answer to `LAYERLENS_ANSWER_FILE` (or prints it):

```python
import os

import httpx

env = httpx.Client(
    base_url=os.environ["LAYERLENS_ENV_BASE_URL"],
    headers={"x-api-key": os.environ["LAYERLENS_ATTEMPT_TOKEN"]},
    timeout=60,
)
tools = env.get("/tools").json()["data"]["tools"]
answer = my_agent(os.environ["LAYERLENS_TASK_PROMPT"], tools, lambda name, args: env.post("/call", json={"tool": name, "args": args}).json()["data"])
with open(os.environ["LAYERLENS_ANSWER_FILE"], "w", encoding="utf-8") as f:
    f.write(answer)
```

```bash
layerlens env run exec support-desk --tasks-file ci/tasks.jsonl --agent-name support-agent \
  --min-pass-rate 0.9 --junit results.xml -- python ci/agent.py
```

See [docs/cli/env.md](../../docs/cli/env.md) and
[docs/api-reference/environments.md](../../docs/api-reference/environments.md).
