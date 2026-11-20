"""M2: ``evaluate run --wait`` and ``bulk eval --wait`` exited 0 when the evaluation failed.

The docs' CI recipe gates the build on ``layerlens --format json evaluate run ... --wait``, which
printed the evaluation and exited 0 whatever its status, so a job was green on an evaluation that
ended in ``failure``. With ``--wait`` both commands now exit 12 when an evaluation ends in any
status but ``success``; what they print is unchanged. A job that was skipped, refused or failed
makes ``bulk eval`` exit 1, which wins over 12.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Iterator
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from layerlens.models import Evaluation
from layerlens.cli._app import cli

CORPUS = Path(__file__).resolve().parents[1] / "contract" / "corpus" / "evaluations_get_many.json"
ENV = {"LAYERLENS_API_KEY": "test"}


def _named(ref: str) -> Mock:
    named = Mock(id=ref)
    named.name = ref  # Mock(name=...) names the mock itself
    return named


def _evaluation(status: str, number: int = 1) -> Evaluation:
    row = json.loads(CORPUS.read_text())["evaluations"][0]
    return Evaluation.model_validate({**row, "id": f"{number:024x}", "status": status})


class _Client:
    """The CLI's client: creates evaluations in order, and a wait returns each one's final status."""

    def __init__(self, finals: List[str]) -> None:
        self.finals = finals
        self.created: List[Evaluation] = []
        self.evaluations = Mock()
        self.evaluations.create.side_effect = self._create
        self.evaluations.wait_for_completion.side_effect = self._wait

    def _create(self, **_: Any) -> Evaluation:
        evaluation = _evaluation("in-progress", len(self.created) + 1)
        self.created.append(evaluation)
        return evaluation

    def _wait(self, evaluation: Evaluation, **_: Any) -> Evaluation:
        number = int(evaluation.id, 16)
        return _evaluation(self.finals[number - 1], number)


@pytest.fixture
def patched() -> Iterator[Dict[str, Any]]:
    state: Dict[str, Any] = {}
    targets = ["evaluate", "bulk"]
    patches = []
    for module in targets:
        base = f"layerlens.cli.commands.{module}"
        patches += [
            patch(f"{base}.get_client", side_effect=lambda _ctx: state["client"]),
            patch(f"{base}.resolve_model", side_effect=lambda _c, ref: None if ref == "missing" else _named(ref)),
            patch(f"{base}.resolve_benchmark", side_effect=lambda _c, ref: _named(ref)),
        ]
    for p in patches:
        p.start()
    yield state
    for p in patches:
        p.stop()


def _run(runner: CliRunner, state: Dict[str, Any], finals: List[str], argv: List[str]) -> Any:
    state["client"] = _Client(finals)
    return runner.invoke(cli, argv, env=ENV)


@pytest.mark.parametrize("status", ["failure", "cancelled", "timeout"])
def test_evaluate_run_wait_exits_12_when_the_evaluation_does_not_succeed(
    runner: CliRunner, patched: Dict[str, Any], status: str
) -> None:
    argv = ["--format", "json", "evaluate", "run", "--model", "m", "--benchmark", "b", "--wait"]
    result = _run(runner, patched, [status], argv)

    assert result.exit_code == 12, result.output
    assert json.loads(result.stdout)["status"] == status  # the output is what it was


def test_evaluate_run_wait_exits_0_on_success(runner: CliRunner, patched: Dict[str, Any]) -> None:
    argv = ["--format", "json", "evaluate", "run", "--model", "m", "--benchmark", "b", "--wait"]
    result = _run(runner, patched, ["success"], argv)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "success"


def test_evaluate_run_wait_in_a_table_exits_12_too(runner: CliRunner, patched: Dict[str, Any]) -> None:
    result = _run(runner, patched, ["failure"], ["evaluate", "run", "--model", "m", "--benchmark", "b", "--wait"])

    assert result.exit_code == 12
    assert "Evaluation finished: failure" in result.output


def test_evaluate_run_without_wait_exits_0_whatever_the_status(runner: CliRunner, patched: Dict[str, Any]) -> None:
    result = _run(
        runner, patched, ["failure"], ["--format", "json", "evaluate", "run", "--model", "m", "--benchmark", "b"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "in-progress"


@pytest.mark.parametrize(("status", "code"), [("failure", 12), ("cancelled", 12), ("success", 0)])
def test_bulk_eval_model_and_benchmark_wait(runner: CliRunner, patched: Dict[str, Any], status: str, code: int) -> None:
    argv = ["--format", "json", "bulk", "eval", "--model", "m", "--benchmark", "b", "--wait"]
    result = _run(runner, patched, [status], argv)

    assert result.exit_code == code, result.output
    assert json.loads(result.stdout)["status"] == status


def _jobs(tmp_path: Path, *models: str) -> str:
    path = tmp_path / "jobs.jsonl"
    path.write_text("".join(json.dumps({"model": m, "benchmark": "b"}) + "\n" for m in models))
    return str(path)


@pytest.mark.parametrize(
    ("finals", "code"),
    [(["success", "success"], 0), (["success", "failure"], 12), (["cancelled", "success"], 12)],
)
def test_bulk_eval_file_wait_exits_12_when_any_evaluation_does_not_succeed(
    runner: CliRunner, patched: Dict[str, Any], tmp_path: Path, finals: List[str], code: int
) -> None:
    argv = ["--format", "json", "bulk", "eval", "--file", _jobs(tmp_path, "m1", "m2"), "--wait"]
    result = _run(runner, patched, finals, argv)

    assert result.exit_code == code, result.output
    assert [e["status"] for e in json.loads(result.stdout)] == finals


def test_bulk_eval_file_wait_a_skipped_job_exits_1_which_wins_over_12(
    runner: CliRunner, patched: Dict[str, Any], tmp_path: Path
) -> None:
    argv = ["--format", "json", "bulk", "eval", "--file", _jobs(tmp_path, "m1", "missing"), "--wait"]
    result = _run(runner, patched, ["failure"], argv)

    assert result.exit_code == 1, result.output
    assert "1 of 2 evaluation(s) failed" in result.stderr


def test_bulk_eval_file_without_wait_exits_0(runner: CliRunner, patched: Dict[str, Any], tmp_path: Path) -> None:
    argv = ["--format", "json", "bulk", "eval", "--file", _jobs(tmp_path, "m1", "m2")]
    result = _run(runner, patched, ["failure", "failure"], argv)

    assert result.exit_code == 0, result.output
