"""``runs.list`` / ``runs.iter``, ``RunComparison`` and ``layerlens env run list|compare`` (LAY-4432)."""

from __future__ import annotations

import re
import json
import asyncio
from typing import Any, Dict, List, Iterator, Optional
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from layerlens import LayerLens, AsyncLayerLens
from layerlens.models import RunPage, RunResult, RunSummary, RunComparison
from layerlens.cli._app import cli
from layerlens.models._run_reports import tally
from layerlens.resources.environments._shared import InvalidArgument

from .conftest import ENV_ID, RUN_ID, FakeAPI, ok, err, row, run_view, environment, make_client

KEY_ENV = {"LAYERLENS_API_KEY": "test-key", "LAYERLENS_STRATIX_API_KEY": "legacy-key"}
BASELINE = "6a91c0e5f1d2a3b4c5d6e7a1"
CANDIDATE = "6a91c0e5f1d2a3b4c5d6e7c1"
OLDER = "6a91c0e5f1d2a3b4c5d6e7a0"
T1, T2, T3, T4 = ("6a91c0e5f1d2a3b4c5d6e80" + str(i) for i in range(1, 5))


def summary(run_id: str, **overrides: Any) -> Dict[str, Any]:
    view = run_view(row("a1", "pass"), run_id=run_id, **overrides)
    view.pop("attempts")
    view.setdefault("created_at", "2026-11-05T16:20:00.123Z")
    view.setdefault("started_at", "2026-11-05T16:20:01Z")
    view.setdefault("finished_at", "2026-11-05T16:24:00Z")
    return view


def task(task_id: str, verdict: Optional[str], **overrides: Any) -> Dict[str, Any]:
    return row(task_id, verdict, **{"task_id": task_id, "task_name": f"task {task_id[-1]}", **overrides})


def a_run(run_id: str, *rows: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    return run_view(*rows, run_id=run_id, created_at="2026-11-05T16:20:00Z", **overrides)


def parsed(view: Dict[str, Any]) -> RunResult:
    return RunResult.model_validate(view)


class TestList:
    def test_it_sends_the_filters_and_reads_a_page(self, client: LayerLens, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}/runs", ok({"runs": [summary(CANDIDATE), summary(BASELINE)], "next_cursor": "djEuMQ"}))

        page = client.environments.runs.list(
            ENV_ID, label=" main ", agent_name="support-agent", kind="external", status="success", limit=2,
            cursor="djEuMA",
        )  # fmt: skip

        assert isinstance(page, RunPage)
        assert [r.run_id for r in page.runs] == [CANDIDATE, BASELINE]
        assert page.next_cursor == "djEuMQ"
        first = page.runs[0]
        assert isinstance(first, RunSummary) and not isinstance(first, RunResult)
        assert first.created_at is not None and first.created_at.year == 2026
        assert first.finished and first.out_of == 1
        sent = api.calls("GET", f"/{ENV_ID}/runs")[0].url.params
        assert dict(sent) == {
            "label": "main",
            "agent_name": "support-agent",
            "kind": "external",
            "status": "success",
            "limit": "2",
            "cursor": "djEuMA",
        }

    def test_it_sends_no_filter_it_was_not_given(self, client: LayerLens, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}/runs", ok({"runs": [], "next_cursor": None}))

        page = client.environments.runs.list(ENV_ID)

        assert page.runs == [] and page.next_cursor is None
        assert dict(api.calls("GET", f"/{ENV_ID}/runs")[0].url.params) == {}

    @pytest.mark.parametrize("field", ["label", "agent_name"])
    def test_a_blank_name_is_refused_before_anything_is_sent(self, client: LayerLens, api: FakeAPI, field: str) -> None:
        with pytest.raises(InvalidArgument, match=f"{field} is blank"):
            client.environments.runs.list(ENV_ID, **{field: "  "})
        assert api.requests == []

    def test_iter_reads_every_page_and_resolves_the_name_once(self, client: LayerLens, api: FakeAPI) -> None:
        api.on("GET", "", ok({"environments": [environment()], "total_count": 1}))
        pages = iter(
            [
                {"runs": [summary(CANDIDATE)], "next_cursor": "c1"},
                {"runs": [], "next_cursor": "c2"},
                {"runs": [summary(BASELINE), summary(OLDER)], "next_cursor": None},
            ]
        )
        api.on("GET", f"/{ENV_ID}/runs", lambda request: ok(next(pages)))

        found = [r.run_id for r in client.environments.runs.iter("support-desk", label="main", page_size=2)]

        assert found == [CANDIDATE, BASELINE, OLDER], "an empty page with a cursor is not the end"
        sent = [dict(r.url.params) for r in api.calls("GET", f"/{ENV_ID}/runs")]
        assert sent == [
            {"label": "main", "limit": "2"},
            {"label": "main", "limit": "2", "cursor": "c1"},
            {"label": "main", "limit": "2", "cursor": "c2"},
        ]
        assert len(api.calls("GET", "")) == 1

    def test_async_list_and_iter(self, async_client: AsyncLayerLens, api: FakeAPI) -> None:
        pages = iter(
            [{"runs": [summary(CANDIDATE)], "next_cursor": "c1"}, {"runs": [summary(BASELINE)], "next_cursor": None}]
        )
        api.on("GET", f"/{ENV_ID}/runs", lambda request: ok(next(pages)))

        async def main() -> None:
            page = await async_client.environments.runs.list(ENV_ID, status="success", limit=1)
            rest = [r.run_id async for r in async_client.environments.runs.iter(ENV_ID, cursor=page.next_cursor)]
            assert [r.run_id for r in page.runs] == [CANDIDATE]
            assert rest == [BASELINE]
            with pytest.raises(InvalidArgument):
                await async_client.environments.runs.list(ENV_ID, label="")

        asyncio.run(main())
        assert dict(api.calls("GET", f"/{ENV_ID}/runs")[1].url.params) == {"cursor": "c1"}

    def test_a_run_result_is_a_summary_with_its_tasks(self) -> None:
        run = parsed(a_run(RUN_ID, task(T1, "pass")))
        assert isinstance(run, RunSummary)
        assert run.attempts[0].task_id == T1
        assert parsed(run_view(row("a1", "pass"))).created_at is None, "from a server that predates it"


class TestComparison:
    def test_a_task_that_passed_and_now_fails_is_a_regression(self) -> None:
        baseline = parsed(a_run(BASELINE, task(T1, "pass"), task(T2, "pass"), task(T3, "fail"), task(T4, "pass")))
        candidate = parsed(
            a_run(
                CANDIDATE,
                task(T1, "pass"),
                task(T2, "fail", evidence="expected 12, the agent answered 11"),
                task(T3, "pass"),
                environment_version=5,
            )
        )

        comparison = RunComparison.between(baseline, candidate)

        assert comparison.regression is True
        assert [c.task_id for c in comparison.regressed] == [T2]
        change = comparison.regressed[0]
        assert (change.baseline.verdict, change.candidate.verdict) == ("pass", "fail")
        assert change.candidate.evidence == "expected 12, the agent answered 11"
        assert [c.task_id for c in comparison.recovered] == [T3]
        assert [a.task_id for a in comparison.only_in_baseline] == [T4], "a task left out is not a regression"
        assert comparison.only_in_candidate == []
        assert comparison.pass_rate_delta == pytest.approx(2 / 3 - 3 / 4)
        assert comparison.environment_versions_differ
        assert comparison.baseline.run_id == BASELINE and not hasattr(comparison.baseline, "attempts")

    @pytest.mark.parametrize(
        ("verdict", "source", "regressed"),
        [
            ("fail", None, True),
            ("error", "agent", True),
            ("error", "platform", True),
            ("timeout", "agent", True),
            ("off_script", None, False),
            ("not_graded", None, False),
            ("pass", None, False),
        ],
    )
    def test_what_counts_against_the_agent_regresses(
        self, verdict: str, source: Optional[str], regressed: bool
    ) -> None:
        baseline = parsed(a_run(BASELINE, task(T1, "pass")))
        candidate = parsed(a_run(CANDIDATE, task(T1, verdict, error_source=source)))

        comparison = RunComparison.between(baseline, candidate)

        assert comparison.regression is regressed
        assert len(comparison.regressed) == int(regressed)

    def test_a_new_failing_task_is_not_a_regression(self) -> None:
        baseline = parsed(a_run(BASELINE, task(T1, "pass")))
        candidate = parsed(a_run(CANDIDATE, task(T1, "pass"), task(T2, "fail")))

        comparison = RunComparison.between(baseline, candidate)

        assert comparison.regression is False
        assert [a.task_id for a in comparison.only_in_candidate] == [T2]
        assert comparison.pass_rate_delta == pytest.approx(-0.5), "the pass rate fell, from a task the baseline lacked"

    def test_a_run_still_going_is_refused(self) -> None:
        done = parsed(a_run(BASELINE, task(T1, "pass")))
        running = parsed(a_run(CANDIDATE, task(T1, None), status="in_progress"))
        with pytest.raises(ValueError, match=f"the candidate run {CANDIDATE} is still in_progress"):
            RunComparison.between(done, running)
        with pytest.raises(ValueError, match="the baseline run"):
            RunComparison.between(running, done)

    def test_no_pass_rate_no_delta(self) -> None:
        baseline = parsed(a_run(BASELINE, task(T1, "off_script")))
        candidate = parsed(a_run(CANDIDATE, task(T1, "pass")))
        assert baseline.pass_rate is None
        assert RunComparison.between(baseline, candidate).pass_rate_delta is None

    def test_the_tally_names_the_first_five_tasks(self) -> None:
        ids = [f"6a91c0e5f1d2a3b4c5d6e9{i:02d}" for i in range(7)]
        baseline = parsed(a_run(BASELINE, *(task(i, "fail") for i in ids)))
        candidate = parsed(a_run(CANDIDATE, *(task(i, "pass") for i in ids)))

        text = tally(RunComparison.between(baseline, candidate))

        assert text.startswith("Recovered: 7 (task 0, task 1, task 2, task 3, task 4 and 2 more) · ")
        assert text.endswith("only on the baseline: 0 · only on the candidate: 0")

    def test_the_markdown_shows_api_text_as_text(self) -> None:
        baseline = parsed(a_run(BASELINE, task(T1, "pass"), label="main"))
        evil = "![x](https://evil.example/p.png) <img src=x> | cell"
        candidate = parsed(a_run(CANDIDATE, task(T1, "fail", evidence=evil, task_name="refund **policy**")))

        text = RunComparison.between(baseline, candidate).to_markdown()

        assert "### LayerLens: support-agent — 1 task regressed" in text
        assert f"| Baseline | `{BASELINE}` | `main` | 1/1 | 100.0% | success |" in text
        assert "Pass rate -100.0 points." in text
        assert "refund \\*\\*policy\\*\\*" in text
        assert "\\!\\[x\\]" in text and "https\\://" in text and "\\<img" in text and "\\| cell" in text
        assert re.search(r"(?<!\\)!\[|(?<!\\)<img", text) is None, "no image and no tag left unescaped"


@pytest.fixture(autouse=True)
def _client_on_the_fake_api(api: FakeAPI) -> Iterator[None]:
    with patch("layerlens.cli.commands.env._common.build_client", lambda ctx, key, use_bearer_auth: make_client(api)):
        yield


def invoke(runner: CliRunner, *args: str, env: Dict[str, str] = KEY_ENV) -> Any:
    return runner.invoke(cli, ["-q", *args], env=env, catch_exceptions=False)


class TestListCommand:
    def test_a_table_newest_first_and_the_cursor_to_go_on(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on(
            "GET",
            f"/{ENV_ID}/runs",
            ok({"runs": [summary(CANDIDATE, label="pr-17"), summary(BASELINE, label="main")], "next_cursor": "c1"}),
        )

        result = invoke(runner, "env", "run", "list", ENV_ID, "--limit", "2", "--status", "success")

        assert result.exit_code == 0, result.stderr
        lines = result.stdout.splitlines()
        assert lines[0].split() == ["Run", "Started", "Kind", "Agent", "Label", "Status", "Passed", "Pass", "rate"]
        assert CANDIDATE in lines[2] and "pr-17" in lines[2] and "2026-11-05 16:20 UTC" in lines[2]
        assert BASELINE in lines[3] and "main" in lines[3] and "1/1" in lines[3] and "100.0%" in lines[3]
        assert "More runs: add --cursor c1" in result.stderr
        assert dict(api.calls("GET", f"/{ENV_ID}/runs")[0].url.params) == {"status": "success", "limit": "2"}

    def test_a_limit_past_one_page_reads_pages_of_what_is_still_wanted(self, runner: CliRunner, api: FakeAPI) -> None:
        pages = iter(
            [
                {"runs": [summary(f"6a91c0e5f1d2a3b4c5d6{i:04d}") for i in range(50)], "next_cursor": "c1"},
                {"runs": [summary(f"6a91c0e5f1d2a3b4c5d7{i:04d}") for i in range(10)], "next_cursor": "c2"},
            ]
        )
        api.on("GET", f"/{ENV_ID}/runs", lambda request: ok(next(pages)))

        result = invoke(runner, "--format", "json", "env", "run", "list", ENV_ID, "--limit", "60", "--label", "main")

        assert result.exit_code == 0, result.stderr
        out = json.loads(result.stdout)
        assert len(out["runs"]) == 60 and out["next_cursor"] == "c2"
        assert [dict(r.url.params) for r in api.calls("GET", f"/{ENV_ID}/runs")] == [
            {"label": "main", "limit": "50"},
            {"label": "main", "limit": "10", "cursor": "c1"},
        ]

    def test_no_runs(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}/runs", ok({"runs": [], "next_cursor": None}))
        result = invoke(runner, "env", "run", "list", ENV_ID, "--agent-name", "nobody")
        assert result.exit_code == 0
        assert result.stdout.strip() == "No runs."
        assert "More runs" not in result.stderr

    def test_the_apis_refusal_exits_1_as_every_env_command(self, runner: CliRunner, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}/runs", err(400, "cursor is not one this list gave"))
        result = invoke(runner, "env", "run", "list", ENV_ID, "--cursor", "junk")
        assert result.exit_code == 1
        assert "cursor is not one this list gave" in result.stderr


def serves(api: FakeAPI, *runs: Dict[str, Any]) -> None:
    for run in runs:
        api.on("GET", f"/{ENV_ID}/runs/{run['run_id']}", ok(run))


class TestCompareCommand:
    def test_no_regression_exits_0(self, runner: CliRunner, api: FakeAPI) -> None:
        serves(
            api,
            a_run(BASELINE, task(T1, "pass"), task(T2, "fail")),
            a_run(CANDIDATE, task(T1, "pass"), task(T2, "pass")),
        )

        result = invoke(runner, "env", "run", "compare", ENV_ID, BASELINE, CANDIDATE)

        assert result.exit_code == 0, result.stderr
        assert f"Baseline   run {BASELINE}" in result.stdout
        assert f"Candidate  run {CANDIDATE}" in result.stdout
        assert "Pass rate  +50.0 points (50.0% -> 100.0%)" in result.stdout
        assert "Recovered: 1 (task 2) · only on the baseline: 0 · only on the candidate: 0" in result.stdout
        assert "No regression" in result.stderr

    def test_a_regression_exits_1_and_names_every_task(self, runner: CliRunner, api: FakeAPI, tmp_path: Path) -> None:
        serves(
            api,
            a_run(BASELINE, task(T1, "pass"), task(T2, "pass"), task(T3, "pass")),
            a_run(
                CANDIDATE,
                task(T1, "pass"),
                task(T2, "fail", evidence="expected 12, the agent answered 11"),
                task(T3, "timeout", error_source="agent", evidence="no answer in 10m"),
            ),
        )
        summary_file = tmp_path / "summary.md"
        summary_file.write_text("# Earlier step\n")

        result = invoke(runner, "env", "run", "compare", ENV_ID, BASELINE, CANDIDATE, "--markdown", str(summary_file))

        assert result.exit_code == 1, result.stderr
        assert "2 tasks passed on the baseline and fail on the candidate:" in result.stdout
        assert "task 2" in result.stdout and "expected 12, the agent answered 11" in result.stdout
        assert "task 3" in result.stdout and "timeout" in result.stdout
        assert "Pass rate  -66.7 points (100.0% -> 33.3%)" in result.stdout
        assert "Regression: 2 tasks regressed" in result.stderr
        written = summary_file.read_text()
        assert written.startswith("# Earlier step\n### LayerLens: support-agent — 2 tasks regressed")

    def test_a_regression_exits_1_even_when_the_markdown_summary_cannot_be_written(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path
    ) -> None:
        # It exited 2, "the runs could not be compared", after comparing them and finding the
        # regression: a CI gate that reads 2 as a broken pipeline, not a regression, let it pass.
        serves(api, a_run(BASELINE, task(T1, "pass")), a_run(CANDIDATE, task(T1, "fail")))
        unwritable = tmp_path / "no-such-dir" / "summary.md"

        result = invoke(runner, "env", "run", "compare", ENV_ID, BASELINE, CANDIDATE, "--markdown", str(unwritable))

        assert result.exit_code == 1, result.stderr
        assert "1 task passed on the baseline and fails on the candidate:" in result.stdout
        assert f"Error: the Markdown summary could not be appended to {unwritable}" in result.stderr
        assert "Regression: 1 task regressed" in result.stderr
        assert "unexpected error" not in result.stderr

    def test_no_regression_with_a_summary_that_cannot_be_written_exits_2(
        self, runner: CliRunner, api: FakeAPI, tmp_path: Path
    ) -> None:
        # Nothing regressed, but the step did not do what it was asked: not a pass, and never 1.
        serves(api, a_run(BASELINE, task(T1, "pass")), a_run(CANDIDATE, task(T1, "pass")))
        unwritable = tmp_path / "no-such-dir" / "summary.md"

        result = invoke(runner, "env", "run", "compare", ENV_ID, BASELINE, CANDIDATE, "--markdown", str(unwritable))

        assert result.exit_code == 2, result.stderr
        assert f"Error: the Markdown summary could not be appended to {unwritable}" in result.stderr
        assert "the runs were compared and no task regressed" in result.stderr
        assert "unexpected error" not in result.stderr

    def test_json_is_the_comparison(self, runner: CliRunner, api: FakeAPI) -> None:
        serves(api, a_run(BASELINE, task(T1, "pass")), a_run(CANDIDATE, task(T1, "error", error_source="agent")))

        result = invoke(runner, "--format", "json", "env", "run", "compare", ENV_ID, BASELINE, CANDIDATE)

        assert result.exit_code == 1
        out = json.loads(result.stdout)
        assert out["regression"] is True
        assert out["baseline"]["run_id"] == BASELINE and "attempts" not in out["baseline"]
        assert out["regressed"][0]["task_id"] == T1
        assert out["regressed"][0]["candidate"]["verdict"] == "error"
        assert out["pass_rate_delta"] == -1.0

    def test_baseline_label_is_the_newest_succeeded_run_of_the_candidates_agent(
        self, runner: CliRunner, api: FakeAPI
    ) -> None:
        candidate = a_run(CANDIDATE, task(T1, "pass"), label="main", agent_name="support-agent", kind="external")
        serves(api, candidate, a_run(BASELINE, task(T1, "pass")))
        api.on(
            "GET",
            f"/{ENV_ID}/runs",
            ok({"runs": [summary(CANDIDATE, label="main")], "next_cursor": "c1"}),
            ok({"runs": [summary(BASELINE, label="main"), summary(OLDER, label="main")], "next_cursor": None}),
        )

        result = invoke(runner, "env", "run", "compare", ENV_ID, "--baseline-label", "main", CANDIDATE)

        assert result.exit_code == 0, result.stderr
        assert f"Baseline: run {BASELINE}, the newest run labelled 'main' that succeeded" in result.stderr
        listed = [dict(r.url.params) for r in api.calls("GET", f"/{ENV_ID}/runs")]
        assert listed[0] == {
            "label": "main",
            "agent_name": "support-agent",
            "kind": "external",
            "status": "success",
            "limit": "2",
        }, "the candidate's own agent and kind, and only runs that succeeded"
        assert listed[1]["cursor"] == "c1"
        assert api.calls("GET", f"/{ENV_ID}/runs/{OLDER}") == [], "the newest one, not every one"

    def test_baseline_agent_name_names_another_agents_run(self, runner: CliRunner, api: FakeAPI) -> None:
        serves(
            api,
            a_run(CANDIDATE, task(T1, "pass"), agent_name="support-agent-v8"),
            a_run(BASELINE, task(T1, "pass"), agent_name="support-agent-v7"),
        )
        api.on("GET", f"/{ENV_ID}/runs", ok({"runs": [summary(BASELINE)], "next_cursor": None}))

        result = invoke(
            runner, "env", "run", "compare", ENV_ID, "--baseline-label", "main",
            "--baseline-agent-name", "support-agent-v7", CANDIDATE,
        )  # fmt: skip

        assert result.exit_code == 0, result.stderr
        assert dict(api.calls("GET", f"/{ENV_ID}/runs")[0].url.params)["agent_name"] == "support-agent-v7"
        assert "different agents: 'support-agent-v7' and 'support-agent-v8'" in result.stderr

    def test_no_run_with_that_label_exits_2(self, runner: CliRunner, api: FakeAPI) -> None:
        serves(api, a_run(CANDIDATE, task(T1, "pass")))
        api.on("GET", f"/{ENV_ID}/runs", ok({"runs": [], "next_cursor": None}))

        result = invoke(runner, "env", "run", "compare", ENV_ID, "--baseline-label", "mian", CANDIDATE)

        assert result.exit_code == 2
        assert "no external run of agent 'support-agent' labelled 'mian' has succeeded" in result.stderr

    def test_a_candidate_without_an_agent_name_needs_the_baseline_named(self, runner: CliRunner, api: FakeAPI) -> None:
        # A model run started without --agent-name has none. Listing by label alone would take
        # the newest run of any agent or model as the baseline, so the command refuses.
        serves(api, a_run(CANDIDATE, task(T1, "pass"), agent_name=None, kind="rollout"))

        result = invoke(runner, "env", "run", "compare", ENV_ID, "--baseline-label", "main", CANDIDATE)

        assert result.exit_code == 2
        assert "the candidate has no agent name" in result.stderr
        assert "--baseline-agent-name" in result.stderr
        assert api.calls("GET", f"/{ENV_ID}/runs") == [], "no baseline is guessed from the label alone"

    def test_a_candidate_without_an_agent_name_compares_with_a_named_baseline(
        self, runner: CliRunner, api: FakeAPI
    ) -> None:
        serves(
            api,
            a_run(CANDIDATE, task(T1, "pass"), agent_name=None, kind="rollout"),
            a_run(BASELINE, task(T1, "pass"), agent_name="gpt-baseline", kind="rollout"),
        )
        api.on("GET", f"/{ENV_ID}/runs", ok({"runs": [summary(BASELINE, kind="rollout")], "next_cursor": None}))

        result = invoke(
            runner, "env", "run", "compare", ENV_ID, "--baseline-label", "main",
            "--baseline-agent-name", "gpt-baseline", CANDIDATE,
        )  # fmt: skip

        assert result.exit_code == 0, result.stderr
        assert dict(api.calls("GET", f"/{ENV_ID}/runs")[0].url.params)["agent_name"] == "gpt-baseline"

    @pytest.mark.parametrize(
        ("status", "says"),
        [
            ("in_progress", f"is still in_progress; wait for it with `layerlens env run wait {ENV_ID} {CANDIDATE}`"),
            ("failure", "did not succeed: it is failure (the proxy is down)"),
            ("cancelled", "did not succeed: it is cancelled"),
        ],
    )
    def test_a_run_that_did_not_succeed_is_not_compared(
        self, runner: CliRunner, api: FakeAPI, status: str, says: str
    ) -> None:
        error = "the proxy is down" if status == "failure" else None
        serves(api, a_run(BASELINE, task(T1, "pass")), a_run(CANDIDATE, task(T1, "fail"), status=status, error=error))

        result = invoke(runner, "env", "run", "compare", ENV_ID, BASELINE, CANDIDATE)

        assert result.exit_code == 2, "never 1: a run that did not succeed is no regression"
        assert says in result.stderr

    @pytest.mark.parametrize(
        "reply",
        [err(500, "internal error"), err(404, "run not found"), err(403, "this key has Standard access")],
    )
    def test_an_api_error_exits_2_not_1(self, runner: CliRunner, api: FakeAPI, reply: Any) -> None:
        api.on("GET", f"/{ENV_ID}/runs/{CANDIDATE}", reply)

        result = invoke(runner, "env", "run", "compare", ENV_ID, BASELINE, CANDIDATE)

        assert result.exit_code == 2
        assert reply[1]["error"] in result.stderr

    def test_a_lost_connection_exits_2(self, runner: CliRunner, api: FakeAPI) -> None:
        def drop(request: httpx.Request) -> Any:
            raise httpx.ConnectError("connection refused", request=request)

        api.on("GET", f"/{ENV_ID}/runs/{CANDIDATE}", drop)

        result = invoke(runner, "env", "run", "compare", ENV_ID, BASELINE, CANDIDATE)

        assert result.exit_code == 2

    def test_no_api_key_exits_2(self, runner: CliRunner) -> None:
        result = invoke(runner, "env", "run", "compare", ENV_ID, BASELINE, CANDIDATE, env={"LAYERLENS_API_KEY": ""})
        assert result.exit_code == 2
        assert "layerlens env needs an API key" in result.stderr

    def test_an_interrupt_exits_2(self, runner: CliRunner, api: FakeAPI) -> None:
        def interrupt(request: httpx.Request) -> Any:
            raise KeyboardInterrupt

        api.on("GET", f"/{ENV_ID}/runs/{CANDIDATE}", interrupt)

        result = invoke(runner, "env", "run", "compare", ENV_ID, BASELINE, CANDIDATE)

        assert result.exit_code == 2
        assert "interrupted; nothing was compared" in result.stderr

    @pytest.mark.parametrize(
        ("args", "says"),
        [
            ([CANDIDATE], "pass BASELINE and CANDIDATE run ids"),
            ([BASELINE, CANDIDATE, OLDER], "pass BASELINE and CANDIDATE run ids"),
            (["--baseline-label", "main", BASELINE, CANDIDATE], "with --baseline-label, pass only the candidate's"),
            (["--baseline-label", " ", CANDIDATE], "--baseline-label is blank"),
            (["--baseline-agent-name", "a", BASELINE, CANDIDATE], "--baseline-agent-name goes with --baseline-label"),
        ],
    )
    def test_usage_errors_exit_2(self, runner: CliRunner, args: List[str], says: str) -> None:
        result = invoke(runner, "env", "run", "compare", ENV_ID, *args)
        assert result.exit_code == 2
        assert says in result.stderr

    def test_the_same_run_twice_exits_2(self, runner: CliRunner, api: FakeAPI) -> None:
        serves(api, a_run(CANDIDATE, task(T1, "pass")))
        result = invoke(runner, "env", "run", "compare", ENV_ID, CANDIDATE, CANDIDATE)
        assert result.exit_code == 2
        assert "the same run" in result.stderr

    def test_different_agents_are_compared_with_a_note(self, runner: CliRunner, api: FakeAPI) -> None:
        serves(api, a_run(BASELINE, task(T1, "pass"), agent_name="billing-agent"), a_run(CANDIDATE, task(T1, "pass")))
        result = invoke(runner, "env", "run", "compare", ENV_ID, BASELINE, CANDIDATE)
        assert result.exit_code == 0
        assert "Note: the runs are of different agents: 'billing-agent' and 'support-agent'" in result.stderr
