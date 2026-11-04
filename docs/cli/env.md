# layerlens env

`layerlens env` drives environments from a terminal or a CI job: create an environment,
wait for its world to mint, run your agent or a model against a set of tasks, and fail the
build when the pass rate drops. It calls the Automation API, like
[`client.environments`](../api-reference/environments.md), and needs an API key with
**Automation** access, in `LAYERLENS_API_KEY`, `--api-key-file PATH` (`-` reads it from
standard input) or `--api-key` (which `env run exec` refuses: see below).

```bash
export LAYERLENS_API_KEY=...   # an Automation key
layerlens env create --type zendesk --name support-desk \
  --description-file ci/environment.md --if-not-exists --wait
layerlens env run exec support-desk --tasks-file ci/tasks.jsonl \
  --agent-name support-agent --min-pass-rate 0.9 \
  --junit results.xml --markdown "$GITHUB_STEP_SUMMARY" \
  -- python ci/agent.py
```

Wherever a command takes `ENV`, pass the environment's id or its name. Results go to
stdout, and progress to stderr; with `--format json`, stdout holds only the JSON.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. With `--min-pass-rate`: the run finished and met it. |
| `1` | An error: authentication, permission, a refused request, a network or server failure. |
| `2` | The command line was invalid. |
| `10` | The run finished below `--min-pass-rate`. |
| `11` | The run or the build did not finish: it failed, was cancelled, ran out of budget or timed out, including when the API kept answering 429, a 5xx or nothing at all until `--timeout`. Also `env run exec` when every attempt ended in an error, which is how a broken command and an agent that fails on every task (a crash on import, say) both look. |

Test for `10` to tell "the agent regressed" from "the pipeline broke". Never treat `11` as a
flake to ignore: it can be an agent that fails every task, so it must block a merge as `10`
does.

## Environments

| Command | |
| --- | --- |
| `layerlens env world-types` | The types an environment can simulate, with their config knobs |
| `layerlens env list [--search TEXT] [--hide-members]` | The project's environments; `--hide-members` leaves out a composite's members |
| `layerlens env get ENV` | One environment, with its task sets |
| `layerlens env create` | Create an environment and start minting it; prints its id |
| `layerlens env status ENV` | The build: `building`, `ready` or `failed`, with its steps |
| `layerlens env wait ENV [--timeout 30m]` | Block until the build finishes; exits `11` if it failed or timed out |
| `layerlens env tools ENV` | The environment's tools |
| `layerlens env reset ENV` | Clear the live state back to the minted world |
| `layerlens env revert ENV --to-version N` | Serve an earlier version |
| `layerlens env delete ENV --yes` | Delete the environment |

`env create` options:

| Option | |
| --- | --- |
| `--type TYPE`, `--name NAME` | Required |
| `--description TEXT`, `--description-file PATH` | Required, one of them. What the world should contain, in plain words |
| `--config KEY=VALUE` | A config knob; repeatable. A value that reads as JSON (`40`, `true`) is sent as that |
| `--seed N` | Default `1` |
| `--if-not-exists` | Reuse an environment of this name created from the same type, description, config and seed; fail, naming the differences, if it differs |
| `--wait`, `--timeout DURATION` | Block until the build finishes (default `30m`) |

Step names in `env status` are informational and can change; scripts should read `status`
from `layerlens --format json env status ENV`.

An agent builds `linear` and `jira` worlds from the description, as a composite of one
world: the environment's type is `composite`, its tools are named `linear__<tool>`, the
world is a member environment named `<name> — Linear`, and `--config` and `--seed` are not
used (`env create` says so on stderr). A failed build of one is not started again: delete
the environment and create it again. `env world-types` shows these types with no config
knobs. See [Worlds an agent builds](../api-reference/environments.md#worlds-an-agent-builds).

## Task sets

```bash
layerlens env tasks list support-desk
layerlens env tasks list support-desk --set generated
layerlens env tasks generate support-desk --set generated --count 20 --budget 2.00 --wait
layerlens env tasks delete support-desk --set generated --yes
```

`tasks generate` takes `--count` (1-100), `--budget` (required), `--prompt` (defaults to the
environment's description), `--target any|discriminating|hard`, `--model` and
`--idempotency-key` (see [Retries and reruns](#retries-and-reruns)).

## Runs

Every run command takes its tasks with `--tasks-file PATH` (a JSONL file, one task per
line, stored with this run only) or `--set NAME`, and optionally `--tasks NAME,…` to run
part of them.

### env run exec

Runs your own agent, whatever it is written in, once per task. It runs on Linux and macOS
(WSL included), not on Windows. Windows does not end what an agent leaves running when it
exits, so a leftover process could go on using the attempt's token. There, drive the run
yourself (see "Driving a run yourself" below), or use `client.environments.external_run`
in Python.

```bash
layerlens env run exec support-desk --tasks-file ci/tasks.jsonl \
  --agent-name support-agent --label "$GIT_SHA" --parallel 4 \
  --min-pass-rate 0.9 --junit results.xml --markdown "$GITHUB_STEP_SUMMARY" \
  -- python ci/agent.py
```

For each task, the command after `--` runs with:

| Variable | |
| --- | --- |
| `LAYERLENS_ENV_BASE_URL` | The attempt's address: `GET /tools` and `POST /call` |
| `LAYERLENS_ATTEMPT_TOKEN` | The attempt's credential, as `x-api-key`, `Authorization: Bearer`, or the Basic password |
| `LAYERLENS_TASK_PROMPT` | The task; also on standard input |
| `LAYERLENS_TASK_ID`, `LAYERLENS_TASK_NAME`, `LAYERLENS_ATTEMPT_ID` | Which attempt this is |
| `LAYERLENS_ANSWER_FILE` | Where to write the final answer. If it is empty, standard output is the answer |

Standard input, standard output and the answer file are UTF-8. A Python command gets
`PYTHONIOENCODING=utf-8` unless you set it; write the answer file with `encoding="utf-8"`.
The command's standard error goes to the CLI's.

A non-zero exit, no answer at all, or an answer over 64 KiB grades the attempt `error`;
running past `--attempt-timeout` (default `10m`) kills the command and grades it `timeout`.
Those are verdicts, so they count against the pass rate. But when every attempt ends in an
error, the run cannot tell a broken command (a typo, a missing dependency) from an agent that
crashes on every task: the run is still recorded as it ran, its reports are written, and
`exec` exits `11` instead of gating, saying how many attempts failed, why the first one did,
and the end of its standard error. Timeouts are the agent's verdict: a run in which an
attempt timed out, or answered, is gated as usual.

A command that cannot be started at all, or an answer the API refuses (the error names the
task), cancels the run and exits `1`. Ctrl-C or SIGTERM cancels the run, if it is still open,
and exits `11`, as does grading still unfinished at `--timeout`.

#### Keeping the key from the agent

An Automation key can read the expected answers, so the command never gets
`LAYERLENS_API_KEY` (or its legacy names), nor any variable whose value holds the key. The
attempt token it gets reaches that attempt's tools and nothing else.

That keeps the key out of the agent's environment, but it is not a wall. An agent running as
the same user as `layerlens` can still find the key, for example in the environment of
`layerlens` or of the CI step's shell (`/proc/<pid>/environ`, `ps eww`). To hold the key from
an agent you do not trust, run the agent as another user, or in a container, and give it
only the attempt's variables. `exec` also refuses `--api-key`, since every user on the
machine can read a command line: give the key in `LAYERLENS_API_KEY` or with
`--api-key-file`.

The job's GitHub token is within the agent's reach too: `actions/checkout` leaves it in
`.git/config`. Check out with `persist-credentials: false`, and give the workflow
`permissions: contents: read`, as `samples/cicd/agent_eval.yml` does.

In GitHub Actions, run the agent in a container:

```yaml
- name: Run agent
  env:
    LAYERLENS_API_KEY: ${{ secrets.LAYERLENS_AUTOMATION_KEY }}
    OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}  # what the agent itself needs
  run: |
    docker build -t support-agent ci/agent
    exec layerlens env run exec support-desk --tasks-file ci/tasks.jsonl \
      --agent-name support-agent --min-pass-rate 0.9 --attempt-timeout 10m \
      -- docker run --rm -i -e LAYERLENS_ENV_BASE_URL -e LAYERLENS_ATTEMPT_TOKEN \
         -e LAYERLENS_TASK_PROMPT -e LAYERLENS_TASK_ID -e LAYERLENS_TASK_NAME \
         -e LAYERLENS_ATTEMPT_ID -e OPENAI_API_KEY \
         support-agent timeout -k 10s 9m python agent.py
```

The container sees only the variables named with `-e`, and none of the runner's processes.
It prints its answer, since `LAYERLENS_ANSWER_FILE` is a path on the runner. A timed-out
attempt kills the `docker` client, not the container, so the agent gets its own, shorter
limit inside: `timeout -k 10s 9m` sends SIGTERM at 9 minutes and SIGKILL 10 seconds later,
for an agent that ignores SIGTERM.

`exec layerlens …` hands the step's shell over to `layerlens`. When the job is cancelled, the
signal then reaches `layerlens`, which cancels the run; without `exec`, the shell does not pass
it on, and the run stays open until it expires.

| Option | |
| --- | --- |
| `--agent-name NAME` | Required. How the agent is labelled in results and traces |
| `--label TEXT` | Free text stored with the run, such as a commit SHA |
| `--parallel N` | Attempts at once, up to `25`. Default `1`. The agents' stderr is not separated |
| `--attempt-timeout DURATION` | Per attempt. Default `10m` |
| `--min-pass-rate R` | `0`-`1`; compared with the run's pass rate, over the rows whose verdict counts, and sets the exit code. A run in which no verdict counts is below any minimum above `0` |
| `--junit PATH` | A JUnit XML report, one test case per task |
| `--markdown PATH` | A Markdown summary, appended, so `$GITHUB_STEP_SUMMARY` works |
| `--timeout DURATION` | How long to wait for grading after the last attempt, then cancel the run and exit `11`. Default `30m` |

Up to 300 tasks per run, and 20 runs an hour per key on one environment. A run closes 24 hours
after it starts, and an answer submitted after that is refused (the run is cancelled and
`exec` exits `1`): keep `--parallel` and `--attempt-timeout` such that every attempt ends
well within it. At the default `10m`, one at a time, 300 tasks can take 50 hours.

### env run model

Runs a model, or an agent behind an OpenAI-compatible endpoint, through LayerLens's own
tool-calling loop:

```bash
layerlens env run model support-desk --set ci --model openai/gpt-5 --budget 5.00 --min-pass-rate 0.8
layerlens env run model support-desk --set ci --endpoint https://agents.example.com/v1 \
  --endpoint-model support-agent-v7 --endpoint-key-env SUPPORT_AGENT_KEY --budget 5.00
```

`--budget` is required, up to `100`. Also `--system-prompt-file`, `--max-steps` (up to
`50`), `--agent-name`, `--label`, `--min-pass-rate`, `--junit`, `--markdown`, `--no-wait` to
print the run id and return, and `--idempotency-key`. A run still going at `--timeout`
(default `30m`), or one the API would not show (429, a 5xx, no connection) until then, is
cancelled, and the command exits `11`; so is one interrupted with Ctrl-C or SIGTERM. Inline
tasks take up to 25 per run.

### Driving a run yourself

```bash
layerlens --format json env run start support-desk --tasks-file ci/tasks.jsonl --agent-name support-agent > run.json
RUN_ID=$(jq -r .run_id run.json)
layerlens env run submit support-desk "$RUN_ID" --attempt ATTEMPT_ID --answer-file answer.txt
layerlens env run submit support-desk "$RUN_ID" --attempt OTHER_ID --timed-out
layerlens env run finish support-desk "$RUN_ID"
layerlens env run wait support-desk "$RUN_ID" --min-pass-rate 0.9
```

`start` also takes `--idempotency-key`. `submit` takes `--answer` or `--answer-file`, or
`--error TEXT` / `--timed-out` for an agent that gave no answer, and `--trace-id` to link the
agent's own trace. Each answer is graded before `submit` returns. A run finishes on its own
when its last attempt is graded; `finish` closes it early, and attempts never submitted count
as errors. Attempts expire 24 hours after `start`.

### Reading a run

```bash
layerlens env run status support-desk RUN_ID
layerlens env run wait support-desk RUN_ID --min-pass-rate 0.9 --timeout 30m
layerlens env run results support-desk RUN_ID --junit results.xml --markdown summary.md
layerlens env run cancel support-desk RUN_ID
```

`run wait` exits `11` at `--timeout` and leaves the run going, unless given
`--cancel-on-timeout`. Every wait keeps polling through a 429, a 5xx or a lost connection
until `--timeout`, and then exits `11` naming the last error. A run that finishes just as the timeout passes, so that the cancel
finds it ended, is read once more and gated as it ended; Ctrl-C exits `11` and leaves it going too, since `wait` did not start
it.

`layerlens --format json env run results support-desk RUN_ID` prints the run with every
attempt's verdict, evidence, answer and trace id. `status` is `pending`, `in_progress`,
`success`, `failure` or `cancelled`; `verdict` is `pass`, `fail`, `error` or `timeout`, or
`off_script` when a conversation task's persona broke its script, which counts neither way.

## Retries and reruns

A request that starts something (`create`, `run exec`, `run start`, `run model`,
`tasks generate`) carries an `Idempotency-Key`, new for each command and used for the CLI's
own resends. When its reply is
lost (a timeout, a reset connection), the CLI sends it again under the same key, and gets back
what the first one started rather than a second one. When the API answers that too many jobs
are being started with the organization's keys at once, the CLI waits as told and tries
again, for up to 30 seconds. If no reply comes, or only a server error, `run start`,
`run model` and `tasks generate` exit `1` naming the key they used, as they do when
interrupted while starting (exit `11`): the run or job may have started all the same.
`run exec`'s key stays internal: it takes no `--idempotency-key`, so after a start whose reply
never came it says to run the job again, and a run it may have started closes on its own within
24 hours.

`run start`, `run model` and `tasks generate` take `--idempotency-key KEY` for that case:
send the key such a message printed, and get back the run or job it may have started rather
than start another. Do not make up a key yourself, and do not use a CI variable: the API
keeps a key forever and answers it with the first run or job it started, without comparing
the rest of the command. A key shared by the jobs or matrix legs of one workflow run, or kept
across "Re-run jobs", hands each of them the same run, the first one, for good. A command
given `--idempotency-key` treats the run it gets back as its own: `run model` cancels it at
`--timeout`.

## Limits

| | |
| --- | --- |
| `--description` | 2,000 characters |
| `--agent-name` | 64 bytes |
| `--label` | 128 bytes |
| A request body (inline tasks, mostly) | 5 MiB (5,242,880 bytes) of JSON, and 5.75 MiB (6,029,312 bytes) once the API's gateway escapes it as text, where every quote and backslash counts twice; the CLI refuses a larger one before sending it. Run fewer or shorter inline tasks, or store them in a task set and run it with `--set` |
| The reply to a start | 5 MiB of prompts, tokens and addresses; the API refuses a start that would answer with more. Start fewer tasks at once with `--tasks` |
| A run of your own agent | 300 tasks, open for 24 hours |
| An answer | 64 KiB |
