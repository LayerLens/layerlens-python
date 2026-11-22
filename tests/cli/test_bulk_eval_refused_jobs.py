"""m8: ``bulk eval`` stopped at the first job the API refused, so the later jobs never ran.

With a traces file of a missing trace id and then a real one, ``bulk eval --judge-id --traces``
created nothing: the 404 ended the command, and the real trace was never evaluated. ``--file``
mode had no guard either. The CHANGELOG says it reports each failed job, counts it, goes on, and
exits 1 when any job failed, after printing the ones that succeeded. It now does, in every mode.
"""

from __future__ import annotations

import json
from typing import Any, List, Iterator
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest
from click.testing import CliRunner

from layerlens.models import Evaluation
from layerlens.cli._app import cli
from layerlens._exceptions import NotFoundError, BadRequestError, APIConnectionError

CORPUS = Path(__file__).resolve().parents[1] / "contract" / "corpus" / "evaluations_get_many.json"
ENV = {"LAYERLENS_API_KEY": "test"}
REQUEST = httpx.Request("POST", "https://api.test/api/v1/organizations/o/projects/p/evaluations")


def _refused(cls: Any, status: int, sentence: str) -> Any:
    response = httpx.Response(status, json={"status": "error", "error": sentence}, request=REQUEST)
    return cls(f"Error code: {status}", response=response, body=sentence)


def _evaluation(number: int, status: str = "in-progress") -> Evaluation:
    row = json.loads(CORPUS.read_text())["evaluations"][0]
    return Evaluation.model_validate({**row, "id": f"{number:024x}", "status": status})


def _named(ref: str) -> Mock:
    named = Mock(id=ref)
    named.name = ref
    return named


@pytest.fixture
def client() -> Iterator[Mock]:
    client = Mock()
    with patch("layerlens.cli.commands.bulk.get_client", return_value=client), patch(
        "layerlens.cli.commands.bulk.resolve_model", side_effect=lambda _c, ref: _named(ref)
    ), patch("layerlens.cli.commands.bulk.resolve_benchmark", side_effect=lambda _c, ref: _named(ref)):
        yield client


def _file(tmp_path: Path, name: str, lines: List[str]) -> str:
    path = tmp_path / name
    path.write_text("".join(line + "\n" for line in lines))
    return str(path)


def test_judge_mode_reports_a_refused_trace_and_evaluates_the_next(
    runner: CliRunner, client: Mock, tmp_path: Path
) -> None:
    missing = "ffffffffffffffffffffffff"

    def create(*, trace_id: str, judge_id: str) -> Any:
        if trace_id == missing:
            raise _refused(NotFoundError, 404, f"trace with ID '{trace_id}' not found")
        return Mock(id="te-2")

    client.trace_evaluations.create.side_effect = create
    traces = _file(tmp_path, "traces.txt", [missing, "6afde6a6cdb2d4ff5d02288d"])

    result = runner.invoke(cli, ["bulk", "eval", "--judge-id", "j", "--traces", traces], env=ENV)

    assert result.exit_code == 1, result.output
    assert client.trace_evaluations.create.call_count == 2
    assert f"[1] FAIL (trace={missing}) - trace with ID '{missing}' not found (HTTP 404)" in result.stderr
    assert "[2] Created: te-2" in result.output
    assert "1 of 2 trace evaluation(s) failed" in result.stderr


def test_file_mode_reports_a_refused_job_and_runs_the_next(runner: CliRunner, client: Mock, tmp_path: Path) -> None:
    created: List[Evaluation] = []

    def create(*, model: Any, benchmark: Any) -> Evaluation:
        if model.id == "deprecated-model":
            raise _refused(BadRequestError, 400, "deprecated model cannot be evaluated")
        created.append(_evaluation(len(created) + 1))
        return created[-1]

    client.evaluations.create.side_effect = create
    jobs = _file(
        tmp_path,
        "jobs.jsonl",
        [json.dumps({"model": "deprecated-model", "benchmark": "b"}), json.dumps({"model": "m", "benchmark": "b"})],
    )

    result = runner.invoke(cli, ["--format", "json", "bulk", "eval", "--file", jobs], env=ENV)

    assert result.exit_code == 1, result.output
    assert "[1] FAIL - deprecated model cannot be evaluated (HTTP 400)" in result.stderr
    assert [e["id"] for e in json.loads(result.stdout)] == [created[0].id]
    assert "1 of 2 evaluation(s) failed" in result.stderr


def test_file_mode_goes_on_after_a_lookup_that_fails(runner: CliRunner, client: Mock, tmp_path: Path) -> None:
    client.evaluations.create.side_effect = lambda **_: _evaluation(2)
    jobs = _file(
        tmp_path,
        "jobs.jsonl",
        [json.dumps({"model": "unreachable", "benchmark": "b"}), json.dumps({"model": "m", "benchmark": "b"})],
    )

    def resolve(_client: Any, ref: str) -> Any:
        if ref == "unreachable":
            raise APIConnectionError(request=REQUEST)
        return _named(ref)

    with patch("layerlens.cli.commands.bulk.resolve_model", side_effect=resolve):
        result = runner.invoke(cli, ["bulk", "eval", "--file", jobs], env=ENV)

    assert result.exit_code == 1, result.output
    assert "[1] FAIL - Connection error." in result.stderr
    assert "[2] Created" in result.output


def test_file_mode_skips_a_job_without_a_benchmark(runner: CliRunner, client: Mock, tmp_path: Path) -> None:
    client.evaluations.create.side_effect = lambda **_: _evaluation(1)
    jobs = _file(tmp_path, "jobs.jsonl", [json.dumps({"model": "m"}), json.dumps({"model": "m", "benchmark": "b"})])

    result = runner.invoke(cli, ["bulk", "eval", "--file", jobs], env=ENV)

    assert result.exit_code == 1, result.output
    assert '[1] SKIP - a job needs a model and a benchmark: {"model": "m"}' in result.stderr
    assert client.evaluations.create.call_count == 1


def test_a_dry_run_reports_a_line_that_is_not_a_job_as_the_run_does(
    runner: CliRunner, client: Mock, tmp_path: Path
) -> None:
    # A line that is not an object ended the dry run in "Unexpected error: 'list' object has no
    # attribute 'get'", and one without a benchmark was listed as "benchmark=None".
    lines = ["[1, 2]", json.dumps({"model": "m"}), json.dumps({"model": "m", "benchmark": "b"})]
    jobs = _file(tmp_path, "jobs.jsonl", lines)

    result = runner.invoke(cli, ["bulk", "eval", "--file", jobs, "--dry-run"], env=ENV)

    assert result.exit_code == 1, result.output
    assert "Unexpected error" not in result.output + result.stderr
    assert "[1] SKIP - a job needs a model and a benchmark: [1, 2]" in result.stderr
    assert '[2] SKIP - a job needs a model and a benchmark: {"model": "m"}' in result.stderr
    assert "[dry-run] Would create 1 evaluation(s):" in result.output
    assert "model=m benchmark=b" in result.output and "benchmark=None" not in result.output
    assert "2 of 3 evaluation(s) would be skipped; see above." in result.stderr
    client.evaluations.create.assert_not_called()


def test_a_wait_that_fails_is_reported_and_the_next_one_still_waits(
    runner: CliRunner, client: Mock, tmp_path: Path
) -> None:
    evaluations = iter([_evaluation(1), _evaluation(2)])
    client.evaluations.create.side_effect = lambda **_: next(evaluations)

    def wait(evaluation: Evaluation, **_: Any) -> Evaluation:
        if evaluation.id == _evaluation(1).id:
            raise _refused(NotFoundError, 404, "evaluation not found")
        return _evaluation(2, "success")

    client.evaluations.wait_for_completion.side_effect = wait
    jobs = _file(tmp_path, "jobs.jsonl", [json.dumps({"model": m, "benchmark": "b"}) for m in ("m1", "m2")])

    result = runner.invoke(cli, ["--format", "json", "bulk", "eval", "--file", jobs, "--wait"], env=ENV)

    assert result.exit_code == 1, result.output
    assert f"{_evaluation(1).id}: evaluation not found (HTTP 404)" in result.stderr
    assert client.evaluations.wait_for_completion.call_count == 2
    assert [e["status"] for e in json.loads(result.stdout)] == ["success"]
