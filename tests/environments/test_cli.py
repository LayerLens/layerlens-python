from __future__ import annotations

import os
import sys
import json
import signal
import threading
from typing import Any, Dict, List, Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from layerlens import LayerLens
from layerlens.cli._app import cli
from layerlens._exceptions import AuthenticationError
from layerlens.cli._client import build_client as real_build_client

from .conftest import (
    ENV_ID,
    RUN_ID,
    FakeAPI,
    ok,
    err,
    row,
    body_of,
    run_view,
    submitted,
    environment,
    make_client,
)

KEY_ENV = {"LAYERLENS_API_KEY": "test-key", "LAYERLENS_STRATIX_API_KEY": "legacy-key"}
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="signals sent to this process are POSIX")
A = ["6a91c0e5f1d2a3b4c5d6e80" + str(i) for i in range(1, 5)]


@pytest.fixture(autouse=True)
def _client_on_the_fake_api(api: FakeAPI) -> Iterator[None]:
    with patch("layerlens.cli.commands.env._common.build_client", lambda ctx, key, use_bearer_auth: make_client(api)):
        yield


def invoke(runner: CliRunner, *args: str, env: Dict[str, str] = KEY_ENV) -> Any:
    return runner.invoke(cli, ["-q", *args], env=env, catch_exceptions=False)


class TestExitCodes:
    def test_both_task_sources_is_a_usage_error(self, runner: CliRunner, tmp_path: Path) -> None:
        tasks = tmp_path / "t.jsonl"
        tasks.write_text("{}\n")
        result = invoke(
            runner,
            "env",
            "run",
            "exec",
            ENV_ID,
            "--tasks-file",
            str(tasks),
            "--set",
            "ci",
            "--agent-name",
            "a",
            "--",
            "true",
        )
        assert result.exit_code == 2
        assert "exactly one of --tasks-file or --set" in result.stderr

    def test_a_missing_command_is_a_usage_error(self, runner: CliRunner) -> None:
        result = invoke(runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a")
        assert result.exit_code == 2

    def test_a_refused_request_exits_1_with_the_apis_sentence(self, runner: CliRunner, api: FakeAPI) -> None:
        message = "this key has Standard access; this route needs an Automation key (Organization Settings → API Keys)"
        api.on("GET", f"/{ENV_ID}/build", err(403, message))
        api.on("GET", f"/{ENV_ID}", ok({"environment": environment()}))
        result = invoke(runner, "env", "status", ENV_ID)
        assert result.exit_code == 1
        assert message in result.stderr and "HTTP 403" in result.stderr

    def test_no_api_key_exits_1(self, runner: CliRunner) -> None:
        result = invoke(runner, "env", "list", env={"LAYERLENS_API_KEY": ""})
        assert result.exit_code == 1
        assert "needs an Automation API key" in result.stderr

    def test_a_failed_build_exits_11(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}/build", ok({"status": "failed", "steps": [], "refused_because": "no such world"}))
        result = invoke(runner, "env", "wait", ENV_ID)
        assert result.exit_code == 11 and "no such world" in result.stderr

    @pytest.mark.parametrize(
        "view, min_rate, code",
        [
            (run_view(row(A[0], "pass"), row(A[1], "pass")), "0.9", 0),
            (run_view(row(A[0], "pass"), row(A[1], "fail")), "0.9", 10),
            (run_view(row(A[0], "pass"), row(A[1], "fail")), "0.5", 0),
            (run_view(row(A[0], "pass"), row(A[1], "error", error_source="platform"), status="failure"), "0", 11),
            (run_view(row(A[0], "pass"), status="cancelled"), "0", 11),
            (run_view(row(A[0], "pass"), row(A[1], "off_script")), "0.9", 0),
            (run_view(row(A[0], "off_script"), row(A[1], "not_graded")), "0.5", 10),
            (run_view(row(A[0], "off_script"), row(A[1], "not_graded")), "0", 0),
            ({**run_view(row(A[0], "pass"), row(A[1], "off_script"), pass_rate=0.5), "counted": None}, "0.9", 10),
        ],
    )
    def test_a_run_gates_the_build(self, runner: CliRunner, api: FakeAPI, view: dict, min_rate: str, code: int) -> None:
        api.on("GET", f"/{ENV_ID}/runs/{RUN_ID}", ok(view))
        result = invoke(runner, "env", "run", "wait", ENV_ID, RUN_ID, "--min-pass-rate", min_rate)
        assert result.exit_code == code


def test_the_summary_adds_up_when_a_verdict_is_not_counted(runner: CliRunner, api: FakeAPI) -> None:
    api.on("GET", f"/{ENV_ID}/runs/{RUN_ID}", ok(run_view(row(A[0], "pass"), row(A[1], "off_script"))))
    result = invoke(runner, "env", "run", "status", ENV_ID, RUN_ID)
    assert "1/1 passed (100.0%)" in result.stdout and "1 not counted" in result.stdout


def test_a_run_with_nothing_counted_says_why_it_misses_the_gate(runner: CliRunner, api: FakeAPI) -> None:
    api.on("GET", f"/{ENV_ID}/runs/{RUN_ID}", ok(run_view(row(A[0], "off_script"))))
    result = invoke(runner, "env", "run", "wait", ENV_ID, RUN_ID, "--min-pass-rate", "0.5")
    assert result.exit_code == 10 and "No task's verdict counts toward the pass rate" in result.stderr


class TestEnvironmentCommands:
    def test_world_types_shows_no_knobs_for_a_type_an_agent_builds(self, runner: CliRunner, api: FakeAPI) -> None:
        linear = {
            "type": "linear",
            "title": "Linear",
            "tool_count": 30,
            "config_schema": {"n_issues": {"type": "integer"}},
            "agent_slice": True,
            "org_knobs": {"counts": {}, "app": "linear"},
        }
        api.on("GET", "/world-types", ok({"world_types": [linear], "count": 1}))
        result = invoke(runner, "env", "world-types")
        assert result.exit_code == 0
        assert "an agent builds it from the description" in result.stdout and "n_issues" not in result.stdout
        as_json = invoke(runner, "--format", "json", "env", "world-types")
        assert json.loads(as_json.stdout)[0]["built_by_agent"] is True

    def test_list_can_leave_out_members(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on("GET", "", ok({"environments": [], "count": 0, "total_count": 0}))
        assert invoke(runner, "env", "list", "--hide-members").exit_code == 0
        assert api.requests[0].url.params["hide_members"] == "true"


class TestCreate:
    def test_prints_the_id_and_sends_typed_config(self, runner: CliRunner, api: FakeAPI, tmp_path: Path) -> None:
        created = {"environment": {"id": ENV_ID, "name": "support-desk", "type": "zendesk", "current_version": 0},
                   "job": {"id": "j1", "status": "pending"}}  # fmt: skip
        api.on("POST", "/worlds", ok(created, 201))
        description = tmp_path / "environment.md"
        description.write_text("A support desk.\n")
        result = invoke(
            runner, "env", "create", "--type", "zendesk", "--name", "support-desk", "--description-file",
            str(description), "--config", "n_tickets=40", "--config", "persona=calm", "--if-not-exists",
        )  # fmt: skip
        assert result.exit_code == 0
        assert result.stdout.strip() == ENV_ID
        assert body_of(api.requests[0])["config"] == {"n_tickets": 40, "persona": "calm"}
        assert body_of(api.requests[0])["description"] == "A support desk.\n"

    def test_json_output_is_only_json(self, runner: CliRunner, api: FakeAPI) -> None:
        created = {"environment": {"id": ENV_ID, "name": "d", "type": "zendesk", "current_version": 0}, "job": None}
        api.on("POST", "/worlds", ok(created, 201))
        result = invoke(
            runner, "--format", "json", "env", "create", "--type", "zendesk", "--name", "d", "--description", "A desk"
        )
        assert json.loads(result.stdout)["environment"]["id"] == ENV_ID

    def test_json_with_wait_prints_the_environment_and_its_build_as_the_wait_left_them(
        self, runner: CliRunner, api: FakeAPI
    ) -> None:
        # G1: it printed the create reply, whose build read "pending", after the build had finished.
        job_id = "6aef0dd3415f6c3a7acc330b"
        created = {"environment": {"id": ENV_ID, "name": "d", "type": "bank", "current_version": 1},
                   "job": {"id": job_id, "status": "pending"}, "reused": False, "unused": None}  # fmt: skip
        finished = {"id": job_id, "kind": "mint_world", "status": "success", "stop_reason": "finished",
                    "created_at": "2026-11-07T11:38:59.603Z", "finished_at": "2026-11-07T11:39:26.081Z",
                    "cost_usd": 0.001042125, "result": {"version": 1, "tools": 34}}  # fmt: skip
        api.on("POST", "/worlds", ok(created, 201))
        api.on("GET", f"/{ENV_ID}/build", ok({"status": "ready", "version": 1, "steps": []}))
        api.on("GET", f"/{ENV_ID}", ok({"environment": environment(name="d", type="bank", tool_count=34)}))
        api.on("GET", f"/{ENV_ID}/jobs/{job_id}", ok(finished))
        args = ["env", "create", "--type", "bank", "--name", "d", "--description", "A bank", "--wait"]
        result = invoke(runner, "--format", "json", *args)
        assert result.exit_code == 0, result.stderr
        printed = json.loads(result.stdout)
        assert printed["job"]["status"] == "success" and printed["job"]["id"] == job_id
        assert (printed["environment"]["id"], printed["environment"]["environment_version"]) == (ENV_ID, 1)
        assert printed["environment"]["tool_count"] == 34
        assert (printed["reused"], printed["unused"]) == (False, None)
        # Without --format json it still prints the id alone.
        assert invoke(runner, *args).stdout.strip() == ENV_ID

    def test_different_inputs_exit_1_naming_them(self, runner: CliRunner, api: FakeAPI) -> None:
        body = {
            "status": "error",
            "error": "already exists, created from a different config",
            "differences": ["config"],
        }
        api.on("POST", "/worlds", (409, body))
        result = invoke(
            runner, "env", "create", "--type", "zendesk", "--name", "d", "--description", "A desk", "--if-not-exists"
        )
        assert result.exit_code == 1 and "different config" in result.stderr

    def test_no_description_is_a_usage_error(self, runner: CliRunner, api: FakeAPI) -> None:
        result = invoke(runner, "env", "create", "--type", "zendesk", "--name", "d")
        assert result.exit_code == 2 and "--description" in result.stderr
        assert api.requests == []

    def test_settings_the_world_does_not_use_are_named_on_stderr(self, runner: CliRunner, api: FakeAPI) -> None:
        created = {
            "environment": {"id": ENV_ID, "name": "d", "type": "composite", "current_version": 0},
            "job": {"id": "6a0000000000000000000002", "status": "pending"},
            "unused": "the settings and seed sent with it are not used.",
        }
        api.on("POST", "/worlds", ok(created, 201))
        result = invoke(
            runner, "env", "create", "--type", "jira", "--name", "d", "--description", "A desk", "--seed", "3"
        )
        assert result.exit_code == 0 and result.stdout.strip() == ENV_ID
        assert "Note: the settings and seed sent with it are not used." in result.stderr


AGENT = r"""
import os, sys, json, time
name = os.environ["LAYERLENS_TASK_NAME"]
seen = {
    "prompt_on_stdin": sys.stdin.read() == os.environ["LAYERLENS_TASK_PROMPT"],
    "keys": sorted(k for k in os.environ if k in ("LAYERLENS_API_KEY", "LAYERLENS_STRATIX_API_KEY", "LAYERLENS_ATLAS_API_KEY")),
    "copies": sorted(k for k, v in os.environ.items() if "test-key" in v),
    "token": os.environ["LAYERLENS_ATTEMPT_TOKEN"],
    "base_url": os.environ["LAYERLENS_ENV_BASE_URL"],
    "attempt": os.environ["LAYERLENS_ATTEMPT_ID"],
}
with open(os.environ["SEEN_DIR"] + "/" + name + ".json", "w") as f:
    json.dump(seen, f)
if name == "task-1":
    open(os.environ["LAYERLENS_ANSWER_FILE"], "w").write("from the file")
elif name == "task-2":
    print("from stdout")
elif name == "task-3":
    sys.exit(3)
else:
    time.sleep(30)
"""


TRACE_IDS = {f"task-{i}": "6a91c0f2a7b8c9d0e1f2a3b" + str(i) for i in range(1, 4)}
TRACE_IDS["task-4"] = "x" * 300
# A well-formed trace id that names no trace, and the API's answer to it (N3).
FAKE_TRACE = "6aee0000000000000000feed"
UNLINKED = f'the trace id "{FAKE_TRACE}" names no trace in this organization, so the attempt is not linked to it'

TRACING_AGENT = r"""
import os, sys, time, json
name = os.environ["LAYERLENS_TASK_NAME"]
open(os.environ["LAYERLENS_TRACE_ID_FILE"], "w").write(json.loads(os.environ["TRACE_IDS"])[name])
if name == "task-2":
    sys.exit(3)
if name == "task-3":
    time.sleep(30)
print("the answer")
"""


# An agent traced with the LayerLens SDK, its uploads caught: each attempt writes the tags
# its trace was uploaded with to SEEN_DIR/<task>.tags.json.
TAGGING_AGENT = r"""
import os, json
from layerlens.models import CreateTracesResponse
from layerlens.instrument import emit, trace_context

class Traces:
    def upload(self, path):
        with open(path) as f:
            payload = json.load(f)[0]
        seen = os.path.join(os.environ["SEEN_DIR"], os.environ["LAYERLENS_TASK_NAME"] + ".tags.json")
        with open(seen, "w") as f:
            json.dump(payload.get("tags"), f)
        return CreateTracesResponse(trace_ids=["6a91c0f2a7b8c9d0e1f2a3b4"])

class Client:
    traces = Traces()

with trace_context(Client(), tags=["support-agent"]):
    emit("tool.call", {"tool_name": "lookup"})
print("the answer")
"""


# Each attempt writes the exit budget it was given (LAYERLENS_EXIT_TIMEOUT) to SEEN_DIR/<task>.exit.
EXIT_BUDGET_AGENT = r"""
import os
with open(os.path.join(os.environ["SEEN_DIR"], os.environ["LAYERLENS_TASK_NAME"] + ".exit"), "w") as f:
    f.write(os.environ.get("LAYERLENS_EXIT_TIMEOUT", "<unset>"))
print("the answer")
"""


def _exec_api(api: FakeAPI, final: dict) -> None:
    start = {
        "run_id": RUN_ID,
        "attempts": [
            {
                "attempt_id": a,
                "task_id": "6a8f2c1d9e0b4a7f3c5d6e1" + a[-1],
                "task_name": f"task-{a[-1]}",
                "prompt": f"Prompt {a[-1]}",
                "base_url": f"https://api.test/api/v1/attempts/{a}",
                "token": f"llat_v1_{RUN_ID}_{a}_" + "m" * 43,
                "expires_at": "2026-10-27T09:14:22Z",
            }
            for a in A
        ],
    }
    api.on("POST", f"/{ENV_ID}/external-runs", ok(start, 201))
    for a in A:
        api.on("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{a}/submit", ok(submitted(a, total=4)))
    api.on("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/finish", ok(final))
    api.on("GET", f"/{ENV_ID}/runs/{RUN_ID}", ok(final))
    api.on("POST", f"/{ENV_ID}/runs/{RUN_ID}/cancel", ok({**final, "status": "cancelled"}))


def _submits(api: FakeAPI) -> Dict[str, Any]:
    return {r.url.path.split("/")[-2]: body_of(r) for r in api.requests if r.url.path.endswith("/submit")}


class TestExec:
    FINAL = run_view(
        row(A[0], "pass"),
        row(A[1], "fail"),
        row(A[2], "error", error_source="agent"),
        row(A[3], "timeout", error_source="agent"),
    )

    def test_runs_the_agent_once_per_attempt_and_submits_what_it_did(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path
    ) -> None:
        _exec_api(api, self.FINAL)
        agent = tmp_path / "agent.py"
        agent.write_text(AGENT)
        summary = tmp_path / "summary.md"
        summary.write_text("# Earlier step\n")
        junit = tmp_path / "results.xml"
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "support-agent", "--parallel", "4",
            "--attempt-timeout", "2s", "--min-pass-rate", "0.9", "--junit", str(junit), "--markdown", str(summary),
            "--", sys.executable, str(agent),
            env={**KEY_ENV, "SEEN_DIR": str(tmp_path), "CI_AUTH_HEADER": "x-api-key: test-key"},
        )  # fmt: skip
        assert result.exit_code == 10, result.stderr

        submits = _submits(api)
        assert submits[A[0]] == {"outcome": "answered", "answer": "from the file"}
        assert submits[A[1]] == {"outcome": "answered", "answer": "from stdout\n"}
        assert submits[A[2]] == {"outcome": "error", "error": "the agent exited with status 3"}
        assert submits[A[3]]["outcome"] == "timeout"

        for i, a in enumerate(A, start=1):
            seen = json.loads((tmp_path / f"task-{i}.json").read_text())
            assert seen["keys"] == [] and seen["copies"] == [] and seen["prompt_on_stdin"]
            assert seen["attempt"] == a and seen["token"].startswith(f"llat_v1_{RUN_ID}_{a}")
            assert seen["base_url"].endswith(f"/attempts/{a}")

        assert api.requests[-1].url.path.endswith(f"/runs/{RUN_ID}")
        assert any(r.url.path.endswith("/finish") for r in api.requests)
        assert summary.read_text().startswith("# Earlier step\n### LayerLens")
        assert 'failures="1" errors="2"' in junit.read_text()
        assert "below --min-pass-rate" in result.stderr

    def test_the_agents_own_trace_is_linked_to_its_attempt_however_it_ends(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path
    ) -> None:
        _exec_api(api, self.FINAL)
        agent = tmp_path / "agent.py"
        agent.write_text(TRACING_AGENT)
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "support-agent", "--parallel", "4",
            "--attempt-timeout", "2s", "--", sys.executable, str(agent),
            env={**KEY_ENV, "TRACE_IDS": json.dumps(TRACE_IDS)},
        )  # fmt: skip
        assert result.exit_code == 0, result.stderr

        submits = _submits(api)
        assert submits[A[0]] == {"outcome": "answered", "answer": "the answer\n", "trace_id": TRACE_IDS["task-1"]}
        assert submits[A[1]] == {
            "outcome": "error",
            "error": "the agent exited with status 3",
            "trace_id": TRACE_IDS["task-2"],
        }
        assert submits[A[2]]["outcome"] == "timeout" and submits[A[2]]["trace_id"] == TRACE_IDS["task-3"]
        # An id the API would refuse is not sent: the answer is still graded, and the run goes on.
        assert submits[A[3]] == {"outcome": "answered", "answer": "the answer\n"}
        assert api.calls("POST", f"/{ENV_ID}/runs/{RUN_ID}/cancel") == []

        assert f"task-1: answered (" in result.stderr and f"trace {TRACE_IDS['task-1']}" in result.stderr
        assert "task-4: answered (" in result.stderr
        assert "no trace linked: LAYERLENS_TRACE_ID_FILE: trace_id is 300 bytes; the limit is 256" in result.stderr

    def test_an_agent_trace_the_api_did_not_link_is_reported_with_its_attempt(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path
    ) -> None:
        # N3: the API grades the answer but links no trace when the id names none in the organization.
        _exec_api(api, self.FINAL)
        reply = {**submitted(A[0], total=4), "trace_id_unlinked": UNLINKED}
        api.routes[("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{A[0]}/submit")] = [ok(reply)]
        agent = tmp_path / "agent.py"
        agent.write_text(TRACING_AGENT)
        trace_ids = {**TRACE_IDS, "task-1": FAKE_TRACE}
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "support-agent", "--parallel", "4",
            "--attempt-timeout", "2s", "--", sys.executable, str(agent),
            env={**KEY_ENV, "TRACE_IDS": json.dumps(trace_ids)},
        )  # fmt: skip
        assert result.exit_code == 0, result.stderr
        assert _submits(api)[A[0]]["trace_id"] == FAKE_TRACE
        task_1 = next(ln for ln in result.stderr.splitlines() if "task-1: answered (" in ln)
        assert task_1.endswith(f"; no trace linked: {UNLINKED}"), task_1
        assert f"trace {TRACE_IDS['task-2']}" in result.stderr

    def test_the_agents_traces_carry_the_runs_label_before_the_tags_it_was_given(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path
    ) -> None:
        _exec_api(api, self.FINAL)
        agent = tmp_path / "agent.py"
        agent.write_text(TAGGING_AGENT)
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "support-agent",
            "--label", "release 1.2, hotfix", "--parallel", "4", "--attempt-timeout", "30s",
            "--", sys.executable, str(agent),
            env={**KEY_ENV, "SEEN_DIR": str(tmp_path), "LAYERLENS_TRACE_TAGS": "ci"},
        )  # fmt: skip
        assert result.exit_code == 0, result.stderr

        # The label first (a comma and all: one tag), the variable's own tags, then the agent's.
        for i in range(1, 5):
            tags = json.loads((tmp_path / f"task-{i}.tags.json").read_text())
            assert tags == ["release 1.2, hotfix", "ci", "support-agent"], i
        # LayerLens's own attempt traces are tagged by the API; exec sends the label with the start.
        assert body_of(api.calls("POST", f"/{ENV_ID}/external-runs")[0])["label"] == "release 1.2, hotfix"

    def test_a_label_holding_the_key_does_not_carry_it_to_the_agent(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path
    ) -> None:
        _exec_api(api, self.FINAL)
        agent = tmp_path / "agent.py"
        agent.write_text(AGENT)
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--label", "test-key",
            "--tasks", "task-1", "--attempt-timeout", "5s", "--", sys.executable, str(agent),
            env={**KEY_ENV, "SEEN_DIR": str(tmp_path)},
        )  # fmt: skip
        assert result.exit_code == 0, result.stderr
        seen = json.loads((tmp_path / "task-1.json").read_text())
        assert seen["copies"] == []

    def test_the_agent_gets_an_exit_budget_unless_the_step_sets_one(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path, monkeypatch: Any
    ) -> None:
        # BUG-01: an agent's SDK waited 5 s at exit, so its trace could be lost before the attempt
        # ended. exec gives it 30 s, or half the attempt's time when that is shorter, unless the
        # step sets LAYERLENS_EXIT_TIMEOUT itself.
        monkeypatch.delenv("LAYERLENS_EXIT_TIMEOUT", raising=False)
        _exec_api(api, self.FINAL)
        agent = tmp_path / "agent.py"
        agent.write_text(EXIT_BUDGET_AGENT)
        seen = {}
        for attempt_timeout, step_sets in (("10m", None), ("30s", None), ("5s", None), ("30s", "120")):
            env = {**KEY_ENV, "SEEN_DIR": str(tmp_path)}
            if step_sets is not None:
                env["LAYERLENS_EXIT_TIMEOUT"] = step_sets
            result = invoke(
                runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--tasks", "task-1",
                "--attempt-timeout", attempt_timeout, "--", sys.executable, str(agent), env=env,
            )  # fmt: skip
            assert result.exit_code == 0, result.stderr
            seen[(attempt_timeout, step_sets)] = (tmp_path / "task-1.exit").read_text()
        assert seen == {("10m", None): "30", ("30s", None): "15", ("5s", None): "2.5", ("30s", "120"): "120"}

    def test_a_command_that_cannot_start_cancels_the_run_and_exits_1(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path
    ) -> None:
        _exec_api(api, self.FINAL)
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--",
            str(tmp_path / "no-such-agent"),
        )  # fmt: skip
        assert result.exit_code == 1
        assert "could not start" in result.stderr
        assert api.requests[-1].url.path.endswith("/cancel")
        assert _submits(api) == {}

    def test_a_submit_the_api_refuses_cancels_the_run_and_exits_1(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path
    ) -> None:
        _exec_api(api, self.FINAL)
        api.routes[("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{A[0]}/submit")] = [
            err(409, "this run closed 24h0m0s after it opened")
        ]
        agent = tmp_path / "agent.py"
        agent.write_text(AGENT)
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--tasks", "task-1",
            "--attempt-timeout", "2s", "--", sys.executable, str(agent),
            env={**KEY_ENV, "SEEN_DIR": str(tmp_path)},
        )  # fmt: skip
        assert result.exit_code == 1
        assert "closed 24h0m0s" in result.stderr
        assert api.requests[-1].url.path.endswith("/cancel")
        assert body_of(api.calls("POST", f"/{ENV_ID}/external-runs")[0])["task_names"] == ["task-1"]


class TestModel:
    def test_an_endpoint_key_comes_from_the_named_variable(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/rollout-runs", ok(run_view(row(A[0], None), kind="rollout", status="pending"), 202))
        result = invoke(
            runner, "env", "run", "model", ENV_ID, "--set", "ci", "--endpoint", "https://agents.example.com/v1",
            "--endpoint-model", "m", "--endpoint-key-env", "AGENT_KEY", "--budget", "5", "--no-wait",
            env={**KEY_ENV, "AGENT_KEY": "secret-value"},
        )  # fmt: skip
        assert result.exit_code == 0
        assert body_of(api.requests[0])["endpoint"] == {
            "base_url": "https://agents.example.com/v1",
            "model": "m",
            "api_key": "secret-value",
        }
        assert "secret-value" not in result.stdout + result.stderr

    def test_model_and_endpoint_together_is_a_usage_error(self, runner: CliRunner) -> None:
        result = invoke(
            runner,
            "env",
            "run",
            "model",
            ENV_ID,
            "--set",
            "ci",
            "--model",
            "m",
            "--endpoint",
            "https://e",
            "--budget",
            "1",
        )
        assert result.exit_code == 2

    def test_waits_and_gates(self, runner: CliRunner, api: FakeAPI) -> None:
        pending = run_view(row(A[0], None), kind="rollout", status="pending")
        done = run_view(row(A[0], "pass"), kind="rollout")
        api.on("POST", f"/{ENV_ID}/rollout-runs", ok(pending, 202))
        api.on("GET", f"/{ENV_ID}/runs/{RUN_ID}", ok(pending), ok(done))
        result = invoke(runner, "env", "run", "model", ENV_ID, "--set", "ci", "--model", "m", "--budget", "1",
                        "--min-pass-rate", "1")  # fmt: skip
        assert result.exit_code == 0
        assert "1/1 passed" in result.stdout


class TestSubmitCommand:
    def test_an_answer_and_a_timeout_go_alone(self, runner: CliRunner) -> None:
        result = invoke(
            runner, "env", "run", "submit", ENV_ID, RUN_ID, "--attempt", A[0], "--answer", "x", "--timed-out"
        )
        assert result.exit_code == 2

    def test_a_timeout_is_submitted_as_one(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{A[0]}/submit", ok(submitted(A[0])))
        result = invoke(runner, "env", "run", "submit", ENV_ID, RUN_ID, "--attempt", A[0], "--timed-out")
        assert result.exit_code == 0
        assert body_of(api.requests[0]) == {"outcome": "timeout"}

    def test_a_trace_id_the_api_did_not_link_is_said_so(self, runner: CliRunner, api: FakeAPI) -> None:
        # N3: the API graded the answer but linked no trace, as the id names none; submit says so.
        reply = {**submitted(A[0]), "trace_id_unlinked": UNLINKED}
        api.on("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{A[0]}/submit", ok(reply))
        args = ["env", "run", "submit", ENV_ID, RUN_ID, "--attempt", A[0], "--answer", "x", "--trace-id", FAKE_TRACE]
        result = invoke(runner, *args)
        assert result.exit_code == 0
        assert result.stdout.startswith(f"Graded attempt {A[0]}") and f"Warning: {UNLINKED}" in result.stderr
        as_json = invoke(runner, "--format", "json", *args)
        assert json.loads(as_json.stdout)["trace_id_unlinked"] == UNLINKED
        assert f"Warning: {UNLINKED}" in as_json.stderr


def test_every_env_command_has_a_help_page(runner: CliRunner) -> None:
    commands: List[List[str]] = [["env"], ["env", "tasks"], ["env", "run"]]
    from layerlens.cli.commands.env import env

    for name, command in env.commands.items():
        commands.append(["env", name])
        for sub in getattr(command, "commands", {}):
            commands.append(["env", name, sub])
    for args in commands:
        result = runner.invoke(cli, [*args, "--help"])
        assert result.exit_code == 0, (args, result.output)


def _timed_out(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def _interrupted(request: httpx.Request) -> httpx.Response:
    raise KeyboardInterrupt


class TestTheKey:
    @pytest.mark.parametrize("where", ["before", "after"])
    def test_exec_refuses_a_key_on_the_command_line(self, runner: CliRunner, api: FakeAPI, where: str) -> None:
        exec_args = ["env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a"]
        args = ["--api-key", "llk_x", *exec_args] if where == "before" else [*exec_args, "--api-key", "llk_x"]
        result = invoke(runner, *args, "--", "true", env={})
        assert result.exit_code == 2
        assert "LAYERLENS_API_KEY" in result.stderr and "--api-key-file" in result.stderr
        assert api.requests == []

    def test_the_key_can_come_from_standard_input(self, runner: CliRunner, api: FakeAPI) -> None:
        keys: List[str] = []

        def build(ctx: Any, key: str, use_bearer_auth: bool) -> Any:  # noqa: ARG001
            keys.append(key)
            return make_client(api)

        api.on("GET", "", ok({"environments": [], "count": 0, "total_count": 0}))
        with patch("layerlens.cli.commands.env._common.build_client", build):
            result = runner.invoke(
                cli,
                ["-q", "--api-key-file", "-", "env", "list"],
                input="llk_from_stdin\n",
                env={"LAYERLENS_API_KEY": "e"},
            )
        assert result.exit_code == 0, result.stderr
        assert keys == ["llk_from_stdin"]

    def test_the_key_can_come_from_a_file_named_after_the_command(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path
    ) -> None:
        keys: List[str] = []

        def build(ctx: Any, key: str, use_bearer_auth: bool) -> Any:  # noqa: ARG001
            keys.append(key)
            return make_client(api)

        key_file = tmp_path / "key"
        key_file.write_text("llk_from_file\n")
        api.on("GET", "", ok({"environments": [], "count": 0, "total_count": 0}))
        with patch("layerlens.cli.commands.env._common.build_client", build):
            result = invoke(runner, "env", "list", "--api-key-file", str(key_file), env={})
        assert result.exit_code == 0, result.stderr
        assert keys == ["llk_from_file"]

    def test_a_key_and_a_key_file_together_are_a_usage_error(self, runner: CliRunner, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("llk_from_file\n")
        result = invoke(runner, "--api-key", "llk_x", "env", "list", "--api-key-file", str(key_file), env={})
        assert result.exit_code == 2 and "not both" in result.stderr

    def test_an_empty_key_file_is_a_usage_error(self, runner: CliRunner, tmp_path: Path) -> None:
        key_file = tmp_path / "key"
        key_file.write_text("\n")
        result = invoke(runner, "env", "list", "--api-key-file", str(key_file), env={})
        assert result.exit_code == 2 and "holds no key" in result.stderr

    def test_a_key_the_api_refuses_reads_as_the_apis_sentence(self, runner: CliRunner) -> None:
        request = httpx.Request("GET", "https://api.test/api/v1/organizations")
        refused = AuthenticationError(
            "Error code: 401 - {'status': 'error', 'error': 'Unknown API key'}",
            response=httpx.Response(401, request=request),
            body="Unknown API key",
        )
        with patch("layerlens.cli.commands.env._common.build_client", real_build_client), patch.object(
            LayerLens, "_get_organization", side_effect=refused
        ):
            result = invoke(runner, "env", "list")
        assert result.exit_code == 1
        assert result.stderr.strip() == "Error: Unknown API key (HTTP 401)"

    def test_an_empty_key_file_path_is_a_usage_error_not_a_fallback(self, runner: CliRunner) -> None:
        result = invoke(runner, "env", "list", "--api-key-file", "", env={"LAYERLENS_API_KEY": "llk_other"})
        assert result.exit_code == 2 and "needs a path" in result.stderr


# Every attempt submitted as the agent's own error is no platform error, so the run reads `success`.
ALL_ERRORED = run_view(*[row(a, "error", error_source="agent", evidence="the agent exited with status 2") for a in A])


class TestBrokenPipeline:
    @pytest.mark.parametrize("gate", [[], ["--min-pass-rate", "0.5"]])
    def test_a_run_in_which_no_attempt_answered_exits_11_saying_why(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path, gate: List[str]
    ) -> None:
        _exec_api(api, ALL_ERRORED)
        junit = tmp_path / "results.xml"
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--parallel", "4", *gate,
            "--junit", str(junit), "--", sys.executable, str(tmp_path / "ci" / "agnet.py"),
        )  # fmt: skip
        assert result.exit_code == 11, result.stderr
        assert "no attempt gave an answer" in result.stderr and "all 4 attempts ended in an error" in result.stderr
        assert "The first, task 'task-1', failed because the agent exited with status 2" in result.stderr
        tail = result.stderr.split("The end of its standard error:\n", 1)[1]
        assert tail.startswith("  ") and "agnet.py" in tail.splitlines()[0]
        assert len(_submits(api)) == 4 and any(r.url.path.endswith("/finish") for r in api.requests)
        assert junit.exists()

    def test_one_answer_is_enough_for_the_gate_to_decide(self, runner: CliRunner, api: FakeAPI) -> None:
        _exec_api(api, run_view(row(A[0], "fail"), *[row(a, "error", error_source="agent") for a in A[1:]]))
        agent = "import os, sys; print('x'); sys.exit(0 if os.environ['LAYERLENS_TASK_NAME'] == 'task-1' else 2)"
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--min-pass-rate", "0.5",
            "--", sys.executable, "-c", agent,
        )  # fmt: skip
        assert result.exit_code == 10, result.stderr
        assert "no attempt gave an answer" not in result.stderr


class TestStartReplyLost:
    def test_exec_sends_the_start_again_under_its_key(self, runner: CliRunner, api: FakeAPI) -> None:
        _exec_api(api, TestExec.FINAL)
        api.routes[("POST", f"/{ENV_ID}/external-runs")].insert(0, _timed_out)
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--",
            sys.executable, "-c", "print('x')",
        )  # fmt: skip
        assert result.exit_code == 0, result.stderr
        starts = api.calls("POST", f"/{ENV_ID}/external-runs")
        assert len(starts) == 2 and starts[0].headers["idempotency-key"] == starts[1].headers["idempotency-key"]

    def test_exec_gives_up_saying_the_run_may_have_started(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/external-runs", _timed_out)
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--",
            sys.executable, "-c", "print('x')",
        )  # fmt: skip
        assert result.exit_code == 1
        assert "could not be started" in result.stderr and "closes on its own within 24 hours" in result.stderr
        starts = api.calls("POST", f"/{ENV_ID}/external-runs")
        assert len(starts) == 3 and len({r.headers["idempotency-key"] for r in starts}) == 1

    def test_a_model_run_that_gives_up_names_the_key_to_get_it_back(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/rollout-runs", _timed_out)
        result = invoke(runner, "env", "run", "model", ENV_ID, "--set", "ci", "--model", "m", "--budget", "1")
        assert result.exit_code == 1
        key = api.calls("POST", f"/{ENV_ID}/rollout-runs")[0].headers["idempotency-key"]
        assert f"--idempotency-key {key}" in result.stderr and "whether the run started is not known" in result.stderr

    @pytest.mark.parametrize(
        "args, path, reply",
        [
            (["run", "model", ENV_ID, "--set", "ci", "--model", "m", "--budget", "1", "--no-wait"], "/rollout-runs",
             ok(run_view(row(A[0], None), kind="rollout", status="pending"), 202)),
            (["run", "start", ENV_ID, "--set", "ci", "--agent-name", "a"], "/external-runs",
             ok({"run_id": RUN_ID, "attempts": []}, 201)),
            (["tasks", "generate", ENV_ID, "--set", "g", "--count", "1", "--budget", "1"], "/task-sets/g/generate",
             ok({"id": "j1", "kind": "generate_tasks", "status": "pending", "created_at": "2026-10-26T12:00:00Z"}, 202)),
        ],
    )  # fmt: skip
    def test_a_ci_rerun_can_reuse_a_key(
        self, runner: CliRunner, api: FakeAPI, args: List[str], path: str, reply: Any
    ) -> None:
        api.on("POST", f"/{ENV_ID}{path}", reply)
        result = invoke(runner, "env", *args, "--idempotency-key", "ci-4711")
        assert result.exit_code == 0, result.stderr
        assert api.calls("POST", f"/{ENV_ID}{path}")[0].headers["idempotency-key"] == "ci-4711"


class TestTimeoutsAndInterrupts:
    PENDING = run_view(row(A[0], None), kind="rollout", status="in_progress")
    CANCEL = f"/{ENV_ID}/runs/{RUN_ID}/cancel"

    def test_a_model_run_still_going_at_its_timeout_is_cancelled(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/rollout-runs", ok(self.PENDING, 202))
        api.on("GET", f"/{ENV_ID}/runs/{RUN_ID}", ok(self.PENDING))
        api.on("POST", self.CANCEL, ok({**self.PENDING, "status": "cancelled"}))
        result = invoke(
            runner, "env", "run", "model", ENV_ID, "--set", "ci", "--model", "m", "--budget", "1", "--timeout", "0.01"
        )
        assert result.exit_code == 11
        assert len(api.calls("POST", self.CANCEL)) == 1
        assert f"run {RUN_ID} is still in_progress after 0.01s, so it was cancelled" in result.stderr

    @pytest.mark.parametrize("flag, cancels", [([], 0), (["--cancel-on-timeout"], 1)])
    def test_wait_cancels_at_its_timeout_only_when_asked(
        self, runner: CliRunner, api: FakeAPI, flag: List[str], cancels: int
    ) -> None:
        api.on("GET", f"/{ENV_ID}/runs/{RUN_ID}", ok(self.PENDING))
        api.on("POST", self.CANCEL, ok({**self.PENDING, "status": "cancelled"}))
        result = invoke(runner, "env", "run", "wait", ENV_ID, RUN_ID, "--timeout", "0.01", *flag)
        assert result.exit_code == 11
        assert len(api.calls("POST", self.CANCEL)) == cancels

    def test_a_model_run_whose_polls_are_throttled_until_its_timeout_is_cancelled(
        self, runner: CliRunner, api: FakeAPI
    ) -> None:
        # LAY-4429: the wait raised the 429 at its deadline, so this exited 1 and left the run going.
        api.on("POST", f"/{ENV_ID}/rollout-runs", ok(self.PENDING, 202))
        api.on("GET", f"/{ENV_ID}/runs/{RUN_ID}", err(429, "Too Many Requests", {"Retry-After": "1"}))
        api.on("POST", self.CANCEL, ok({**self.PENDING, "status": "cancelled"}))
        result = invoke(
            runner, "env", "run", "model", ENV_ID, "--set", "ci", "--model", "m", "--budget", "1", "--timeout", "0.01"
        )
        assert result.exit_code == 11, result.stderr
        assert len(api.calls("POST", self.CANCEL)) == 1
        assert f"run {RUN_ID} did not complete within 0.01 seconds; the last poll failed: " in result.stderr
        assert "so it was cancelled" in result.stderr

    @pytest.mark.parametrize("flag, cancels", [([], 0), (["--cancel-on-timeout"], 1)])
    def test_wait_whose_polls_are_throttled_until_its_timeout_exits_11(
        self, runner: CliRunner, api: FakeAPI, flag: List[str], cancels: int
    ) -> None:
        api.on("GET", f"/{ENV_ID}/runs/{RUN_ID}", err(503, "Service Unavailable"))
        api.on("POST", self.CANCEL, ok({**self.PENDING, "status": "cancelled"}))
        result = invoke(runner, "env", "run", "wait", ENV_ID, RUN_ID, "--timeout", "0.01", *flag)
        assert result.exit_code == 11, result.stderr
        assert "the last poll failed" in result.stderr
        assert len(api.calls("POST", self.CANCEL)) == cancels

    def test_ctrl_c_while_waiting_exits_11_and_leaves_the_run_alone(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}/runs/{RUN_ID}", _interrupted)
        result = invoke(runner, "env", "run", "wait", ENV_ID, RUN_ID)
        assert result.exit_code == 11 and "goes on" in result.stderr
        assert api.calls("POST", self.CANCEL) == []

    def test_ctrl_c_while_exec_waits_for_grading_exits_11(self, runner: CliRunner, api: FakeAPI) -> None:
        _exec_api(api, TestExec.FINAL)
        api.routes[("GET", f"/{ENV_ID}/runs/{RUN_ID}")] = [_interrupted]
        api.routes[("POST", self.CANCEL)] = [err(409, "this run has finished")]
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--",
            sys.executable, "-c", "print('x')",
        )  # fmt: skip
        assert result.exit_code == 11
        assert f"interrupted, and run {RUN_ID} had finished by then" in result.stderr

    @posix_only
    @pytest.mark.parametrize(
        "cut_short", [False, True], ids=["leaving-the-block-cancelled-it", "a-second-ctrl-c-cut-it"]
    )
    def test_ctrl_c_while_exec_runs_its_attempts_says_the_run_was_cancelled(
        self, runner: CliRunner, api: FakeAPI, cut_short: bool
    ) -> None:
        # Leaving `with external` cancels the run, and exec reports what that cancel did. Only
        # when a second Ctrl-C cut that cancel short does exec cancel again, and its cancel is
        # the one that lands (BUG-09: it used to cancel twice on every interrupt).
        cancelled = ok({**TestExec.FINAL, "status": "cancelled"})

        def interrupting(reply: Any) -> Any:
            def handler(request: httpx.Request) -> Any:  # noqa: ARG001
                os.kill(os.getpid(), signal.SIGINT)
                return reply

            return handler

        _exec_api(api, TestExec.FINAL)
        api.routes[("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{A[0]}/submit")] = [
            interrupting(ok(submitted(A[0], total=4)))
        ]
        api.routes[("POST", self.CANCEL)] = [interrupting(cancelled), cancelled] if cut_short else [cancelled]
        result = runner.invoke(
            cli, ["-q", "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--",
                  sys.executable, "-c", "print('x')"], env=KEY_ENV,
        )  # fmt: skip
        assert result.exit_code == 11, result.stderr
        assert f"interrupted, so run {RUN_ID} was cancelled" in result.stderr
        assert len(api.calls("POST", self.CANCEL)) == (2 if cut_short else 1)

    @posix_only
    def test_ctrl_c_lets_an_answer_already_sent_be_graded_before_the_run_is_cancelled(
        self, runner: CliRunner, api: FakeAPI
    ) -> None:
        # LAY-4454: a worker checked for the interrupt without the lock, then submitted, so
        # the cancel and an answer still being graded reached the API in either order.
        seen: List[str] = []

        def graded_after_the_interrupt(request: httpx.Request) -> Any:  # noqa: ARG001
            # To the main thread itself: a process-wide signal may land on this worker thread,
            # and reach the main thread only at its next poll, after this submit has ended.
            signal.pthread_kill(threading.main_thread().ident or 0, signal.SIGINT)
            threading.Event().wait(1.0)  # the judge grading it (time.sleep is patched out here)
            seen.append("graded")
            return ok(submitted(A[0], total=4))

        def cancel(request: httpx.Request) -> Any:  # noqa: ARG001
            seen.append("cancel")
            return ok({**TestExec.FINAL, "status": "cancelled"})

        _exec_api(api, TestExec.FINAL)
        api.routes[("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{A[0]}/submit")] = [graded_after_the_interrupt]
        api.routes[("POST", self.CANCEL)] = [cancel]
        result = runner.invoke(
            cli, ["-q", "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--",
                  sys.executable, "-c", "print('x')"], env=KEY_ENV,
        )  # fmt: skip
        assert result.exit_code == 11, result.stderr
        assert seen == ["graded", "cancel"]
        assert "1 answer is being submitted; waiting up to 5s before cancelling the run" in result.stderr
        assert f"interrupted, so run {RUN_ID} was cancelled" in result.stderr
        # The interrupt came while task-1 was submitting: no later attempt submitted anything.
        assert [r.url.path.split("/")[-2] for r in api.requests if r.url.path.endswith("/submit")] == [A[0]]

    @posix_only
    def test_ctrl_c_after_the_run_finished_cancels_once_and_says_so_once(self, runner: CliRunner, api: FakeAPI) -> None:
        # BUG-09: the answers in flight were graded, so the run had finished when it was
        # cancelled. Two cancels went out, and stderr showed the SDK's raw 409 before exec's line.
        def graded_after_the_interrupt(request: httpx.Request) -> Any:  # noqa: ARG001
            signal.pthread_kill(threading.main_thread().ident or 0, signal.SIGINT)
            threading.Event().wait(0.5)
            return ok(submitted(A[0], total=4))

        _exec_api(api, TestExec.FINAL)
        api.routes[("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{A[0]}/submit")] = [graded_after_the_interrupt]
        api.routes[("POST", self.CANCEL)] = [
            err(409, f"run {RUN_ID} has already finished, so there is nothing to cancel")
        ]
        result = runner.invoke(
            cli, ["-q", "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--",
                  sys.executable, "-c", "print('x')"], env=KEY_ENV,
        )  # fmt: skip
        assert result.exit_code == 11, result.stderr
        assert len(api.calls("POST", self.CANCEL)) == 1
        assert f"interrupted, and run {RUN_ID} had finished by then" in result.stderr
        assert "could not cancel run" not in result.stderr and "Error code:" not in result.stderr

    @pytest.mark.parametrize("rate", ["nan", "inf", "-0.1", "1.5"])
    def test_a_pass_rate_that_is_not_one_is_a_usage_error(self, runner: CliRunner, api: FakeAPI, rate: str) -> None:
        result = invoke(runner, "env", "run", "wait", ENV_ID, RUN_ID, "--min-pass-rate", rate)
        assert result.exit_code == 2 and api.requests == []


def test_a_submit_refused_mid_run_names_the_task(runner: CliRunner, api: FakeAPI, tmp_path: Path) -> None:
    _exec_api(api, TestExec.FINAL)
    api.routes[("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{A[0]}/submit")] = [err(404, "task not found")]
    result = invoke(
        runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--tasks", "task-1", "--",
        sys.executable, "-c", "print('x')",
    )  # fmt: skip
    assert result.exit_code == 1
    assert "task 'task-1' (6a8f2c1d9e0b4a7f3c5d6e11) was refused: task not found (HTTP 404)" in result.stderr
    assert api.requests[-1].url.path.endswith("/cancel")


def test_a_tasks_file_over_the_apis_body_limit_is_refused_before_sending(
    runner: CliRunner, api: FakeAPI, tmp_path: Path
) -> None:
    tasks = tmp_path / "tasks.jsonl"
    line = json.dumps({"name": "t", "prompt": "x" * 20_000, "grader": {"kind": "answer"}})
    tasks.write_text("\n".join([line] * 300) + "\n")
    result = invoke(runner, "env", "run", "start", ENV_ID, "--tasks-file", str(tasks), "--agent-name", "a")
    assert result.exit_code == 2 and "the API takes at most 5.00 MiB (5242880 bytes)" in result.stderr
    assert api.requests == []


class TestRound2:
    PENDING = run_view(row(A[0], None), kind="rollout", status="in_progress")
    CANCEL = f"/{ENV_ID}/runs/{RUN_ID}/cancel"
    MODEL = ("env", "run", "model", ENV_ID, "--set", "ci", "--model", "m", "--budget", "1")

    def test_a_run_got_back_by_its_key_is_cancelled_at_timeout_as_this_commands_own(
        self, runner: CliRunner, api: FakeAPI
    ) -> None:
        api.on("POST", f"/{ENV_ID}/rollout-runs", ok(self.PENDING, 202))
        api.on("GET", f"/{ENV_ID}/runs/{RUN_ID}", ok(self.PENDING))
        api.on("POST", self.CANCEL, ok({**self.PENDING, "status": "cancelled"}))
        result = invoke(runner, *self.MODEL, "--timeout", "0.01", "--idempotency-key", "run-7f3a")
        assert result.exit_code == 11 and len(api.calls("POST", self.CANCEL)) == 1

    @pytest.mark.parametrize("verdicts", [["timeout"] * 4, ["error", "error", "timeout", "error"]])
    def test_an_agent_that_ran_out_of_time_is_gated_not_called_broken(
        self, runner: CliRunner, api: FakeAPI, verdicts: List[str]
    ) -> None:
        _exec_api(api, run_view(*[row(a, v, error_source="agent") for a, v in zip(A, verdicts)]))
        failing = [f"task-{i}" for i, verdict in enumerate(verdicts, start=1) if verdict == "error"]
        agent = (
            f"import os, sys, time\nif os.environ['LAYERLENS_TASK_NAME'] in {failing!r}: sys.exit(2)\ntime.sleep(30)\n"
        )
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--parallel", "4",
            "--attempt-timeout", "1s", "--min-pass-rate", "0.5", "--", sys.executable, "-c", agent,
        )  # fmt: skip
        assert result.exit_code == 10, result.stderr
        assert "no attempt gave an answer" not in result.stderr

    @pytest.mark.parametrize(
        "args, path, status",
        [
            (MODEL, "/rollout-runs", 504),
            (("env", "run", "start", ENV_ID, "--set", "ci", "--agent-name", "a"), "/external-runs", 502),
            (("env", "tasks", "generate", ENV_ID, "--set", "g", "--count", "1", "--budget", "1"),
             "/task-sets/g/generate", 500),
        ],
    )  # fmt: skip
    def test_a_start_that_ends_in_a_server_error_names_its_key(
        self, runner: CliRunner, api: FakeAPI, args: Any, path: str, status: int
    ) -> None:
        api.on("POST", f"/{ENV_ID}{path}", (status, {"message": "Endpoint request timed out"}))
        result = invoke(runner, *args)
        assert result.exit_code == 1
        key = api.calls("POST", f"/{ENV_ID}{path}")[0].headers["idempotency-key"]
        assert f"--idempotency-key {key}" in result.stderr and f"(HTTP {status})" in result.stderr

    def test_a_run_that_finished_as_the_timeout_hit_is_gated_as_it_ended(self, runner: CliRunner, api: FakeAPI) -> None:
        done = run_view(row(A[0], "pass"), kind="rollout")
        api.on("POST", f"/{ENV_ID}/rollout-runs", ok(self.PENDING, 202))
        api.on(
            "GET", f"/{ENV_ID}/runs/{RUN_ID}", lambda r: ok(done if api.calls("POST", self.CANCEL) else self.PENDING)
        )
        api.on("POST", self.CANCEL, err(409, f"run {RUN_ID} has already finished, so there is nothing to cancel"))
        result = invoke(runner, *self.MODEL, "--timeout", "0.01", "--min-pass-rate", "1")
        assert result.exit_code == 0, result.stderr
        assert "1/1 passed" in result.stdout and "gated as it ended" in result.stderr

    def test_ctrl_c_while_a_run_starts_names_the_key_to_get_it_back(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/rollout-runs", _interrupted)
        result = invoke(runner, *self.MODEL, "--idempotency-key", "run-7f3a")
        assert result.exit_code == 11 and "--idempotency-key run-7f3a" in result.stderr

    @posix_only
    def test_sigterm_while_a_run_starts_is_an_interrupt(self, runner: CliRunner, api: FakeAPI) -> None:
        class Escaped(Exception):
            pass

        def escape(signum: int, frame: Any) -> None:  # noqa: ARG001
            raise Escaped

        def terminated(request: httpx.Request) -> Any:
            os.kill(os.getpid(), signal.SIGTERM)
            return ok(self.PENDING, 202)

        api.on("POST", f"/{ENV_ID}/rollout-runs", terminated)
        previous = signal.signal(signal.SIGTERM, escape)
        try:
            result = runner.invoke(cli, ["-q", *self.MODEL, "--no-wait"], env=KEY_ENV)
        finally:
            signal.signal(signal.SIGTERM, previous)
        assert result.exit_code == 11 and "interrupted while starting the run" in result.stderr

    @posix_only
    def test_a_second_interrupt_does_not_cut_the_cancel_short(self, runner: CliRunner, api: FakeAPI) -> None:
        def cancel_interrupted_again(request: httpx.Request) -> Any:
            os.kill(os.getpid(), signal.SIGINT)
            return ok({**self.PENDING, "status": "cancelled"})

        api.on("POST", f"/{ENV_ID}/rollout-runs", ok(self.PENDING, 202))
        api.on("GET", f"/{ENV_ID}/runs/{RUN_ID}", _interrupted)
        api.on("POST", self.CANCEL, cancel_interrupted_again)
        result = runner.invoke(cli, ["-q", *self.MODEL], env=KEY_ENV)
        assert result.exit_code == 11 and f"interrupted, so run {RUN_ID} was cancelled" in result.stderr

    def test_exec_refuses_windows_before_calling_the_api(
        self, runner: CliRunner, api: FakeAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        result = invoke(runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--", "true")
        assert result.exit_code == 2 and "env run exec does not run on Windows" in result.stderr
        assert "client.environments.external_run" in result.stderr
        assert api.requests == []

    def test_exec_without_a_key_does_not_suggest_api_key(self, runner: CliRunner) -> None:
        result = invoke(runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--", "true", env={})
        assert result.exit_code == 1
        assert result.stderr.strip().endswith("set LAYERLENS_API_KEY, or pass --api-key-file PATH.")

    def test_a_cancel_that_failed_is_not_reported_as_done(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path
    ) -> None:
        _exec_api(api, TestExec.FINAL)
        api.routes[("POST", self.CANCEL)] = [err(403, "this key cannot cancel runs")]
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--",
            str(tmp_path / "no-such-agent"),
        )  # fmt: skip
        assert result.exit_code == 1
        assert "the run could not be cancelled" in result.stderr and "the run was cancelled" not in result.stderr

    def test_a_command_that_cannot_start_after_the_run_finished_does_not_say_it_was_cancelled(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path
    ) -> None:
        # Leaving the block found the run finished (409): there was nothing to cancel (CodeRabbit on #99).
        _exec_api(api, TestExec.FINAL)
        api.routes[("POST", self.CANCEL)] = [
            err(409, f"run {RUN_ID} has already finished, so there is nothing to cancel")
        ]
        result = invoke(
            runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--",
            str(tmp_path / "no-such-agent"),
        )  # fmt: skip
        assert result.exit_code == 1
        assert "the run had already finished" in result.stderr and "the run was cancelled" not in result.stderr

    def test_a_name_that_cannot_be_looked_up_is_not_a_lost_start(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on("GET", "", _timed_out)
        result = invoke(runner, "env", "run", "start", "support-desk", "--set", "ci", "--agent-name", "a")
        assert result.exit_code == 1 and "whether the run started" not in result.stderr
        assert api.calls("POST", f"/{ENV_ID}/external-runs") == []

    @pytest.mark.parametrize(
        "args",
        [("env", "delete", ENV_ID), ("env", "tasks", "delete", ENV_ID, "--set", "ci")],
    )
    def test_a_key_on_standard_input_needs_yes_to_delete(self, runner: CliRunner, api: FakeAPI, args: Any) -> None:
        result = runner.invoke(cli, ["-q", "--api-key-file", "-", *args], input="llk_from_stdin\n", env={})
        assert result.exit_code == 2 and "pass --yes" in result.stderr
        assert api.requests == []

    def test_exec_whose_start_stays_busy_says_to_run_the_job_again(self, runner: CliRunner, api: FakeAPI) -> None:
        busy = (503, {"message": "too many jobs are being started; try again with the same Idempotency-Key"},
                {"Retry-After": "1"})  # fmt: skip
        api.on("POST", f"/{ENV_ID}/external-runs", busy)
        result = invoke(runner, "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--", "true")
        assert result.exit_code == 1 and "run the job again" in result.stderr
