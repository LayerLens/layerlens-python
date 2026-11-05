"""``layerlens ci report``: a summary of what the CI step names, never the project's latest.

The CLI and the SDK client run for real; only ``httpx.Client.send`` is replaced, by a
fake API that serves bodies shaped as atlas-app sends them (benchmark evaluations come
from the recorded corpus) and records every request.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path

import httpx
import pytest
from click.testing import Result, CliRunner

from layerlens.cli._app import cli

CORPUS = Path(__file__).resolve().parents[1] / "contract" / "corpus"

ORG = "68a1f0c2d4e5b6a7c8d9e100"
PROJECT = "68a1f0c2d4e5b6a7c8d9e200"
ENV_ID = "6adcba8583c75b47f74ebd08"
RUN_ID = "6a91c0e5f1d2a3b4c5d6e7f8"
EVAL_ID = "68a1f0c2d4e5b6a7c8d9e002"
OTHER_EVAL_ID = "68a1f0c2d4e5b6a7c8d9e0ff"
TE_ID = "6b02a1c3d4e5f6a7b8c9d0e1"
TRACE_ID = "6b02a1c3d4e5f6a7b8c9d0aa"
JUDGE_ID = "6b02a1c3d4e5f6a7b8c9d0bb"

API = "/api/v1"
TRACE_EVALS = f"{API}/organizations/{ORG}/projects/{PROJECT}/trace-evaluations"
ENVIRONMENTS = f"{API}/env/organizations/{ORG}/projects/{PROJECT}/environments"
KEY_ENV = {"LAYERLENS_API_KEY": "test-key", "LAYERLENS_BASE_URL": f"https://api.test{API}"}


def _corpus(name: str) -> Dict[str, Any]:
    return json.loads((CORPUS / name).read_text())


def benchmark_evaluation(**overrides: Any) -> Dict[str, Any]:
    """``GET /evaluations/{id}`` as recorded from atlas-app, with some fields changed.

    atlas-app stores ``accuracy`` as a percentage, 0-100 (its results handler divides by
    100 and multiplies back); the corpus holds a hand-picked 0.87, so set a real one.
    """
    return {**_corpus("evaluations_get_one_computed.json"), "accuracy": 76.667, **overrides}


def envelope(data: Any) -> Dict[str, Any]:
    return {"status": "success", "data": data}


def trace_evaluation(status: str = "success", **overrides: Any) -> Dict[str, Any]:
    """``GET /trace-evaluations/{id}``: atlas-app's TraceEvaluation struct."""
    return {
        "id": TE_ID,
        "organization_id": ORG,
        "project_id": PROJECT,
        "trace_id": TRACE_ID,
        "judge_id": JUDGE_ID,
        "user_id": "ci@layerlens.ai",
        "status": status,
        "created_at": "2026-11-04T12:00:00Z",
        "started_at": "2026-11-04T12:00:01Z",
        "finished_at": "2026-11-04T12:00:09Z" if status in ("success", "failure") else None,
        **overrides,
    }


def trace_evaluation_result(passed: bool, score: float, reasoning: str) -> Dict[str, Any]:
    """``GET /trace-evaluations/{id}/results``: atlas-app's TraceEvaluationResult struct."""
    return {
        "id": "6b02a1c3d4e5f6a7b8c9d0cc",
        "trace_evaluation_id": TE_ID,
        "trace_id": TRACE_ID,
        "judge_id": JUDGE_ID,
        "score": score,
        "passed": passed,
        "reasoning": reasoning,
        "steps": [{"tool": "read_span", "args": {"span": "root"}, "result": "ok"}],
        "model": "claude-sonnet-4-20250514",
        "turns": 3,
        "latency_ms": 2500,
        "prompt_tokens": 1200,
        "completion_tokens": 300,
        "total_cost": 0.0042,
        "created_at": "2026-11-04T12:00:09Z",
    }


def environment() -> Dict[str, Any]:
    return {
        "id": ENV_ID,
        "name": "support-desk",
        "type": "zendesk",
        "current_version": 4,
        "environment_version": 4,
        "tool_count": 18,
        "created_at": "2026-10-26T12:00:00.123Z",
        "updated_at": "2026-10-26T12:05:00Z",
        "world_hash": "abc",
        "generator_version": "1.2.3",
    }


def env_run() -> Dict[str, Any]:
    """``GET /environments/{id}/runs/{run_id}``: one task passed, one failed."""
    attempt = {
        "task_version": 1,
        "grader": "answer",
        "error_source": None,
        "agent_answer": "the answer",
        "agent_answer_truncated": False,
        "cost_usd": 0.0,
        "trace_id": "6a91c0f2a7b8c9d0e1f2a3b4",
        "agent_trace_id": None,
    }
    return {
        "run_id": RUN_ID,
        "kind": "external",
        "status": "success",
        "stop_reason": "finished",
        "error": None,
        "environment_version": 4,
        "agent_name": "support-agent",
        "label": "3f2c1ab",
        "model": "external:support-agent",
        "set_name": None,
        "total": 2,
        "passed": 1,
        "failed": 1,
        "errored": 0,
        "timed_out": 0,
        "pending": 0,
        "counted": 2,
        "pass_rate": 0.5,
        "cost_usd": 0.0,
        "attempts": [
            {**attempt, "attempt_id": "a1", "task_id": "t1", "task_name": "refund-order", "verdict": "pass"},
            {
                **attempt,
                "attempt_id": "a2",
                "task_id": "t2",
                "task_name": "escalate-ticket",
                "verdict": "fail",
                "evidence": "expected priority urgent, the agent set normal",
            },
        ],
    }


Route = Tuple[int, Any]


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """A fake LayerLens API at the httpx layer, keyed by (method, path); unrouted paths 404."""
    creds_dir = tmp_path / ".layerlens"
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_DIR", creds_dir)
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_FILE", creds_dir / "credentials")
    monkeypatch.setattr("layerlens._base_client.time.sleep", lambda _: None)
    state = SimpleNamespace(routes={}, requests=[])
    organization = {"id": ORG, "name": "Acme", "projects": [{"id": PROJECT, "name": "Default"}]}
    state.routes[("GET", f"{API}/organizations")] = (200, {"data": organization})

    def send(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        state.requests.append(request)
        status, body = state.routes.get(
            (request.method, request.url.path), (404, {"status": "error", "error": "not found"})
        )
        return httpx.Response(status, content=json.dumps(body).encode(), request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    return state


def paths(api: SimpleNamespace) -> List[str]:
    """Every path the CLI asked for, leaving out the client's organization lookup."""
    lookups = (f"{API}/organizations", f"{API}/dgklmnr/organizations")
    return [r.url.path for r in api.requests if r.url.path not in lookups]


def invoke(runner: CliRunner, *args: str, env: Optional[Dict[str, str]] = None) -> Result:
    return runner.invoke(cli, list(args), env=KEY_ENV if env is None else env)


def stderr(result: Result) -> str:
    try:
        return result.stderr
    except ValueError:
        return ""


class TestWhatIsReported:
    def test_a_report_that_names_nothing_is_a_usage_error_and_asks_for_nothing(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        result = invoke(runner, "ci", "report")

        assert result.exit_code == 2, result.output + stderr(result)
        assert "--evaluation-id" in stderr(result)
        assert "--latest" in stderr(result)
        assert paths(api) == []

    def test_a_named_benchmark_evaluation_is_reported_and_the_projects_others_are_not(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        api.routes[("GET", f"{API}/evaluations/{EVAL_ID}")] = (200, benchmark_evaluation())
        someone_elses = benchmark_evaluation(id=OTHER_EVAL_ID, model_name="Someone Else's Model")
        api.routes[("GET", f"{API}/evaluations")] = (200, {"evaluations": [someone_elses], "total_count": 1})

        result = invoke(runner, "ci", "report", "--evaluation-id", EVAL_ID)

        assert result.exit_code == 0, result.output + stderr(result)
        assert f"| `{EVAL_ID}` | GPT-4o | MMLU | success | 76.7% | 2 |" in result.stdout
        assert OTHER_EVAL_ID not in result.stdout
        assert paths(api) == [f"{API}/evaluations/{EVAL_ID}"]

    def test_an_unfinished_benchmark_evaluation_shows_no_accuracy_or_failed_prompts(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        # atlas-app updates both as results arrive: until the evaluation succeeds they cover only
        # the prompts graded so far, which is no result.
        api.routes[("GET", f"{API}/evaluations/{EVAL_ID}")] = (
            200,
            benchmark_evaluation(status="in-progress", accuracy=41.5, failed_prompt_count=3),
        )

        result = invoke(runner, "ci", "report", "--evaluation-id", EVAL_ID)

        assert result.exit_code == 0, result.output + stderr(result)
        assert f"| `{EVAL_ID}` | GPT-4o | MMLU | in-progress | - | - |" in result.stdout

    def test_a_graded_trace_evaluation_shows_its_verdict_score_and_why_it_failed(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        api.routes[("GET", f"{TRACE_EVALS}/{TE_ID}")] = (200, envelope(trace_evaluation("success")))
        api.routes[("GET", f"{TRACE_EVALS}/{TE_ID}/results")] = (
            200,
            envelope(trace_evaluation_result(False, 0.4, "The agent never confirmed the refund | amount.")),
        )

        result = invoke(runner, "ci", "report", "--trace-evaluation-id", TE_ID)

        assert result.exit_code == 0, result.output + stderr(result)
        assert f"| `{TE_ID}` | `{JUDGE_ID}` | `{TRACE_ID}` | success | failed | 0.40 |" in result.stdout
        assert f"`{TE_ID}`: The agent never confirmed the refund \\| amount." in result.stdout

    def test_a_trace_evaluation_names_its_judge_from_the_snapshot_atlas_sends(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        # atlas-app's TraceEvaluation sends this one key in camelCase, and its fields in snake_case.
        snapshot = {
            "name": "Ticket resolved",
            "version": 3,
            "evaluation_goal": "Did the agent resolve the customer's ticket?",
            "model_id": "6b02a1c3d4e5f6a7b8c9d0dd",
            "model_name": "Claude Sonnet 4.5",
            "model_company": "Anthropic",
        }
        api.routes[("GET", f"{TRACE_EVALS}/{TE_ID}")] = (
            200,
            envelope(trace_evaluation("in_progress", judgeSnapshot=snapshot)),
        )

        result = invoke(runner, "ci", "report", "--trace-evaluation-id", TE_ID)

        assert result.exit_code == 0, result.output + stderr(result)
        assert f"| `{TE_ID}` | Ticket resolved v3 | `{TRACE_ID}` | in_progress | - | - |" in result.stdout

    def test_a_trace_evaluation_that_failed_to_run_says_why(self, runner: CliRunner, api: SimpleNamespace) -> None:
        api.routes[("GET", f"{TRACE_EVALS}/{TE_ID}")] = (
            200,
            envelope(trace_evaluation("failure", status_description="the judge model timed out after 120s")),
        )

        result = invoke(runner, "ci", "report", "--trace-evaluation-id", TE_ID)

        assert result.exit_code == 0, result.output + stderr(result)
        assert (
            f"| `{TE_ID}` | `{JUDGE_ID}` | `{TRACE_ID}` | failure: the judge model timed out after 120s | - | - |"
            in result.stdout
        )
        assert paths(api) == [f"{TRACE_EVALS}/{TE_ID}"]

    def test_text_from_the_api_is_shown_as_text_never_as_markup(self, runner: CliRunner, api: SimpleNamespace) -> None:
        # A judge's reasoning can quote the trace it read, which anyone who reached the agent wrote.
        markup = "Quoted: ![x](https://evil.example/p.png) <img src=x> [click](https://evil.example)"
        api.routes[("GET", f"{API}/evaluations/{EVAL_ID}")] = (200, benchmark_evaluation(model_name="model_<b>"))
        api.routes[("GET", f"{TRACE_EVALS}/{TE_ID}")] = (200, envelope(trace_evaluation("success")))
        api.routes[("GET", f"{TRACE_EVALS}/{TE_ID}/results")] = (
            200,
            envelope(trace_evaluation_result(False, 0.0, markup)),
        )

        result = invoke(runner, "ci", "report", "--evaluation-id", EVAL_ID, "--trace-evaluation-id", TE_ID)

        assert result.exit_code == 0, result.output + stderr(result)
        assert f"| `{EVAL_ID}` | model\\_\\<b\\> | MMLU |" in result.stdout
        assert (
            f"- `{TE_ID}`: Quoted: \\!\\[x\\](https\\://evil.example/p.png) \\<img src=x\\> "
            "\\[click\\](https\\://evil.example)" in result.stdout
        )

    def test_a_trace_evaluation_still_running_is_reported_without_asking_for_results(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        api.routes[("GET", f"{TRACE_EVALS}/{TE_ID}")] = (200, envelope(trace_evaluation("in_progress")))

        result = invoke(runner, "ci", "report", "--trace-evaluation-id", TE_ID)

        assert result.exit_code == 0, result.output + stderr(result)
        assert f"| `{TE_ID}` | `{JUDGE_ID}` | `{TRACE_ID}` | in_progress | - | - |" in result.stdout
        assert paths(api) == [f"{TRACE_EVALS}/{TE_ID}"]

    def test_an_environment_run_named_by_environment_name_reports_its_counts_and_failures(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        api.routes[("GET", ENVIRONMENTS)] = (
            200,
            envelope({"environments": [environment()], "count": 1, "total_count": 1}),
        )
        api.routes[("GET", f"{ENVIRONMENTS}/{ENV_ID}/runs/{RUN_ID}")] = (200, envelope(env_run()))

        result = invoke(runner, "ci", "report", "--env-run", f"support-desk:{RUN_ID}")

        assert result.exit_code == 0, result.output + stderr(result)
        assert "support-desk" in result.stdout
        assert "1/2 passed (50.0%)" in result.stdout
        assert "| escalate-ticket | fail | expected priority urgent, the agent set normal |" in result.stdout
        name_lookup = next(r for r in api.requests if r.url.path == ENVIRONMENTS)
        assert name_lookup.url.params["name"] == "support-desk"

    def test_an_environment_name_may_hold_a_colon(self, runner: CliRunner, api: SimpleNamespace) -> None:
        # atlas-app takes any 1-N characters as a name; a run id never holds a colon.
        api.routes[("GET", ENVIRONMENTS)] = (
            200,
            envelope({"environments": [{**environment(), "name": "team:support"}], "count": 1, "total_count": 1}),
        )
        api.routes[("GET", f"{ENVIRONMENTS}/{ENV_ID}/runs/{RUN_ID}")] = (200, envelope(env_run()))

        result = invoke(runner, "ci", "report", "--env-run", f"team:support:{RUN_ID}")

        assert result.exit_code == 0, result.output + stderr(result)
        name_lookup = next(r for r in api.requests if r.url.path == ENVIRONMENTS)
        assert name_lookup.url.params["name"] == "team:support"
        assert "1/2 passed (50.0%)" in result.stdout

    def test_every_kind_named_is_reported_in_one_summary(self, runner: CliRunner, api: SimpleNamespace) -> None:
        api.routes[("GET", f"{API}/evaluations/{EVAL_ID}")] = (200, benchmark_evaluation())
        api.routes[("GET", f"{TRACE_EVALS}/{TE_ID}")] = (200, envelope(trace_evaluation("in_progress")))
        api.routes[("GET", f"{ENVIRONMENTS}/{ENV_ID}/runs/{RUN_ID}")] = (200, envelope(env_run()))

        result = invoke(
            runner,
            "ci",
            "report",
            "--evaluation-id",
            EVAL_ID,
            "--trace-evaluation-id",
            TE_ID,
            "--env-run",
            f"{ENV_ID}:{RUN_ID}",
        )

        assert result.exit_code == 0, result.output + stderr(result)
        assert result.stdout.count("## LayerLens CI report") == 1
        assert f"`{EVAL_ID}`" in result.stdout
        assert f"`{TE_ID}`" in result.stdout
        assert f"run `{RUN_ID}`" in result.stdout

    def test_latest_reports_the_projects_most_recent_benchmark_evaluations(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        rows = [benchmark_evaluation(id=OTHER_EVAL_ID, model_name="Claude"), benchmark_evaluation()]
        api.routes[("GET", f"{API}/evaluations")] = (200, {"evaluations": rows, "total_count": 2})

        result = invoke(runner, "ci", "report", "--latest", "2")

        assert result.exit_code == 0, result.output + stderr(result)
        assert f"| `{OTHER_EVAL_ID}` | Claude | MMLU |" in result.stdout
        assert f"| `{EVAL_ID}` | GPT-4o | MMLU |" in result.stdout
        listing = next(r for r in api.requests if r.url.path == f"{API}/evaluations")
        assert listing.url.params["page_size"] == "2"
        assert listing.url.params["sort_by"] == "submitted_at"
        assert listing.url.params["order"] == "desc"

    def test_latest_in_a_project_without_evaluations_says_so(self, runner: CliRunner, api: SimpleNamespace) -> None:
        api.routes[("GET", f"{API}/evaluations")] = (200, {"evaluations": [], "total_count": 0})

        result = invoke(runner, "ci", "report", "--latest", "5")

        assert result.exit_code == 0, result.output + stderr(result)
        assert "No benchmark evaluations in this project." in result.stdout

    def test_an_evaluation_named_and_among_the_latest_is_reported_once(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        api.routes[("GET", f"{API}/evaluations/{EVAL_ID}")] = (200, benchmark_evaluation())
        rows = [benchmark_evaluation(), benchmark_evaluation(id=OTHER_EVAL_ID, model_name="Claude")]
        api.routes[("GET", f"{API}/evaluations")] = (200, {"evaluations": rows, "total_count": 2})

        result = invoke(runner, "ci", "report", "--evaluation-id", EVAL_ID, "--evaluation-id", EVAL_ID, "--latest", "2")

        assert result.exit_code == 0, result.output + stderr(result)
        assert result.stdout.count(f"| `{EVAL_ID}` |") == 1
        assert result.stdout.count(f"| `{OTHER_EVAL_ID}` |") == 1

    def test_a_trace_evaluation_or_run_named_twice_is_fetched_and_reported_once(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        api.routes[("GET", f"{TRACE_EVALS}/{TE_ID}")] = (200, envelope(trace_evaluation("in_progress")))
        api.routes[("GET", f"{ENVIRONMENTS}/{ENV_ID}/runs/{RUN_ID}")] = (200, envelope(env_run()))
        run = f"{ENV_ID}:{RUN_ID}"

        result = invoke(
            runner, "ci", "report", "--trace-evaluation-id", TE_ID, "--trace-evaluation-id", TE_ID,
            "--env-run", run, "--env-run", run,
        )  # fmt: skip

        assert result.exit_code == 0, result.output + stderr(result)
        assert paths(api) == [f"{TRACE_EVALS}/{TE_ID}", f"{ENVIRONMENTS}/{ENV_ID}/runs/{RUN_ID}"]
        assert result.stdout.count(f"| `{TE_ID}` |") == 1
        assert result.stdout.count(f"run `{RUN_ID}`") == 1

    def test_limit_still_works_as_latest_with_a_deprecation_notice(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        api.routes[("GET", f"{API}/evaluations")] = (200, {"evaluations": [benchmark_evaluation()], "total_count": 1})

        result = invoke(runner, "ci", "report", "--limit", "3")

        assert result.exit_code == 0, result.output + stderr(result)
        assert f"`{EVAL_ID}`" in result.stdout
        assert "--limit is deprecated" in stderr(result)
        assert "--latest" in stderr(result)
        listing = next(r for r in api.requests if r.url.path == f"{API}/evaluations")
        assert listing.url.params["page_size"] == "3"


class TestErrors:
    @pytest.mark.parametrize("value", ["support-desk", f":{RUN_ID}", "support-desk:"])
    def test_an_env_run_that_is_not_env_colon_run_id_is_a_usage_error(
        self, runner: CliRunner, api: SimpleNamespace, value: str
    ) -> None:
        result = invoke(runner, "ci", "report", "--env-run", value)

        assert result.exit_code == 2, result.output + stderr(result)
        assert "ENV:RUN_ID" in stderr(result)
        assert paths(api) == []

    @pytest.mark.parametrize("option", ["--evaluation-id", "--trace-evaluation-id"])
    @pytest.mark.parametrize("value", ["", "  "])
    def test_an_empty_id_is_a_usage_error(
        self, runner: CliRunner, api: SimpleNamespace, option: str, value: str
    ) -> None:
        # An earlier CI step that failed before writing its id leaves the variable empty.
        result = invoke(runner, "ci", "report", option, value)

        assert result.exit_code == 2, result.output + stderr(result)
        assert "empty" in stderr(result)
        assert paths(api) == []

    def test_an_evaluation_that_does_not_exist_fails_the_report_and_names_it(
        self, runner: CliRunner, api: SimpleNamespace, tmp_path: Path
    ) -> None:
        api.routes[("GET", f"{API}/evaluations/{EVAL_ID}")] = (200, benchmark_evaluation())
        summary = tmp_path / "summary.md"

        result = invoke(
            runner, "ci", "report", "--evaluation-id", EVAL_ID, "--evaluation-id", OTHER_EVAL_ID, "-o", str(summary)
        )

        assert result.exit_code == 1, result.output + stderr(result)
        assert stderr(result).strip() == f"Error: evaluation {OTHER_EVAL_ID}: not found (HTTP 404)"
        assert result.stdout == ""
        assert not summary.exists()

    def test_a_finished_trace_evaluation_whose_result_cannot_be_read_fails_the_report(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        api.routes[("GET", f"{TRACE_EVALS}/{TE_ID}")] = (200, envelope(trace_evaluation("success")))

        result = invoke(runner, "ci", "report", "--trace-evaluation-id", TE_ID)

        assert result.exit_code == 1, result.output + stderr(result)
        assert stderr(result).strip() == (
            f"Error: trace evaluation {TE_ID} succeeded, but its result is missing or could not be read"
        )
        assert result.stdout == ""

    def test_a_refused_environment_run_fails_with_the_apis_reason(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        reason = "this key has Standard access; this route needs an Automation key"
        api.routes[("GET", f"{ENVIRONMENTS}/{ENV_ID}/runs/{RUN_ID}")] = (403, {"status": "error", "error": reason})

        result = invoke(runner, "ci", "report", "--env-run", f"{ENV_ID}:{RUN_ID}")

        assert result.exit_code == 1, result.output + stderr(result)
        assert f"environment run {ENV_ID}:{RUN_ID}" in stderr(result)
        assert reason in stderr(result)

    def test_an_environment_run_with_a_login_session_says_an_api_key_is_needed(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        import time

        from layerlens.cli._auth import save_credentials

        save_credentials(
            {
                "access_token": "access-tok",
                "id_token": "id-tok",
                "refresh_token": "refresh-tok",
                "expires_at": time.time() + 3600,
                "user": {"email": "someone@example.com", "name": "Someone", "sub": "user-1"},
            }
        )
        # A login session reaches the API through the login router, which lists the user's organizations.
        organization = {"id": ORG, "name": "Acme", "projects": [{"id": PROJECT, "name": "Default"}]}
        api.routes[("GET", f"{API}/dgklmnr/organizations")] = (200, {"data": [organization]})

        result = invoke(
            runner,
            "ci",
            "report",
            "--env-run",
            f"{ENV_ID}:{RUN_ID}",
            env={"LAYERLENS_BASE_URL": f"https://api.test{API}"},
        )

        assert result.exit_code == 1, result.output + stderr(result)
        assert "Automation" in stderr(result)
        assert paths(api) == []


class TestOutput:
    def test_the_markdown_report_is_appended_so_earlier_steps_summaries_survive(
        self, runner: CliRunner, api: SimpleNamespace, tmp_path: Path
    ) -> None:
        api.routes[("GET", f"{API}/evaluations/{EVAL_ID}")] = (200, benchmark_evaluation())
        summary = tmp_path / "step_summary.md"
        summary.write_text("### An earlier step's summary\n")

        result = invoke(runner, "ci", "report", "--evaluation-id", EVAL_ID, "-o", str(summary))

        assert result.exit_code == 0, result.output + stderr(result)
        content = summary.read_text()
        assert content.startswith("### An earlier step's summary\n")
        assert f"`{EVAL_ID}`" in content
        assert EVAL_ID not in result.stdout

    def test_json_names_each_kind_with_its_fields(self, runner: CliRunner, api: SimpleNamespace) -> None:
        api.routes[("GET", f"{API}/evaluations/{EVAL_ID}")] = (200, benchmark_evaluation())
        api.routes[("GET", f"{TRACE_EVALS}/{TE_ID}")] = (200, envelope(trace_evaluation("success")))
        api.routes[("GET", f"{TRACE_EVALS}/{TE_ID}/results")] = (
            200,
            envelope(trace_evaluation_result(True, 0.9, "All criteria met.")),
        )
        api.routes[("GET", f"{ENVIRONMENTS}/{ENV_ID}/runs/{RUN_ID}")] = (200, envelope(env_run()))

        result = invoke(
            runner,
            "--format",
            "json",
            "ci",
            "report",
            "--evaluation-id",
            EVAL_ID,
            "--trace-evaluation-id",
            TE_ID,
            "--env-run",
            f"{ENV_ID}:{RUN_ID}",
        )

        assert result.exit_code == 0, result.output + stderr(result)
        report = json.loads(result.stdout)
        assert [e["id"] for e in report["evaluations"]] == [EVAL_ID]
        assert report["evaluations"][0]["accuracy"] == 76.667
        [te] = report["trace_evaluations"]
        assert (te["id"], te["status"], te["result"]["passed"], te["result"]["score"]) == (TE_ID, "success", True, 0.9)
        [run] = report["environment_runs"]
        assert (run["environment"], run["run_id"], run["passed"], run["total"]) == (ENV_ID, RUN_ID, 1, 2)
        assert report["generated_at"].endswith("Z")

    def test_a_json_report_file_is_replaced_so_it_stays_one_json_document(
        self, runner: CliRunner, api: SimpleNamespace, tmp_path: Path
    ) -> None:
        api.routes[("GET", f"{API}/evaluations/{EVAL_ID}")] = (200, benchmark_evaluation())
        out = tmp_path / "report.json"

        for _ in range(2):
            result = invoke(runner, "--format", "json", "ci", "report", "--evaluation-id", EVAL_ID, "-o", str(out))
            assert result.exit_code == 0, result.output + stderr(result)

        assert [e["id"] for e in json.loads(out.read_text())["evaluations"]] == [EVAL_ID]

    def test_dry_run_says_what_it_would_report_and_asks_for_nothing(
        self, runner: CliRunner, api: SimpleNamespace
    ) -> None:
        result = invoke(
            runner, "ci", "report", "--evaluation-id", EVAL_ID, "--env-run", f"{ENV_ID}:{RUN_ID}", "--dry-run"
        )

        assert result.exit_code == 0, result.output + stderr(result)
        assert "[dry-run]" in result.output
        assert "1 benchmark evaluation" in result.output
        assert "1 environment run" in result.output
        assert api.requests == []
