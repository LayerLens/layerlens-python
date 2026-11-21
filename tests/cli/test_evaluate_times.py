"""N4: ``layerlens evaluate get`` printed its times as numbers no one could read.

Submitted At and Finished At came out as Unix seconds (``1794011871``) and Average Duration as
nanoseconds (``1486055168``) with no unit; ``evaluate list`` showed the same seconds under
Submitted. A table now shows a UTC date and a duration with its unit. ``--format json`` still
gives the API's numbers, which scripts read.
"""

from __future__ import annotations

import json
from typing import Any, List
from unittest.mock import patch

import httpx
import pytest
from click.testing import Result

from layerlens import LayerLens
from layerlens.cli._app import cli

from .conftest import _make_runner

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
EVAL_ID = "67e1edb271742ad0740b9b97"


def _evaluation(**fields: Any) -> dict:
    """An evaluation as GET /evaluations/{id} and the list answer it (trimmed)."""
    return {
        "id": EVAL_ID,
        "status": "success",
        "status_description": "",
        "submitted_at": 1742859698,
        "finished_at": 1742862257,
        "model_id": "67e09b8ee014f9fa7157ada7",
        "model_name": "DeepSeek V3",
        "dataset_id": "67e09b8ee014f9fa7157adab",
        "dataset_name": "Human Evaluation",
        "average_duration": 14572662136,
        "accuracy": 87.19512,
        **fields,
    }


class _API:
    def __init__(self, evaluation: dict) -> None:
        self.evaluation = evaluation
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == f"/api/v1/evaluations/{EVAL_ID}":
            return httpx.Response(200, json=self.evaluation)
        if request.url.path == "/api/v1/evaluations":
            return httpx.Response(200, json={"evaluations": [self.evaluation], "count": 1, "total_count": 1})
        return httpx.Response(404, text="404 page not found")


def _invoke(evaluation: dict, *args: str) -> Result:
    def client(ctx: Any) -> LayerLens:
        made = LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
        made.organization_id, made.project_id = ORG, PROJECT
        made._transport = httpx.MockTransport(_API(evaluation))
        return made

    with patch("layerlens.cli.commands.evaluate.get_client", client):
        return _make_runner().invoke(cli, ["-q", *args], env={"LAYERLENS_API_KEY": "test-key"})


def _field(output: str, label: str) -> str:
    line = next(ln for ln in output.splitlines() if ln.startswith(label + " "))
    return line[len(label) :].strip()


def test_evaluate_get_shows_dates_and_a_duration_with_its_unit() -> None:
    result = _invoke(_evaluation(), "evaluate", "get", EVAL_ID)
    assert result.exit_code == 0, result.output
    assert _field(result.stdout, "Submitted At") == "2025-03-24 23:41:38 UTC"
    assert _field(result.stdout, "Finished At") == "2025-03-25 00:24:17 UTC"
    # The average time a prompt took: 14,572,662,136 ns.
    assert _field(result.stdout, "Average Duration") == "14.57 s"


@pytest.mark.parametrize(
    ("nanoseconds", "shown"),
    [(850_000_000, "850 ms"), (1_486_055_168, "1.49 s"), (125_400_000_000, "2 min 5 s"), (0, "-")],
)
def test_the_duration_reads_in_the_unit_that_fits(nanoseconds: int, shown: str) -> None:
    result = _invoke(_evaluation(average_duration=nanoseconds), "evaluate", "get", EVAL_ID)
    assert result.exit_code == 0, result.output
    assert _field(result.stdout, "Average Duration") == shown


def test_an_evaluation_still_running_has_no_finish_time() -> None:
    result = _invoke(_evaluation(status="in-progress", finished_at=0, average_duration=0), "evaluate", "get", EVAL_ID)
    assert result.exit_code == 0, result.output
    assert _field(result.stdout, "Finished At") == "-"
    assert _field(result.stdout, "Submitted At") == "2025-03-24 23:41:38 UTC"


def test_evaluate_list_shows_the_submit_date_and_json_keeps_the_numbers() -> None:
    table = _invoke(_evaluation(), "evaluate", "list")
    assert table.exit_code == 0, table.output
    assert "2025-03-24 23:41:38 UTC" in table.stdout and "1742859698" not in table.stdout
    listed = _invoke(_evaluation(), "--format", "json", "evaluate", "list")
    got = _invoke(_evaluation(), "--format", "json", "evaluate", "get", EVAL_ID)
    assert listed.exit_code == 0 and got.exit_code == 0, listed.output + got.output
    row, one = json.loads(listed.stdout)[0], json.loads(got.stdout)
    assert (row["submitted_at"], one["finished_at"], one["average_duration"]) == (1742859698, 1742862257, 14572662136)
