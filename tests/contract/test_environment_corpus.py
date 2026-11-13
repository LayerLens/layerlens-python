"""Parse recorded Automation API replies with the SDK's environment models.

``corpus/environments/`` holds replies recorded from a live atlas-app stack (see the
corpus README). Each must parse as recorded, and again with every key the Go DTOs can
send as null forced to null, one at a time: those are pointer fields with no
``omitempty`` (``run_view.go``, ``build_status.go``, ``jobs_get.go``), and a reply only
shows the states its run happened to reach.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Type, Tuple, Optional
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from layerlens.models import (
    Job,
    ToolList,
    RunResult,
    StateReset,
    Environment,
    SubmitResult,
    EnvironmentBuild,
    ExternalRunStart,
    CreatedEnvironment,
)
from layerlens.models.environment import Task, WorldType, TaskSetSummary
from layerlens.resources.environments._shared import unwrap, unwrap_list, submit_state

CORPUS = Path(__file__).parent / "corpus" / "environments"

_RUN_NULLABLE = ("stop_reason", "error", "agent_name", "label", "set_name", "pass_rate", "cost_usd")
_ATTEMPT_NULLABLE = (
    "attempt_id",
    "task_name",
    "task_version",
    "verdict",
    "grader",
    "evidence",
    "error_source",
    "agent_answer",
    "cost_usd",
    "trace_id",
    "agent_trace_id",
)
_JOB_NULLABLE = ("stop_reason", "finished_at", "progress", "cost_usd", "error", "result")
_BUILD_NULLABLE = ("version", "tools", "records", "answer_key", "refused_because")

# fixture -> (model, key the model sits under in data or None, list key or None, nullable keys, nullable row keys)
FIXTURES: Dict[str, Tuple[Type[BaseModel], Optional[str], Optional[str], Tuple[str, ...], Tuple[str, ...]]] = {
    "list": (Environment, None, "environments", (), ()),
    "get": (Environment, "environment", None, (), ()),
    "create": (CreatedEnvironment, None, None, ("job",), ()),
    "create_reused": (CreatedEnvironment, None, None, ("job",), ()),
    "job_build": (Job, None, None, _JOB_NULLABLE, ()),
    "job_generation": (Job, None, None, _JOB_NULLABLE, ()),
    "build_ready": (EnvironmentBuild, None, None, _BUILD_NULLABLE, ()),
    "tools": (ToolList, None, None, (), ()),
    "task_sets": (TaskSetSummary, None, "task_sets", (), ()),
    "task_set_page": (Task, None, "tasks", (), ()),
    "run_external": (RunResult, None, None, _RUN_NULLABLE, _ATTEMPT_NULLABLE),
    "run_rollout": (RunResult, None, None, _RUN_NULLABLE, _ATTEMPT_NULLABLE),
    "run_rollout_cancelled": (RunResult, None, None, _RUN_NULLABLE, _ATTEMPT_NULLABLE),
    "run_external_unsubmitted": (RunResult, None, None, _RUN_NULLABLE, _ATTEMPT_NULLABLE),
    "external_run_finished": (RunResult, None, None, _RUN_NULLABLE, _ATTEMPT_NULLABLE),
    "world_types": (WorldType, None, "world_types", (), ()),
    "state_reset": (StateReset, None, None, (), ()),
    "external_run_start": (ExternalRunStart, None, None, (), ()),
    "submit": (SubmitResult, None, None, (), ()),
}


def _load(name: str) -> Any:
    return json.loads((CORPUS / f"{name}.json").read_text())["response"]


def _parse(name: str, body: Any) -> List[BaseModel]:
    model, key, list_key, _, _ = FIXTURES[name]
    response = httpx.Response(200, json=body, request=httpx.Request("GET", "https://api.test"))
    if list_key is not None:
        return list(unwrap_list(response, model, list_key))
    return [unwrap(response, model, key=key)]


def test_every_fixture_in_the_corpus_is_checked() -> None:
    assert {p.stem for p in CORPUS.glob("*.json")} == set(FIXTURES)


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_parses_as_recorded(name: str) -> None:
    assert _parse(name, _load(name))


def _forced_nulls() -> List[Tuple[str, str, bool]]:
    cases = []
    for name, (_, _, _, keys, row_keys) in FIXTURES.items():
        cases.extend((name, key, False) for key in keys)
        cases.extend((name, key, True) for key in row_keys)
    return cases


@pytest.mark.parametrize("name, key, in_rows", _forced_nulls())
def test_parses_with_a_nullable_key_null(name: str, key: str, in_rows: bool) -> None:
    body = copy.deepcopy(_load(name))
    if in_rows:
        assert body["data"]["attempts"], f"{name} has no attempts to null {key} in"
        for attempt in body["data"]["attempts"]:
            attempt[key] = None
    else:
        body["data"][key] = None
    (parsed,) = _parse(name, body)
    assert parsed is not None


def test_the_corpus_holds_a_run_with_an_unsubmitted_task() -> None:
    (run,) = _parse("run_external_unsubmitted", _load("run_external_unsubmitted"))
    assert isinstance(run, RunResult)
    unsubmitted = [a for a in run.attempts if a.evidence == "not submitted"]
    assert unsubmitted and all(a.agent_answer is None and a.verdict == "error" for a in unsubmitted)


def test_the_start_fixture_carries_no_real_attempt_token() -> None:
    (start,) = _parse("external_run_start", _load("external_run_start"))
    assert isinstance(start, ExternalRunStart)
    assert all(a.token.endswith("m" * 43) for a in start.attempts)


def test_the_corpus_is_recorded_from_a_composite() -> None:
    # Every environment is one since layerlens-app#2881: a corpus of a minted world would no
    # longer be what the API sends.
    (created,) = _parse("create", _load("create"))
    assert isinstance(created, CreatedEnvironment) and created.environment.type == "composite"
    (environment,) = _parse("get", _load("get"))
    assert isinstance(environment, Environment) and environment.type == "composite"
    member = next(e for e in _parse("list", _load("list")) if isinstance(e, Environment) and e.type != "composite")
    assert member.name == f"{environment.name} — Zendesk"
    (tools,) = _parse("tools", _load("tools"))
    assert isinstance(tools, ToolList) and tools.tools
    assert all(t.name.startswith("zendesk__") and not t.method and not t.path for t in tools.tools)
    (job,) = _parse("job_build", _load("job_build"))
    assert isinstance(job, Job) and job.kind == "build_setup"


def test_a_task_set_page_tells_where_the_next_starts() -> None:
    data = _load("task_set_page")["data"]
    assert data["next_offset"] is None
    assert len(_parse("task_set_page", _load("task_set_page"))) == data["total"]


@pytest.mark.parametrize(
    "name, task_name, graded",
    [
        ("run_external", "ticket-count", True),
        ("run_external", "agent-crash", True),
        ("run_external", "slow", True),
        ("run_external_unsubmitted", "wrong-answer", False),
    ],
)
def test_a_finished_run_tells_a_graded_error_from_a_task_never_submitted(
    name: str, task_name: str, graded: bool
) -> None:
    (run,) = _parse(name, _load(name))
    assert isinstance(run, RunResult) and run.finished
    row = next(a for a in run.attempts if a.task_name == task_name)
    assert row.attempt_id is not None
    state = submit_state(run, row.attempt_id)
    assert state.graded is graded and state.closed is not graded
