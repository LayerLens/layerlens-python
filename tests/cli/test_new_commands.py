"""CLI tests for replay / synthetic / evaluations subcommands."""

from __future__ import annotations

import sys
import json
import inspect

import click
import pytest
from click.testing import CliRunner

from layerlens.cli._app import cli
from layerlens.evaluation_runs.models import (
    RunAggregate,
    EvaluationRun,
    EvaluationRunStatus,
)


@pytest.fixture
def runner():
    # `mix_stderr` is incompatible across click versions in this repo's baseline;
    # use the default runner which still separates streams via --catch.
    return CliRunner()


# ---------------------------------------------------------------------------
# synthetic
# ---------------------------------------------------------------------------


class TestSyntheticCommands:
    def test_templates_lists_known_ids(self, runner):
        result = runner.invoke(cli, ["--quiet", "synthetic", "templates"])
        assert result.exit_code == 0
        assert "llm.chat.basic" in result.output
        assert "rag.retrieval" in result.output

    def test_generate_to_stdout(self, runner):
        result = runner.invoke(
            cli,
            [
                "--quiet",
                "synthetic",
                "generate",
                "--template",
                "llm.chat.basic",
                "--count",
                "2",
            ],
        )
        assert result.exit_code == 0
        lines = [line for line in result.output.splitlines() if line.startswith("{")]
        assert len(lines) == 2
        parsed = json.loads(lines[0])
        assert parsed["data"]["synthetic"] is True

    def test_generate_to_file(self, runner, tmp_path):
        out = tmp_path / "traces.jsonl"
        result = runner.invoke(
            cli,
            [
                "--quiet",
                "synthetic",
                "generate",
                "--template",
                "rag.retrieval",
                "--count",
                "3",
                "--out",
                str(out),
            ],
        )
        assert result.exit_code == 0
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_generate_unknown_template_exits_nonzero(self, runner):
        result = runner.invoke(
            cli,
            [
                "--quiet",
                "synthetic",
                "generate",
                "--template",
                "does.not.exist",
                "--count",
                "1",
            ],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def _exported_trace(tmp_path) -> str:
    """A trace as `layerlens trace export` writes it: `replay run` reads it without the API."""
    path = tmp_path / "t1.json"
    path.write_text(
        json.dumps(
            {
                "id": "t1",
                "organization_id": "o",
                "project_id": "p",
                "created_at": "x",
                "filename": "t1.json",
                "data": {},
            }
        )
    )
    return str(path)


class TestReplayCommands:
    def test_run_fallback_prints_json(self, runner, tmp_path):
        result = runner.invoke(
            cli, ["--quiet", "replay", "run", "--trace-id", "t1", "--trace-file", _exported_trace(tmp_path)]
        )
        assert result.exit_code == 0
        payload = json.loads(
            result.output.split("\n{", 1)[-1] if not result.output.lstrip().startswith("{") else result.output
        )
        assert payload["original_trace_id"] == "t1"
        assert payload["status"] == "completed"

    def test_run_propagates_model_override_into_metadata(self, runner, tmp_path):
        result = runner.invoke(
            cli,
            [
                "--quiet",
                "replay",
                "run",
                "--trace-id",
                "t1",
                "--trace-file",
                _exported_trace(tmp_path),
                "--model-override",
                "gpt-4o-mini",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(_last_json_blob(result.output))
        assert payload["metadata"]["replay_type"] == "model_swap"
        assert payload["metadata"]["overrides"]["model"] == "gpt-4o-mini"

    def test_bad_replay_fn_spec_errors(self, runner):
        result = runner.invoke(
            cli,
            [
                "--quiet",
                "replay",
                "run",
                "--trace-id",
                "t1",
                "--replay-fn",
                "no_colon",
            ],
        )
        assert result.exit_code != 0
        assert "expected 'module:attr'" in result.output


# ---------------------------------------------------------------------------
# replay-fn / --target RCE guard (A5 / PAY-SEC-1) — POSITIVE security tests
# ---------------------------------------------------------------------------
#
# The loader resolves a user ``module:attr`` string and the controller INVOKES
# it — a remote-code-execution surface. The old guard was a 12-name stdlib
# denylist; it failed OPEN for ``posix:system`` (== ``os.system``; ``posix`` not
# listed) and for any import-with-side-effects module (``import_module`` ran the
# target's top-level code BEFORE any attr check). There was ZERO positive test:
# deleting the denylist kept the suite green. These tests drive the REAL Click
# commands and assert a malicious target is BLOCKED. BITE: neuter the allowlist
# check in src/layerlens/cli/_safe_loader.py (``_is_stdlib_root`` -> ``return
# False``) and every case below goes from refused → loaded (RED).

# Each entry exercises a distinct bypass class against the loader:
#   posix:system            -> the headline bypass: posix.system IS os.system
#   os:system               -> the obvious one the denylist did cover
#   subprocess:run          -> process spawn
#   builtins:eval           -> arbitrary-code primitive
#   sys:exit / ctypes:CDLL  -> other denylisted roots, allowlist-confirmed
#   platform:_syscmd_uname  -> a stdlib *submodule attr* shell-out the denylist missed
#   antigravity:__name__    -> import-time side-effect module (opens a browser on import)
#   pdb:run / webbrowser:open / this:s -> more import-side-effect / process modules
_MALICIOUS_TARGETS = [
    "posix:system",
    "os:system",
    "os.path:exists",
    "subprocess:run",
    "subprocess:Popen",
    "builtins:eval",
    "builtins:exec",
    "sys:exit",
    "ctypes:CDLL",
    "shutil:rmtree",
    "runpy:run_path",
    "importlib:import_module",
    "pickle:loads",
    "socket:socket",
    "platform:_syscmd_uname",
    "antigravity:__name__",
    "pdb:run",
    "webbrowser:open",
    "this:s",
]


class TestReplayFnRceGuard:
    @pytest.mark.parametrize("target", _MALICIOUS_TARGETS)
    def test_replay_fn_blocks_malicious_target(self, runner, target):
        result = runner.invoke(
            cli,
            ["--quiet", "replay", "run", "--trace-id", "t1", "--replay-fn", target],
        )
        # Refused at parse time (BadParameter -> exit 2), NEVER loaded/invoked.
        assert result.exit_code != 0, f"{target!r} was NOT refused (RCE bypass)"
        assert "refusing to load" in result.output, f"{target!r} did not hit the allowlist guard"

    @pytest.mark.parametrize("target", _MALICIOUS_TARGETS)
    def test_evaluations_target_blocks_malicious(self, runner, tmp_path, target):
        ds_path = tmp_path / "ds.json"
        ds_path.write_text(json.dumps([{"id": "a", "input": 1, "expected_output": 1}]))
        result = runner.invoke(
            cli,
            [
                "--quiet",
                "evaluations",
                "run",
                "--dataset-id",
                "local",
                "--dataset-file",
                str(ds_path),
                "--target",
                target,
            ],
        )
        assert result.exit_code != 0, f"--target {target!r} was NOT refused (RCE bypass)"
        assert "refusing to load" in result.output, f"--target {target!r} did not hit the allowlist guard"

    @pytest.mark.parametrize("target", _MALICIOUS_TARGETS)
    def test_evaluations_scorer_blocks_malicious(self, runner, tmp_path, target_module, target):
        ds_path = tmp_path / "ds.json"
        ds_path.write_text(json.dumps([{"id": "a", "input": 1, "expected_output": 1}]))
        result = runner.invoke(
            cli,
            [
                "--quiet",
                "evaluations",
                "run",
                "--dataset-id",
                "local",
                "--dataset-file",
                str(ds_path),
                "--target",
                f"{target_module}:identity",
                "--scorer",
                f"evil={target}",
            ],
        )
        assert result.exit_code != 0, f"--scorer {target!r} was NOT refused (RCE bypass)"
        assert "refusing to load" in result.output, f"--scorer {target!r} did not hit the allowlist guard"

    def test_import_module_never_called_for_blocked_target(self, monkeypatch):
        """The allowlist check precedes import_module, so a blocked module's
        TOP-LEVEL code (an import-time side effect — ``antigravity`` opens a
        browser, a malicious package's ``__init__`` runs) NEVER executes: the
        guard refuses the root before any import.

        We spy on the loader's ``importlib.import_module`` and assert it is never
        invoked for a blocked target. BITE: with the allowlist neutered the
        denied target would fall through to ``import_module`` and this spy would
        fire (and the real side effect would run).
        """
        from layerlens.cli import _safe_loader

        calls = []
        real_import = _safe_loader.importlib.import_module

        def spy(name, *a, **k):
            calls.append(name)
            return real_import(name, *a, **k)

        monkeypatch.setattr(_safe_loader.importlib, "import_module", spy)

        for target in ("posix:system", "os:system", "antigravity:__name__", "this:s"):
            with pytest.raises(click.BadParameter):
                _safe_loader.load_callable(target, param_hint="--replay-fn")
        assert calls == [], f"import_module was called for a blocked target: {calls}"

    def test_real_application_target_is_allowed(self, runner, tmp_path, target_module):
        """The allowlist must not over-block: a real on-disk application module
        loads (this is the legitimate use). Drives the real evaluations command
        end-to-end with a real --target + --scorer."""
        ds_path = tmp_path / "ds.json"
        ds_path.write_text(json.dumps([{"id": "a", "input": 1, "expected_output": 1}]))
        result = runner.invoke(
            cli,
            [
                "--quiet",
                "evaluations",
                "run",
                "--dataset-id",
                "local",
                "--dataset-file",
                str(ds_path),
                "--target",
                f"{target_module}:identity",
                "--scorer",
                f"exact={target_module}:scorer",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(_last_json_blob(result.output))
        assert payload["status"] == "completed"


# ---------------------------------------------------------------------------
# evaluations
# ---------------------------------------------------------------------------


_TARGET_MODULE = "layerlens_test_target_module"

# The safe loader (layerlens.cli._safe_loader) deliberately refuses any module
# that does not resolve to a REAL on-disk source file outside the stdlib — an
# in-memory ``types.ModuleType`` injected into ``sys.modules`` has no file spec
# and is rejected (fail-closed against an attacker poisoning sys.modules). So the
# test target must be a real importable module on disk, exactly as a production
# ``--target mypkg.module:fn`` is. We write one into tmp and add it to sys.path.
_TARGET_SOURCE = """\
def identity(x):
    return x


def scorer(actual, expected, _meta):
    return 1.0 if actual == expected else 0.0
"""


@pytest.fixture
def target_module(tmp_path, monkeypatch):
    """Materialise a real on-disk ``layerlens_test_target_module`` and import it.

    Yields the importable module name. Cleans the import caches afterward so the
    module name does not leak across tests.
    """
    import importlib

    mod_dir = tmp_path / "_targets"
    mod_dir.mkdir()
    (mod_dir / f"{_TARGET_MODULE}.py").write_text(_TARGET_SOURCE)
    monkeypatch.syspath_prepend(str(mod_dir))
    sys.modules.pop(_TARGET_MODULE, None)
    importlib.invalidate_caches()
    try:
        yield _TARGET_MODULE
    finally:
        sys.modules.pop(_TARGET_MODULE, None)


class TestEvaluationsCommands:
    def test_run_requires_dataset_file(self, runner, target_module):
        # F9: docs/cli/commands.md marks --dataset-file required; the CLI marked it optional,
        # listed it so in --help, then refused to run without it. It is a required option now.
        result = runner.invoke(
            cli,
            [
                "--quiet",
                "evaluations",
                "run",
                "--dataset-id",
                "d1",
                "--target",
                f"{target_module}:identity",
            ],
        )
        assert result.exit_code == 2
        assert "Missing option '--dataset-file'" in result.output
        shown = runner.invoke(cli, ["evaluations", "run", "--help"]).output
        assert "[required]" in shown.split("--dataset-file", 1)[1].split("--target", 1)[0]

    def test_run_reads_dataset_file_and_emits_run(self, runner, tmp_path, target_module):
        ds_path = tmp_path / "ds.json"
        ds_path.write_text(
            json.dumps(
                [
                    {"id": "a", "input": 1, "expected_output": 1},
                    {"id": "b", "input": 2, "expected_output": 3},  # will fail
                ]
            )
        )
        result = runner.invoke(
            cli,
            [
                "--quiet",
                "evaluations",
                "run",
                "--dataset-id",
                "local",
                "--dataset-file",
                str(ds_path),
                "--target",
                f"{target_module}:identity",
                "--scorer",
                f"exact={target_module}:scorer",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(_last_json_blob(result.output))
        assert payload["status"] == "completed"
        assert 0.4 < payload["aggregate"]["pass_rate"] < 0.6  # 1 of 2 items pass

    def test_compare_exits_nonzero_on_regression(self, runner, tmp_path):
        base = _run_with(pass_rate=1.0, mean=1.0, items=[("a", True)])
        cand = _run_with(pass_rate=0.0, mean=0.0, items=[("a", False)])
        base_path = tmp_path / "base.json"
        cand_path = tmp_path / "cand.json"
        base_path.write_text(base.model_dump_json())
        cand_path.write_text(cand.model_dump_json())
        result = runner.invoke(
            cli,
            ["--quiet", "evaluations", "compare", str(base_path), str(cand_path)],
        )
        assert result.exit_code == 1
        payload = json.loads(_last_json_blob(result.output))
        assert payload["is_regression"] is True

    def test_compare_exits_zero_when_stable(self, runner, tmp_path):
        base = _run_with(pass_rate=1.0, mean=1.0, items=[("a", True)])
        cand = _run_with(pass_rate=1.0, mean=1.0, items=[("a", True)])
        base_path = tmp_path / "base.json"
        cand_path = tmp_path / "cand.json"
        base_path.write_text(base.model_dump_json())
        cand_path.write_text(cand.model_dump_json())
        result = runner.invoke(
            cli,
            ["--quiet", "evaluations", "compare", str(base_path), str(cand_path)],
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# evaluations compare: exit codes a CI step can act on (LAY-4429)
# ---------------------------------------------------------------------------


@pytest.fixture
def split_runner():
    """stdout and stderr apart: click 8.2+ always keeps them apart, older click on request."""
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


_VALID = {
    "id": "c",
    "dataset_id": "d",
    "dataset_version": 1,
    "items": [],
    "aggregate": {"mean_scores": {"exact": 1.0}, "pass_rate": 1.0},
}
_VALID_JSON = json.dumps(_VALID)


def _without(*names):
    return json.dumps({key: value for key, value in _VALID.items() if key not in names})


def _pass_rate(token):
    """``_VALID`` with ``token`` written verbatim as its pass rate."""
    return _VALID_JSON.replace('"pass_rate": 1.0', f'"pass_rate": {token}')


# (file content, what the one error line must name besides the file)
_UNREADABLE = {
    "truncated-json": ('{"id": "c", "dataset_id": "d"', ["not valid JSON", "line 1, column 30"]),
    "empty-file": ("", ["not valid JSON", "line 1, column 1"]),
    "missing-field": (_without("dataset_version"), ["dataset_version: Field required"]),
    # The model would fill these in (no items, a pass rate of 0) and compare a run that never ran.
    "no-items": (_without("items"), ["items: Field required"]),
    "no-aggregate": (_without("aggregate"), ["aggregate: Field required"]),
    "empty-aggregate": (
        json.dumps({**_VALID, "aggregate": {}}),
        ["aggregate.mean_scores: Field required", "aggregate.pass_rate: Field required"],
    ),
    # Python's json reads these, JSON has no such numbers, and a NaN pass rate never regresses.
    "nan": (_pass_rate("NaN"), ["not valid JSON", "NaN is not a JSON number"]),
    "infinity": (_pass_rate("Infinity"), ["not valid JSON", "Infinity is not a JSON number"]),
    "minus-infinity": (_pass_rate("-Infinity"), ["not valid JSON", "-Infinity is not a JSON number"]),
    "overflowing-number": (_pass_rate("1e999"), ["not valid JSON", "1e999 is too large for a number"]),
    "wrong-type": (json.dumps({**_VALID, "dataset_version": "one"}), ["dataset_version", "valid integer"]),
    "nested-field": (
        json.dumps({**_VALID, "aggregate": {"mean_scores": {}, "pass_rate": "high"}}),
        ["aggregate.pass_rate", "valid number"],
    ),
    "list-item-field": (json.dumps({**_VALID, "items": [{"input": 1}]}), ["items[0].item_id", "Field required"]),
    "array-not-object": (json.dumps([_VALID]), ["expected a JSON object, found an array"]),
    "null-not-object": ("null", ["expected a JSON object, found null"]),
}


class TestEvaluationsCompareExitCodes:
    """0 no regression, 1 regression, 2 the runs could not be compared."""

    def _write(self, tmp_path, name, content):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    @pytest.mark.parametrize("side", ["baseline", "candidate"])
    @pytest.mark.parametrize("case", sorted(_UNREADABLE))
    def test_unreadable_input_is_one_error_line_and_exit_2(self, split_runner, tmp_path, case, side):
        content, must_name = _UNREADABLE[case]
        good = self._write(tmp_path, "good.json", _run_with(pass_rate=1.0, mean=1.0, items=[]).model_dump_json())
        bad = self._write(tmp_path, f"{case}.json", content)
        base, cand = (bad, good) if side == "baseline" else (good, bad)

        result = split_runner.invoke(cli, ["--quiet", "evaluations", "compare", str(base), str(cand)])

        assert result.exit_code == 2, result.output
        assert result.stdout == ""
        lines = result.stderr.splitlines()
        assert len(lines) == 1, result.stderr
        assert lines[0].startswith(f"Error: {side} {bad}"), lines[0]
        for words in must_name:
            assert words in lines[0], lines[0]
        assert "Traceback" not in result.stderr

    def test_a_file_that_is_not_utf8_is_exit_2(self, split_runner, tmp_path):
        good = self._write(tmp_path, "good.json", _run_with(pass_rate=1.0, mean=1.0, items=[]).model_dump_json())
        bad = tmp_path / "latin1.json"
        bad.write_bytes(b'{"id": "caf\xe9"}')

        result = split_runner.invoke(cli, ["--quiet", "evaluations", "compare", str(good), str(bad)])

        assert result.exit_code == 2
        assert result.stderr.splitlines() == [f"Error: candidate {bad} is not UTF-8 text"]

    def test_many_field_errors_still_make_one_line(self, split_runner, tmp_path):
        good = self._write(tmp_path, "good.json", _run_with(pass_rate=1.0, mean=1.0, items=[]).model_dump_json())
        bad = self._write(tmp_path, "empty-object.json", "{}")

        result = split_runner.invoke(cli, ["--quiet", "evaluations", "compare", str(good), str(bad)])

        assert result.exit_code == 2
        (line,) = result.stderr.splitlines()
        assert "id: Field required" in line
        assert "dataset_id: Field required" in line
        assert "dataset_version: Field required" in line
        # Every problem in one go, not one per attempt.
        assert "items: Field required" in line
        assert "aggregate: Field required" in line

    def test_a_run_with_no_items_still_compares(self, split_runner, tmp_path):
        # items: [] is a run of an empty dataset; only leaving the field out is refused.
        base = self._write(tmp_path, "b.json", _VALID_JSON)
        cand = self._write(tmp_path, "c.json", _VALID_JSON)

        result = split_runner.invoke(cli, ["--quiet", "evaluations", "compare", str(base), str(cand)])

        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)["is_regression"] is False

    def test_a_regression_is_still_exit_1_with_the_comparison_on_stdout(self, split_runner, tmp_path):
        base = self._write(
            tmp_path, "b.json", _run_with(pass_rate=1.0, mean=1.0, items=[("a", True)]).model_dump_json()
        )
        cand = self._write(
            tmp_path, "c.json", _run_with(pass_rate=0.0, mean=0.0, items=[("a", False)]).model_dump_json()
        )

        result = split_runner.invoke(cli, ["--quiet", "evaluations", "compare", str(base), str(cand)])

        assert result.exit_code == 1
        assert json.loads(result.stdout)["is_regression"] is True
        assert result.stderr == ""

    def test_an_unexpected_failure_is_exit_2_never_1(self, split_runner, tmp_path, monkeypatch):
        from layerlens.evaluation_runs import RunComparer

        def broken(self, baseline, candidate):
            raise RuntimeError("comparer bug")

        monkeypatch.setattr(RunComparer, "compare", broken)
        run = _run_with(pass_rate=1.0, mean=1.0, items=[]).model_dump_json()
        base = self._write(tmp_path, "b.json", run)
        cand = self._write(tmp_path, "c.json", run)

        result = split_runner.invoke(cli, ["--quiet", "evaluations", "compare", str(base), str(cand)])

        assert result.exit_code == 2
        assert result.stderr.splitlines() == ["Error: unexpected error: comparer bug"]
        assert result.stdout == ""

    def test_the_help_states_the_exit_codes(self, split_runner):
        result = split_runner.invoke(cli, ["--quiet", "evaluations", "compare", "--help"])

        assert result.exit_code == 0
        assert "Exit codes: 0 no regression, 1 regression, 2 the runs could not be compared" in result.stdout


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _last_json_blob(output: str) -> str:
    """Return the last top-level JSON object in the CLI output."""
    stripped = output.strip()
    # Output may include extraneous lines (banner disabled via --quiet, but stderr may
    # still emit messages). Find the outermost JSON object.
    for idx, ch in enumerate(stripped):
        if ch == "{":
            return stripped[idx:]
    raise AssertionError(f"no JSON object found in output: {output!r}")


def _run_with(*, pass_rate: float, mean: float, items):
    from layerlens.evaluation_runs.models import EvaluationRunItem

    return EvaluationRun(
        id="run-" + str(int(pass_rate * 100)),
        dataset_id="d",
        dataset_version=1,
        status=EvaluationRunStatus.COMPLETED,
        items=[EvaluationRunItem(item_id=i, passed=p) for i, p in items],
        aggregate=RunAggregate(mean_scores={"exact": mean}, pass_rate=pass_rate, item_count=len(items)),
    )
