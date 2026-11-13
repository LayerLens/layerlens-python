"""CLI entry points for local dataset-driven evaluation runs."""

from __future__ import annotations

import sys
import json
import math
import traceback
from typing import Any, Dict, List, Tuple, Union, cast

import click
from pydantic import ValidationError

from .._command import LayerLensGroup
from ...datasets import Dataset, DatasetVisibility, InMemoryDatasetStore
from .._safe_loader import load_callable as _load_callable
from ...evaluation_runs import RunComparer, EvaluationRunner
from ...evaluation_runs.models import ScorerFn, EvaluationRun


@click.group(cls=LayerLensGroup)
def evaluations() -> None:
    """Run and compare dataset-scoped evaluations locally."""


@evaluations.command("run")
@click.option("--dataset-id", required=True)
# Required: the run is local and reads its dataset from this file; there is no remote lookup.
@click.option(
    "--dataset-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Load a dataset from a JSON file: a list of {input, expected_output} items, each with an optional id "
    "(an item without one is item_0, item_1, ... by its place).",
)
@click.option("--target", required=True, help="Callable 'module:attr'.")
@click.option(
    "--scorer",
    "scorers",
    multiple=True,
    help="Scorer 'name=module:attr' (repeatable). Default: 'exact' equality.",
)
@click.option(
    "--out",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Write the run to this JSON file (default: stdout).",
)
def run(
    dataset_id: str,
    dataset_file: str,
    target: str,
    scorers: tuple,
    out: str | None,
) -> None:
    """Execute an evaluation run and print the aggregated results."""
    store = InMemoryDatasetStore()
    with open(dataset_file, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    ds = store.create(Dataset(id=dataset_id, name=dataset_id, visibility=DatasetVisibility.PRIVATE))
    store.import_items(ds.id, raw)

    target_fn = _load_callable(target)
    scorer_map: Dict[str, ScorerFn] = {}
    if scorers:
        for spec in scorers:
            if "=" not in spec:
                raise click.BadParameter(f"expected name=module:attr (got {spec!r})")
            name, fn_spec = spec.split("=", 1)
            scorer_map[name] = cast(ScorerFn, _load_callable(fn_spec, param_hint="--scorer"))
    else:

        def _exact(actual: Any, expected: Any, _meta: Any) -> float:
            return 1.0 if actual == expected else 0.0

        scorer_map["exact"] = _exact

    run_obj = EvaluationRunner(store).run(dataset_id=ds.id, target=target_fn, scorers=scorer_map)
    payload = run_obj.model_dump_json(indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(payload)
    else:
        sys.stdout.write(payload + "\n")


# `evaluations compare` exits as diff(1) does, so that a CI step can tell "the candidate
# regressed" from "this step is broken" by the exit code alone (LAY-4429).
EXIT_NO_REGRESSION = 0
EXIT_REGRESSION = 1
EXIT_NOT_COMPARED = 2

# At most this many refused fields are named; the rest are counted.
_MAX_FIELD_ERRORS = 5

# What compare decides a regression from. The model fills these in when a file leaves them
# out (no items, a pass rate of 0), so such a file would compare as a run that scored
# nothing: a regression as the candidate, a pass as the baseline. `run --out` writes them all.
_COMPARED_FIELDS = ("items", "aggregate")
_COMPARED_AGGREGATE_FIELDS = ("mean_scores", "pass_rate")

_JSON_TYPE_NAMES = {
    list: "an array",
    str: "a string",
    bool: "a boolean",
    int: "a number",
    float: "a number",
    type(None): "null",
}


class NotCompared(click.ClickException):
    """The runs could not be compared. click prints it as one ``Error:`` line, and exits 2."""

    exit_code = EXIT_NOT_COMPARED


class _NotAJSONNumber(ValueError):
    """A number Python's json reads and JSON has no way to write: NaN, Infinity, 1e999."""


def _json_constant(name: str) -> float:
    raise _NotAJSONNumber(f"{name} is not a JSON number")


def _json_float(text: str) -> float:
    value = float(text)
    if math.isinf(value):
        raise _NotAJSONNumber(f"{text} is too large for a number")
    return value


def _load_run(role: str, path: str) -> EvaluationRun:
    """The evaluation run saved at ``path``, or :class:`NotCompared` saying which file is
    wrong (``role`` is ``baseline`` or ``candidate``) and what in it."""
    where = f"{role} {path}"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            # JSON has no NaN or Infinity; let one through and it decides the comparison
            # alone: a NaN pass rate, for one, is never a regression.
            raw = json.load(fh, parse_constant=_json_constant, parse_float=_json_float)
    except UnicodeDecodeError:
        raise NotCompared(f"{where} is not UTF-8 text") from None
    except json.JSONDecodeError as err:
        raise NotCompared(f"{where} is not valid JSON: {err.msg} (line {err.lineno}, column {err.colno})") from None
    except _NotAJSONNumber as err:
        raise NotCompared(f"{where} is not valid JSON: {err}") from None
    except OSError as err:
        raise NotCompared(f"{where} cannot be read: {err.strerror or err}") from None
    if not isinstance(raw, dict):
        found = _JSON_TYPE_NAMES.get(type(raw), type(raw).__name__)
        raise NotCompared(f"{where} is not an evaluation run: expected a JSON object, found {found}")
    left_out = [f"{name}: Field required" for name in _left_out(raw)]
    try:
        run = EvaluationRun.model_validate(raw)
    except ValidationError as err:
        raise NotCompared(f"{where} is not an evaluation run: {_listed([*_refusals(err), *left_out])}") from None
    if left_out:
        raise NotCompared(f"{where} is not an evaluation run: {_listed(left_out)}")
    return run


def _left_out(raw: Dict[str, Any]) -> List[str]:
    """The fields compare reads that ``raw`` leaves out, for the model to fill in."""
    missing = [name for name in _COMPARED_FIELDS if name not in raw]
    aggregate = raw.get("aggregate")
    if isinstance(aggregate, dict):
        missing += [f"aggregate.{name}" for name in _COMPARED_AGGREGATE_FIELDS if name not in aggregate]
    return missing


def _refusals(err: ValidationError) -> List[str]:
    """Each refused field and why: ``aggregate.pass_rate: Input should be a valid number``."""
    return [f"{_field_path(e['loc'])}: {e['msg']}" for e in err.errors()]


def _listed(refusals: List[str]) -> str:
    """The refusals on one line: the first ``_MAX_FIELD_ERRORS``, and a count of the rest."""
    named = "; ".join(refusals[:_MAX_FIELD_ERRORS])
    more = len(refusals) - _MAX_FIELD_ERRORS
    return f"{named}; and {more} more" if more > 0 else named


def _field_path(loc: Tuple[Union[int, str], ...]) -> str:
    """``("items", 0, "item_id")`` as ``items[0].item_id``."""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else part
    return path


@evaluations.command("compare")
@click.argument("baseline", type=click.Path(exists=True, dir_okay=False))
@click.argument("candidate", type=click.Path(exists=True, dir_okay=False))
@click.option("--score-tolerance", type=float, default=0.02)
@click.pass_context
def compare(ctx: click.Context, baseline: str, candidate: str, score_tolerance: float) -> None:
    """Compare two saved evaluation runs, and exit 1 if the candidate regressed.

    BASELINE and CANDIDATE are runs saved by `layerlens evaluations run --out FILE`.
    The comparison goes to stdout as JSON.

    \b
    Exit codes: 0 no regression, 1 regression, 2 the runs could not be compared
    (a file is not valid JSON or not an evaluation run; the error names the file and field).
    """
    try:
        base_run = _load_run("baseline", baseline)
        cand_run = _load_run("candidate", candidate)
        comparison = RunComparer(score_tolerance=score_tolerance).compare(base_run, cand_run)
    except NotCompared:
        raise
    except Exception as err:
        # Never exit 1 for anything but a regression, or a CI step would read a bug as one.
        if ctx.obj.get("verbose"):
            traceback.print_exc()
        raise NotCompared(f"unexpected error: {err}") from err

    click.echo(comparison.model_dump_json(indent=2))
    if comparison.is_regression:
        sys.exit(EXIT_REGRESSION)
