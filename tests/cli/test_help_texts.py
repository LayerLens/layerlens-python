"""m9: help texts that were wrong, and two commands that ended badly on a usage problem.

- ``layerlens bulk --help`` showed ``bulk eval --model gpt-4 --benchmark mmlu --traces trace_ids.txt``,
  which the command refuses (--traces needs --judge-id).
- ``bulk eval`` said "from a file or stdin"; ``--file -`` is refused.
- Eleven options had no help: ``synthetic generate`` --template, --count, --provider, --project-id,
  --organization-id; ``replay run`` --trace-id, --trace-file, --model-override; ``evaluations run``
  --dataset-id; ``evaluations compare`` --score-tolerance; ``env run model`` --system-prompt-file.
- ``synthetic generate --count 0`` (or -2) generated one trace and exited 0.
- ``trace export ID -o missing/x.json`` printed "Unexpected error: [Errno 2] ..."; ``synthetic generate
  --out`` and ``evaluations run --out`` into a missing folder ended in a traceback.
"""

from __future__ import annotations

import sys
import json
import importlib
from typing import Any, List, Tuple, Iterator
from pathlib import Path
from unittest.mock import Mock, patch

import click
import pytest
from click.testing import CliRunner

from layerlens.cli._app import cli


def _commands(cmd: click.Command, path: Tuple[str, ...] = ()) -> Iterator[Tuple[Tuple[str, ...], click.Command]]:
    if isinstance(cmd, click.Group):
        for name, sub in cmd.commands.items():
            yield from _commands(sub, (*path, name))
    else:
        yield path, cmd


def test_every_option_has_help() -> None:
    missing = [
        f"{' '.join(path)} {'/'.join(param.opts)}"
        for path, command in _commands(cli)
        for param in command.params
        if isinstance(param, click.Option) and not param.hidden and not param.help
    ]
    assert missing == []


def _help(runner: CliRunner, *path: str) -> str:
    result = runner.invoke(cli, [*path, "--help"])
    assert result.exit_code == 0, result.output
    return result.output


def test_the_bulk_examples_are_ones_the_command_accepts(runner: CliRunner) -> None:
    shown = _help(runner, "bulk") + _help(runner, "bulk", "eval")
    for line in shown.splitlines():
        if "--traces" in line and "bulk eval" in line:
            assert "--judge-id" in line and "--model" not in line, line


def test_bulk_eval_no_longer_says_stdin(runner: CliRunner) -> None:
    assert "stdin" not in _help(runner, "bulk", "eval")


def test_bulk_eval_wait_says_the_judge_form_does_not_wait(runner: CliRunner) -> None:
    # --judge-id/--traces never read --wait, yet the help said it waits and exits 12.
    shown = " ".join(_help(runner, "bulk", "eval").split())
    assert "With --file or --model/--benchmark: wait for the evaluations to finish" in shown
    assert "--judge-id/--traces does not wait." in shown
    assert "Mode 3 does not wait for its trace evaluations, with --wait or not." in shown


def test_score_tolerance_shows_its_default(runner: CliRunner) -> None:
    shown = _help(runner, "evaluations", "compare")
    assert "--score-tolerance" in shown and "0.02" in shown


@pytest.mark.parametrize("count", ["0", "-2"])
def test_synthetic_count_below_1_is_a_usage_error(runner: CliRunner, count: str) -> None:
    result = runner.invoke(cli, ["synthetic", "generate", "--template", "llm.chat.basic", "--count", count])

    assert result.exit_code == 2
    assert "Invalid value for '--count'" in result.output + result.stderr
    assert result.stdout == ""


def test_synthetic_count_1_makes_one_trace(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["synthetic", "generate", "--template", "llm.chat.basic", "--count", "1"])

    assert result.exit_code == 0, result.output
    assert len(result.stdout.splitlines()) == 1


def _missing(tmp_path: Path, name: str) -> str:
    return str(tmp_path / "no-such-folder" / name)


def test_trace_export_into_a_missing_folder_is_a_sentence(runner: CliRunner, tmp_path: Path) -> None:
    client = Mock()
    client.traces.get.return_value = {"id": "t1", "data": {}}
    path = _missing(tmp_path, "t.json")
    with patch("layerlens.cli.commands.trace.get_client", return_value=client):
        result = runner.invoke(cli, ["trace", "export", "t1", "-o", path], env={"LAYERLENS_API_KEY": "k"})

    assert result.exit_code == 1
    assert result.stderr == f"Error: the trace could not be written to {path}: No such file or directory\n"


def test_synthetic_out_into_a_missing_folder_is_a_sentence(runner: CliRunner, tmp_path: Path) -> None:
    path = _missing(tmp_path, "t.jsonl")
    result = runner.invoke(
        cli, ["synthetic", "generate", "--template", "llm.chat.basic", "--count", "1", "--out", path]
    )

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert f"Error: the traces could not be written to {path}: No such file or directory" in result.stderr


_TARGET = "layerlens_help_texts_target"


@pytest.fixture
def target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    folder = tmp_path / "_targets"
    folder.mkdir()
    (folder / f"{_TARGET}.py").write_text("def identity(x):\n    return x\n")
    monkeypatch.syspath_prepend(str(folder))
    sys.modules.pop(_TARGET, None)
    importlib.invalidate_caches()
    yield _TARGET
    sys.modules.pop(_TARGET, None)


def test_evaluations_run_out_into_a_missing_folder_is_a_sentence(
    runner: CliRunner, tmp_path: Path, target: str
) -> None:
    dataset = tmp_path / "ds.json"
    dataset.write_text(json.dumps([{"id": "a", "input": 1, "expected_output": 1}]))
    path = _missing(tmp_path, "run.json")
    argv: List[Any] = ["evaluations", "run", "--dataset-id", "local", "--dataset-file", str(dataset)]
    result = runner.invoke(cli, [*argv, "--target", f"{target}:identity", "--out", path])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert f"Error: the run could not be written to {path}: No such file or directory" in result.stderr
