from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from layerlens import LayerLensError, EnvironmentBuildError, EnvironmentConflictError
from layerlens.models import TaskSpec, RunResult, Environment, ExternalRunStart, CreatedEnvironment
from layerlens._exceptions import (
    ConflictError,
    NotFoundError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    PermissionDeniedError,
    APIResponseValidationError,
)
from layerlens.resources.environments.runs import external_run_body
from layerlens.resources.environments._shared import (
    BUSY_RETRY_SECONDS,
    MAX_ESCAPED_BODY_BYTES,
    MAX_REQUEST_BODY_BYTES,
    InvalidArgument,
)

from .conftest import (
    ENV_ID,
    RUN_ID,
    ATTEMPT_1,
    ATTEMPT_2,
    FakeAPI,
    ok,
    err,
    row,
    body_of,
    run_view,
    submitted,
    start_body,
    environment,
    make_client,
)

CREATED = {
    "environment": {"id": ENV_ID, "name": "support-desk", "type": "zendesk", "current_version": 0},
    "job": {"id": "6a8c6a45b1c2d3e4f5a6b7c8", "status": "pending"},
}


def building(**overrides: object) -> dict:
    return {
        "status": "building",
        "version": None,
        "steps": [{"name": "generate world", "state": "running", "detail": ""}],
        "tools": None,
        "records": None,
        "answer_key": None,
        "refused_because": None,
        **overrides,
    }


class TestTransport:
    def test_a_login_session_cannot_reach_the_automation_api(self, api: FakeAPI) -> None:
        client = make_client(api, use_bearer_auth=True)
        with pytest.raises(LayerLensError, match="needs an API key with Automation access"):
            client.environments  # noqa: B018

    def test_requests_go_to_the_agent_api_path_form_with_the_key(self, client, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}", ok({"environment": environment()}))
        client.environments.get(ENV_ID)
        request = api.requests[-1]
        assert request.url.path.startswith("/api/v1/env/organizations/")
        assert request.headers["x-api-key"] == "test-key"

    def test_a_reply_outside_the_envelope_raises_instead_of_returning_nothing(self, client, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}", (200, {"environment": environment()}))
        with pytest.raises(APIResponseValidationError, match="success envelope"):
            client.environments.get(ENV_ID)

    def test_a_reply_that_does_not_fit_the_model_raises(self, client, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}", ok({"environment": {"id": ENV_ID}}))
        with pytest.raises(APIResponseValidationError, match="does not fit Environment"):
            client.environments.get(ENV_ID)

    def test_an_unknown_field_from_a_newer_api_is_ignored(self, client, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}", ok({"environment": environment(added_later="x")}))
        assert client.environments.get(ENV_ID).name == "support-desk"


class TestResolve:
    def test_an_id_is_used_as_is(self, client, api: FakeAPI) -> None:
        assert client.environments.resolve(ENV_ID) == ENV_ID
        assert api.requests == []

    def test_a_name_is_looked_up_by_exact_name(self, client, api: FakeAPI) -> None:
        api.on("GET", "", ok({"environments": [environment()], "count": 1, "total_count": 1}))
        assert client.environments.resolve("support-desk") == ENV_ID
        assert api.requests[0].url.params["name"] == "support-desk"

    def test_an_unknown_name_says_so(self, client, api: FakeAPI) -> None:
        api.on("GET", "", ok({"environments": [], "count": 0, "total_count": 0}))
        with pytest.raises(LayerLensError, match="no environment named 'support-desk'"):
            client.environments.resolve("support-desk")

    def test_two_environments_with_one_name_ask_for_the_id(self, client, api: FakeAPI) -> None:
        twins = [environment(), environment(id="6adcba8583c75b47f74ebd09")]
        api.on("GET", "", ok({"environments": twins, "count": 2, "total_count": 2}))
        with pytest.raises(LayerLensError, match="more than one environment is named"):
            client.environments.resolve("support-desk")


class TestList:
    def test_every_page_is_read(self, client, api: FakeAPI) -> None:
        first = [environment(id=f"{i:024x}") for i in range(500)]
        second = [environment(id=f"{i + 500:024x}") for i in range(3)]
        api.on(
            "GET",
            "",
            ok({"environments": first, "count": 500, "total_count": 503}),
            ok({"environments": second, "count": 3, "total_count": 503}),
        )
        found = client.environments.list()
        assert len(found) == 503
        assert [r.url.params["page"] for r in api.requests] == ["1", "2"]
        assert api.requests[0].url.params["limit"] == "500"

    def test_members_of_composites_can_be_left_out(self, client, api: FakeAPI) -> None:
        api.on("GET", "", ok({"environments": [environment()], "count": 1, "total_count": 1}))
        client.environments.list(hide_members=True)
        client.environments.list()
        assert api.requests[0].url.params["hide_members"] == "true"
        assert "hide_members" not in api.requests[1].url.params


class TestCreate:
    def test_sends_the_inputs_and_an_idempotency_key(self, client, api: FakeAPI) -> None:
        api.on("POST", "/worlds", ok(CREATED, 201))
        created = client.environments.create(
            "support-desk", "zendesk", description="A desk", config={"n_tickets": 40}, if_not_exists=True
        )
        assert isinstance(created, CreatedEnvironment)
        assert created.id == ENV_ID and created.job is not None and not created.reused
        request = api.requests[0]
        assert body_of(request) == {
            "name": "support-desk",
            "type": "zendesk",
            "description": "A desk",
            "config": {"n_tickets": 40},
            "if_not_exists": True,
        }
        assert request.headers["idempotency-key"].startswith("layerlens-sdk-")

    def test_a_retried_create_sends_the_same_idempotency_key(self, client, api: FakeAPI) -> None:
        busy = err(503, "too many jobs are being started at once; try again", {"Retry-After": "1"})
        api.on("POST", "/worlds", busy, ok(CREATED, 201))
        client.environments.create("support-desk", "zendesk", description="A desk")
        keys = {r.headers["idempotency-key"] for r in api.calls("POST", "/worlds")}
        assert len(api.calls("POST", "/worlds")) == 2 and len(keys) == 1

    def test_two_creates_send_different_keys(self, client, api: FakeAPI) -> None:
        api.on("POST", "/worlds", ok(CREATED, 201))
        client.environments.create("a", "zendesk", description="A desk")
        client.environments.create("b", "zendesk", description="A desk")
        assert len({r.headers["idempotency-key"] for r in api.requests}) == 2

    def test_a_reused_environment_says_so(self, client, api: FakeAPI) -> None:
        api.on("POST", "/worlds", ok({**CREATED, "reused": True}))
        assert client.environments.create("support-desk", "zendesk", description="A desk", if_not_exists=True).reused

    def test_a_blank_description_is_refused_before_sending(self, client, api: FakeAPI) -> None:
        with pytest.raises(ValueError, match="description"):
            client.environments.create("support-desk", "zendesk", description=" \n")
        with pytest.raises(ValueError, match="description"):
            client.environments.create("support-desk", "zendesk", description=None)  # type: ignore[arg-type]
        assert api.requests == []

    def test_a_conflict_that_is_not_about_inputs_says_why_in_its_message(self, client, api: FakeAPI) -> None:
        sentence = '"d" needs a new build, and its apps are built together as a setup: remove it and create it again'
        api.on("POST", "/worlds", (409, {"code": "conflict", "message": sentence, "request_id": "r1"}))
        with pytest.raises(EnvironmentConflictError) as caught:
            client.environments.create("d", "linear", description="A desk", if_not_exists=True)
        assert str(caught.value) == sentence and caught.value.differences == []

    def test_settings_an_agent_built_world_does_not_use_are_named(self, client, api: FakeAPI) -> None:
        note = "This world is built by an agent from the organization its description is read into, so the settings and seed sent with it are not used."
        api.on("POST", "/worlds", ok({**CREATED, "unused": note}, 201))
        created = client.environments.create("tracker", "jira", description="A desk", config={"n_issues": 40})
        assert created.unused == note

    def test_unused_is_none_when_every_setting_is_used(self, client, api: FakeAPI) -> None:
        api.on("POST", "/worlds", ok(CREATED, 201))
        assert client.environments.create("support-desk", "zendesk", description="A desk").unused is None

    def test_different_inputs_raise_a_conflict_naming_them(self, client, api: FakeAPI) -> None:
        conflict = {
            "status": "error",
            "error": 'an environment named "support-desk" already exists in this project, created from a different description and config',
            "differences": ["description", "config"],
        }
        api.on("POST", "/worlds", (409, conflict))
        with pytest.raises(EnvironmentConflictError) as caught:
            client.environments.create("support-desk", "zendesk", description="A desk", if_not_exists=True)
        assert caught.value.differences == ["description", "config"]
        assert caught.value.project_id is None
        assert "created from a different description" in str(caught.value)
        assert isinstance(caught.value, ConflictError)

    def test_a_name_taken_in_another_project_names_that_project(self, client, api: FakeAPI) -> None:
        body = {"status": "error", "error": "an environment named X already exists in project p2", "project_id": "p2"}
        api.on("POST", "/worlds", (409, body))
        with pytest.raises(EnvironmentConflictError) as caught:
            client.environments.create("support-desk", "zendesk", description="A desk", if_not_exists=True)
        assert caught.value.project_id == "p2" and caught.value.differences == []


class TestWaitUntilReady:
    def test_returns_the_environment_once_ready(self, client, api: FakeAPI) -> None:
        ready = building(status="ready", version=1, tools=18, steps=[{"name": "build", "state": "done", "detail": ""}])
        api.on("GET", f"/{ENV_ID}/build", ok(building()), ok(building()), ok(ready))
        api.on("GET", f"/{ENV_ID}", ok({"environment": environment()}))
        found = client.environments.wait_until_ready(ENV_ID, poll_interval_seconds=0.01)
        assert isinstance(found, Environment)
        assert len(api.calls("GET", f"/{ENV_ID}/build")) == 3

    def test_a_failed_build_raises_with_the_generators_reason(self, client, api: FakeAPI) -> None:
        api.on(
            "GET", f"/{ENV_ID}/build", ok(building(status="failed", refused_because="a stripe world holds no tickets"))
        )
        with pytest.raises(EnvironmentBuildError, match="holds no tickets") as caught:
            client.environments.wait_until_ready(ENV_ID)
        assert caught.value.refused_because == "a stripe world holds no tickets"
        assert caught.value.environment_id == ENV_ID

    def test_a_build_still_running_at_the_deadline_times_out(self, client, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}/build", ok(building()))
        with pytest.raises(TimeoutError, match="still building"):
            client.environments.wait_until_ready(ENV_ID, timeout_seconds=0)

    def test_a_server_error_while_polling_is_ridden_out(self, client, api: FakeAPI) -> None:
        gateway = (504, {"message": "Endpoint request timed out"})
        api.on("GET", f"/{ENV_ID}/build", gateway, gateway, gateway, ok(building(status="ready", version=1)))
        api.on("GET", f"/{ENV_ID}", ok({"environment": environment()}))
        client.environments.wait_until_ready(ENV_ID, poll_interval_seconds=0.01)

    def test_a_refusal_while_polling_is_raised_at_once(self, client, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}/build", err(403, "this key has Standard access"))
        with pytest.raises(PermissionDeniedError):
            client.environments.wait_until_ready(ENV_ID)
        assert len(api.requests) == 1


class TestRevertAndDelete:
    def test_reverting_to_the_version_already_served_is_not_an_error(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/revert", err(409, "version 3 is already what this environment serves"))
        api.on("GET", f"/{ENV_ID}", ok({"environment": environment(current_version=3)}))
        assert client.environments.revert(ENV_ID, to_version=3).current_version == 3

    def test_reverting_to_a_version_that_does_not_exist_raises(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/revert", err(409, "this environment has no version 9"))
        api.on("GET", f"/{ENV_ID}", ok({"environment": environment(current_version=3)}))
        with pytest.raises(ConflictError, match="no version 9"):
            client.environments.revert(ENV_ID, to_version=9)

    def test_an_environment_gone_by_the_time_it_is_deleted_is_deleted(self, client, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}", ok({"environment": environment()}))
        api.on("DELETE", f"/{ENV_ID}", err(404, "environment not found"))
        client.environments.delete(ENV_ID)

    def test_a_delete_no_route_answers_is_an_error(self, client, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}", ok({"environment": environment()}))
        api.on("DELETE", f"/{ENV_ID}", (404, "404 page not found"))
        with pytest.raises(NotFoundError):
            client.environments.delete(ENV_ID)

    def test_deleting_an_id_that_never_existed_raises_without_a_delete(self, client, api: FakeAPI) -> None:
        api.on("GET", f"/{ENV_ID}", err(404, "environment not found"))
        with pytest.raises(NotFoundError):
            client.environments.delete(ENV_ID)
        assert api.calls("DELETE", f"/{ENV_ID}") == []


class TestWorldTypes:
    def test_an_unreachable_generator_raises_instead_of_listing_nothing(self, client, api: FakeAPI) -> None:
        unavailable = "the world generator could not be reached, so no world types can be listed right now"
        api.on("GET", "/world-types", ok({"world_types": [], "count": 0, "unavailable": unavailable}))
        with pytest.raises(LayerLensError, match="could not be reached"):
            client.environments.world_types()

    def test_knobs_parse_with_their_json_schema_names(self, client, api: FakeAPI) -> None:
        knob = {"type": "integer", "minimum": 1, "maximum": 500, "default": 40, "minLength": 2}
        world_type = {"type": "zendesk", "title": "Zendesk", "config_schema": {"n_tickets": knob}, "tool_count": 18}
        api.on("GET", "/world-types", ok({"world_types": [world_type], "count": 1}))
        (found,) = client.environments.world_types()
        assert found.config_schema["n_tickets"].maximum == 500
        assert found.config_schema["n_tickets"].min_length == 2

    def test_a_type_an_agent_builds_says_so(self, client, api: FakeAPI) -> None:
        linear = {
            "type": "linear",
            "title": "Linear",
            "tool_count": 30,
            "agent_slice": True,
            "org_knobs": {"counts": {"issues": "n_issues"}, "app": "linear"},
        }
        sliced_without_an_app = {"type": "edgar", "agent_slice": True, "org_knobs": {"counts": {}, "app": " "}}
        api.on("GET", "/world-types", ok({"world_types": [linear, sliced_without_an_app, {"type": "zendesk"}]}))
        found = {t.type: t.built_by_agent for t in client.environments.world_types()}
        assert found == {"linear": True, "edgar": False, "zendesk": False}


class TestTaskSets:
    def test_get_reads_every_page_by_next_offset(self, client, api: FakeAPI) -> None:
        def task(n: int) -> dict:
            return {
                "id": f"{n:024x}",
                "name": f"t{n}",
                "version": 1,
                "prompt": "p",
                "grader": {"kind": "answer", "config": {"expected": "1", "match": "contains"}},
                "retired": False,
                "created_at": "2026-10-26T12:00:00Z",
                "updated_at": "2026-10-26T12:00:00Z",
            }

        api.on(
            "GET",
            f"/{ENV_ID}/task-sets/ci set",
            ok({"set_name": "ci set", "total": 3, "offset": 0, "next_offset": 2, "tasks": [task(0), task(1)]}),
            ok({"set_name": "ci set", "total": 3, "offset": 2, "next_offset": None, "tasks": [task(2)]}),
        )
        task_set = client.environments.task_sets.get(ENV_ID, "ci set")
        assert [t.name for t in task_set.tasks] == ["t0", "t1", "t2"] and task_set.total == 3
        assert [r.url.params["offset"] for r in api.requests] == ["0", "2"]
        assert api.requests[0].url.raw_path.split(b"?")[0].endswith(b"/task-sets/ci%20set")

    def test_a_set_name_with_a_slash_is_refused_before_any_request(self, client, api: FakeAPI) -> None:
        with pytest.raises(ValueError, match="holds no '/'"):
            client.environments.task_sets.get(ENV_ID, "a/b")
        assert api.requests == []

    def test_generate_sends_its_budget_and_a_key(self, client, api: FakeAPI) -> None:
        job = {"id": "j1", "kind": "generate_tasks", "status": "pending", "created_at": "2026-10-26T12:00:00Z"}
        api.on("POST", f"/{ENV_ID}/task-sets/generated/generate", ok(job, 202))
        found = client.environments.task_sets.generate(ENV_ID, "generated", count=20, budget_usd=2.0, target="hard")
        assert found.status == "pending"
        assert body_of(api.requests[0]) == {"count": 20, "budget_usd": 2.0, "target": "hard"}
        assert "idempotency-key" in api.requests[0].headers

    def test_delete_returns_how_many_went(self, client, api: FakeAPI) -> None:
        api.on("DELETE", f"/{ENV_ID}/task-sets/ci", ok({"deleted": 4}))
        assert client.environments.task_sets.delete(ENV_ID, "ci") == 4


class TestTaskChoice:
    def test_a_run_needs_exactly_one_source_of_tasks(self, client, api: FakeAPI) -> None:
        with pytest.raises(ValueError, match="exactly one of set_name, tasks or tasks_file"):
            client.environments.runs.start_external(ENV_ID, agent_name="a")
        with pytest.raises(ValueError, match=r"got set_name, tasks"):
            client.environments.runs.start_external(ENV_ID, agent_name="a", set_name="ci", tasks=[])
        assert api.requests == []

    def test_a_tasks_file_is_sent_inline(self, client, api: FakeAPI, tmp_path: Path) -> None:
        task = {"name": "t", "prompt": "p", "grader": {"kind": "answer", "config": {"expected": "1"}}}
        path = tmp_path / "tasks.jsonl"
        path.write_text(json.dumps(task) + "\n\n" + json.dumps({**task, "name": "u"}) + "\n")
        api.on("POST", f"/{ENV_ID}/external-runs", ok(start_body(ATTEMPT_1), 201))
        client.environments.runs.start_external(ENV_ID, agent_name="a", tasks_file=path, task_names=["u"])
        assert body_of(api.requests[0]) == {
            "tasks": [task, {**task, "name": "u"}],
            "task_names": ["u"],
            "agent_name": "a",
        }

    def test_a_broken_line_names_the_file_and_line(self, client, tmp_path: Path) -> None:
        path = tmp_path / "tasks.jsonl"
        path.write_text('{"name": "t"}\n{oops\n')
        with pytest.raises(ValueError, match=r"tasks.jsonl:2: not a JSON object"):
            client.environments.runs.start_external(ENV_ID, agent_name="a", tasks_file=path)

    def test_task_specs_are_sent_as_their_fields(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/rollout-runs", ok(run_view(row(ATTEMPT_1, None), kind="rollout"), 202))
        spec = TaskSpec(name="t", prompt="p", grader={"kind": "judge", "config": {"rubric": "r"}})
        client.environments.runs.run_model(ENV_ID, budget_usd=1.0, model="openai/gpt-5", tasks=[spec])
        assert body_of(api.requests[0])["tasks"] == [
            {"name": "t", "prompt": "p", "grader": {"kind": "judge", "config": {"rubric": "r"}}}
        ]

    def test_a_model_run_needs_a_model_or_an_endpoint(self, client) -> None:
        with pytest.raises(ValueError, match="needs model, or endpoint"):
            client.environments.runs.run_model(ENV_ID, budget_usd=1.0, set_name="ci")


SUBMIT = f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{ATTEMPT_1}/submit"
RUN = f"/{ENV_ID}/runs/{RUN_ID}"


class TestSubmit:
    def test_an_answer_is_graded_in_one_request(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, ok(submitted(ATTEMPT_1)))
        result = client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "9900001017", trace_id="t1")
        assert result.answered == 1
        assert body_of(api.requests[0]) == {"outcome": "answered", "answer": "9900001017", "trace_id": "t1"}

    def test_a_trace_id_at_the_apis_limit_is_sent(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, ok(submitted(ATTEMPT_1)))
        trace_id = "é" * 128  # 256 bytes, the API's limit; it measures the id trimmed
        client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "x", trace_id=f" {trace_id}\n")
        assert body_of(api.requests[0])["trace_id"] == f" {trace_id}\n"

    def test_a_trace_id_past_the_apis_limit_is_refused_before_sending(self, client, api: FakeAPI) -> None:
        with pytest.raises(InvalidArgument, match="trace_id is 257 bytes; the limit is 256"):
            client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "x", trace_id="é" * 128 + "x")
        assert api.requests == []

    def test_a_second_submit_of_a_graded_attempt_is_a_conflict(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, err(409, f"attempt {ATTEMPT_1}, for task x, is already submitted"))
        api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, "pass"), row(ATTEMPT_2, None), status="in_progress")))
        with pytest.raises(ConflictError, match="already submitted"):
            client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a")
        assert len(api.calls("POST", SUBMIT)) == 1

    def test_a_submit_held_back_by_a_call_in_flight_goes_through_once_it_returns(self, client, api: FakeAPI) -> None:
        held = err(409, "a call to this task is still being served; submit the answer once it returns, or after X")
        api.on("POST", SUBMIT, held, held, ok(submitted(ATTEMPT_1)))
        api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, None), status="in_progress")))
        result = client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, outcome="timeout", error="ran 600s")
        assert result.attempt_id == ATTEMPT_1
        assert len(api.calls("POST", SUBMIT)) == 3

    def test_a_submit_to_a_finished_run_raises_the_apis_conflict(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, err(409, "this run is finished"))
        unsubmitted = row(ATTEMPT_1, "error", agent_answer=None, trace_id=None, cost_usd=None, evidence="not submitted")
        api.on("GET", RUN, ok(run_view(unsubmitted, status="failure", error="no task was answered")))
        with pytest.raises(ConflictError, match="this run is finished"):
            client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a")

    def test_a_held_back_submit_gives_up_at_its_deadline(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, err(409, "an answer to this task is already being graded"))
        api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, None), status="in_progress")))
        with patch("layerlens.resources.environments._shared.SUBMIT_SETTLE_SECONDS", 0):
            with pytest.raises(ConflictError, match="already being graded"):
                client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a")

    def test_a_gateway_timeout_is_settled_from_the_run(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, (504, {"message": "Endpoint request timed out"}), err(409, "already being graded"))
        api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, None), row(ATTEMPT_2, None), status="in_progress")),
               ok(run_view(row(ATTEMPT_1, "pass"), row(ATTEMPT_2, None), status="in_progress")))  # fmt: skip
        result = client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a")
        assert result.attempt_id == ATTEMPT_1 and result.answered == 1 and not result.finished
        assert len(api.calls("POST", SUBMIT)) == 2

    def test_a_submit_that_never_landed_is_sent_again(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, (502, {"message": "Internal server error"}), ok(submitted(ATTEMPT_1)))
        api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, None), status="in_progress")))
        assert client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a").answered == 1

    def test_the_base_clients_own_retries_are_off_for_a_submit(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, (504, {"message": "timeout"}), ok(submitted(ATTEMPT_1)))
        api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, None), status="in_progress")))
        client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a")
        posts_and_gets = [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in api.requests]
        assert posts_and_gets == [("POST", "submit"), ("GET", RUN_ID), ("POST", "submit")]

    def test_a_run_that_ended_without_the_answer_raises(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, (504, {"message": "timeout"}), err(409, "this run is finished"))
        not_submitted = row(
            ATTEMPT_1,
            "error",
            agent_answer=None,
            trace_id=None,
            cost_usd=None,
            error_source="agent",
            evidence="not submitted",
        )
        api.on("GET", RUN, ok(run_view(not_submitted, status="failure", error="no task was answered")))
        with pytest.raises(LayerLensError, match="ended before attempt"):
            client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a")

    def test_the_last_answer_landing_after_a_timeout_is_found_on_the_finished_run(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, (504, {"message": "timeout"}))
        api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, "fail"), status="success")))
        result = client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a")
        assert result.finished and result.answered == 1

    def test_a_graded_error_outcome_that_ended_the_run_is_found_after_a_timeout(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, (504, {"message": "timeout"}))
        crashed = row(
            ATTEMPT_1, "error", agent_answer=None, error_source="agent", evidence="the agent reported an error"
        )
        api.on("GET", RUN, ok(run_view(crashed, status="success")))
        result = client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, outcome="error", error="exit 3")
        assert result.finished

    def test_a_refusal_that_waiting_cannot_cure_is_raised_at_once(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, err(409, "the task x no longer has the version this run opened on"))
        api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, None), status="in_progress")))
        with pytest.raises(ConflictError, match="no longer has the version"):
            client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a")
        assert len(api.calls("POST", SUBMIT)) == 1

    def test_an_answer_the_api_failed_on_is_sent_again_a_bounded_number_of_times(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, err(500, "the answer could not be graded: the judge did not answer"))
        api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, None), status="in_progress")))
        with patch("layerlens.resources.environments._shared.SUBMIT_SETTLE_SECONDS", 0.3):
            with pytest.raises(InternalServerError, match="could not be graded"):
                client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a")
        assert len(api.calls("POST", SUBMIT)) == 4

    def test_a_503_before_any_handler_ran_is_waited_out_and_sent_again(self, client, api: FakeAPI) -> None:
        throttled = err(503, "API key verification is temporarily unavailable, please retry", {"Retry-After": "1"})
        api.on("POST", SUBMIT, throttled, ok(submitted(ATTEMPT_1)))
        assert client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a").attempt_id == ATTEMPT_1
        assert api.calls("GET", RUN) == [] and len(api.calls("POST", SUBMIT)) == 2

    def test_a_refused_connection_never_counts_as_a_submit_that_landed(self, client, api: FakeAPI) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        api.on("POST", SUBMIT, refuse, err(409, "is already submitted"))
        api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, "pass"), status="in_progress")))
        with pytest.raises(ConflictError, match="already submitted"):
            client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a")

    def test_a_settled_submit_on_a_closed_run_counts_only_stored_attempts(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, (504, {"message": "timeout"}))
        unsubmitted = row(ATTEMPT_2, "error", agent_answer=None, trace_id=None, cost_usd=None, evidence="not submitted")
        api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, "pass"), unsubmitted, status="cancelled")))
        result = client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a")
        assert (result.answered, result.total) == (1, 2)

    def test_an_error_outcome_carries_no_answer(self, client, api: FakeAPI) -> None:
        api.on("POST", SUBMIT, ok(submitted(ATTEMPT_1)))
        client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, outcome="timeout", error="ran 600s")
        assert body_of(api.requests[0]) == {"outcome": "timeout", "error": "ran 600s"}

    def test_an_unknown_outcome_is_refused_locally(self, client, api: FakeAPI) -> None:
        with pytest.raises(ValueError, match="outcome must be one of"):
            client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a", outcome="skipped")
        assert api.requests == []


class TestExternalRun:
    def test_the_run_starts_on_entering_and_finishes_on_leaving(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/external-runs", ok(start_body(ATTEMPT_1, ATTEMPT_2), 201))
        api.on("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{ATTEMPT_1}/submit", ok(submitted(ATTEMPT_1)))
        api.on(
            "POST",
            f"/{ENV_ID}/external-runs/{RUN_ID}/finish",
            ok(run_view(row(ATTEMPT_1, "pass"), row(ATTEMPT_2, "error"))),
        )
        run = client.environments.external_run(ENV_ID, agent_name="support-agent", set_name="ci", label="sha")
        assert api.requests == []
        with run:
            assert [a.task_name for a in run.attempts] == ["task-1", "task-2"]
            run.attempts[0].submit("answer")
        assert [r.url.path.rsplit("/", 1)[-1] for r in api.requests] == ["external-runs", "submit", "finish"]
        assert body_of(api.requests[0]) == {"set_name": "ci", "agent_name": "support-agent", "label": "sha"}

    def test_an_exception_in_the_block_cancels_the_run(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/external-runs", ok(start_body(ATTEMPT_1), 201))
        api.on("POST", f"/{ENV_ID}/runs/{RUN_ID}/cancel", ok(run_view(row(ATTEMPT_1, "error"), status="cancelled")))
        with pytest.raises(RuntimeError, match="harness broke"):
            with client.environments.external_run(ENV_ID, agent_name="a", set_name="ci"):
                raise RuntimeError("harness broke")
        assert api.requests[-1].url.path.endswith("/cancel")
        assert api.calls("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/finish") == []

    def test_a_cancel_that_fails_does_not_hide_the_original_error(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/external-runs", ok(start_body(ATTEMPT_1), 201))
        api.on("POST", f"/{ENV_ID}/runs/{RUN_ID}/cancel", err(409, "already finished"))
        with pytest.raises(RuntimeError, match="harness broke"):
            with client.environments.external_run(ENV_ID, agent_name="a", set_name="ci"):
                raise RuntimeError("harness broke")

    def test_an_attempt_is_submitted_once(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/external-runs", ok(start_body(ATTEMPT_1), 201))
        api.on("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{ATTEMPT_1}/submit", ok(submitted(ATTEMPT_1)))
        run = client.environments.external_run(ENV_ID, agent_name="a", set_name="ci").start()
        attempt = run.attempts[0]
        attempt.submit("a")
        with pytest.raises(LayerLensError, match="already submitted"):
            attempt.submit_error("again")
        assert attempt.submitted
        assert len(api.calls("POST", f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{ATTEMPT_1}/submit")) == 1

    def test_an_attempts_repr_keeps_its_token_out_of_logs(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/external-runs", ok(start_body(ATTEMPT_1), 201))
        attempt = client.environments.external_run(ENV_ID, agent_name="a", set_name="ci").start().attempts[0]
        assert attempt.token.startswith("llat_v1_") and attempt.token not in repr(attempt)

    def test_attempts_before_the_start_say_how_to_start(self, client) -> None:
        run = client.environments.external_run(ENV_ID, agent_name="a", set_name="ci")
        with pytest.raises(LayerLensError, match="has not started"):
            run.attempts  # noqa: B018

    def test_a_restarted_run_replays_the_same_idempotency_key(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/external-runs", (502, {"message": "bad gateway"}), ok(start_body(ATTEMPT_1), 201))
        client._max_retries = 0
        run = client.environments.external_run(ENV_ID, agent_name="a", set_name="ci")
        with pytest.raises(InternalServerError):
            run.start()
        assert not run.started
        run.start()
        keys = [r.headers["idempotency-key"] for r in api.calls("POST", f"/{ENV_ID}/external-runs")]
        assert len(keys) == 2 and keys[0] == keys[1]

    def test_wait_returns_the_finished_run(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/external-runs", ok(start_body(ATTEMPT_1), 201))
        api.on(
            "GET", RUN, ok(run_view(row(ATTEMPT_1, None), status="in_progress")), ok(run_view(row(ATTEMPT_1, "pass")))
        )
        run = client.environments.external_run(ENV_ID, agent_name="a", set_name="ci").start()
        result = run.wait(poll_interval_seconds=0.01)
        assert isinstance(result, RunResult) and result.passed == 1


class TestRuns:
    def test_wait_times_out_on_a_run_still_going(self, client, api: FakeAPI) -> None:
        api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, None), status="in_progress")))
        with pytest.raises(TimeoutError, match="still in_progress"):
            client.environments.runs.wait(ENV_ID, RUN_ID, timeout_seconds=0)

    def test_a_model_run_sends_its_fields(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/rollout-runs", ok(run_view(row(ATTEMPT_1, None), kind="rollout"), 202))
        client.environments.runs.run_model(
            ENV_ID,
            budget_usd=5.0,
            endpoint={"base_url": "https://agents.example.com/v1", "model": "m", "api_key": "k"},
            set_name="ci",
            max_steps=20,
            label="sha",
        )
        assert body_of(api.requests[0]) == {
            "set_name": "ci",
            "budget_usd": 5.0,
            "endpoint": {"base_url": "https://agents.example.com/v1", "model": "m", "api_key": "k"},
            "label": "sha",
            "max_steps": 20,
        }

    def test_failures_are_the_graded_attempts_that_did_not_pass(self) -> None:
        run = RunResult.model_validate(
            run_view(row(ATTEMPT_1, "pass"), row(ATTEMPT_2, "fail"), row("x" * 23 + "3", None))
        )
        assert [a.attempt_id for a in run.failures()] == [ATTEMPT_2]


def _timed_out(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


# What the API answers when too many jobs are being started with an organization's keys at once.
BUSY = (
    503,
    {
        "code": "INTERNAL",
        "message": "too many jobs are being started with this organization's API keys at once to check its "
        "limits, so this one was not started; try again with the same Idempotency-Key",
    },
    {"Retry-After": "1"},
)
STARTS = f"/{ENV_ID}/external-runs"


class TestResends:
    def test_a_start_whose_reply_was_lost_is_sent_again_under_its_key(self, client, api: FakeAPI) -> None:
        api.on("POST", STARTS, _timed_out, ok(start_body(ATTEMPT_1), 201))
        started = client.environments.runs.start_external(ENV_ID, agent_name="a", set_name="ci")
        assert started.run_id == RUN_ID
        keys = [r.headers["idempotency-key"] for r in api.calls("POST", STARTS)]
        assert len(keys) == 2 and keys[0] == keys[1]

    def test_a_reply_that_never_comes_is_given_up_on_after_the_clients_retries(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/rollout-runs", _timed_out)
        with pytest.raises(APITimeoutError):
            client.environments.runs.run_model(ENV_ID, budget_usd=1.0, model="m", set_name="ci", idempotency_key="k1")
        sent = api.calls("POST", f"/{ENV_ID}/rollout-runs")
        assert [r.headers["idempotency-key"] for r in sent] == ["k1"] * 3

    def test_a_generate_whose_connection_reset_is_sent_again(self, client, api: FakeAPI) -> None:
        def reset(request: httpx.Request) -> httpx.Response:
            raise httpx.RemoteProtocolError("peer closed connection", request=request)

        job = {"id": "j1", "kind": "generate_tasks", "status": "pending", "created_at": "2026-10-26T12:00:00Z"}
        api.on("POST", f"/{ENV_ID}/task-sets/g/generate", reset, ok(job, 202))
        assert client.environments.task_sets.generate(ENV_ID, "g", count=1, budget_usd=1.0).id == "j1"
        assert len(api.calls("POST", f"/{ENV_ID}/task-sets/g/generate")) == 2

    def test_a_request_without_a_key_is_not_sent_again_after_a_lost_reply(self, client, api: FakeAPI) -> None:
        api.on("POST", f"/{ENV_ID}/runs/{RUN_ID}/cancel", _timed_out)
        with pytest.raises(APITimeoutError):
            client.environments.runs.cancel(ENV_ID, RUN_ID)
        assert len(api.requests) == 1

    def test_a_busy_admission_is_waited_out_past_the_clients_two_retries(self, client, api: FakeAPI) -> None:
        api.on("POST", STARTS, *[BUSY] * 6, ok(start_body(ATTEMPT_1), 201))
        with patch("layerlens._resource.time.sleep") as slept:
            client.environments.runs.start_external(ENV_ID, agent_name="a", set_name="ci")
        assert len(api.calls("POST", STARTS)) == 7
        assert [c.args[0] for c in slept.call_args_list] == [1.0] * 6

    def test_a_busy_admission_is_waited_out_for_about_30_seconds_in_all(self, client, api: FakeAPI) -> None:
        api.on("POST", STARTS, BUSY)
        with patch("layerlens._resource.time.sleep") as slept, pytest.raises(InternalServerError) as raised:
            client.environments.runs.start_external(ENV_ID, agent_name="a", set_name="ci")
        assert raised.value.status_code == 503
        assert sum(c.args[0] for c in slept.call_args_list) == BUSY_RETRY_SECONDS
        assert len(api.calls("POST", STARTS)) == 1 + int(BUSY_RETRY_SECONDS)

    def test_a_body_the_api_refuses_by_its_size_is_not_sent_again(self, client, api: FakeAPI) -> None:
        api.on("POST", STARTS, (413, {"code": "INTERNAL", "message": "the request body is larger than 5242880 bytes"}))
        with pytest.raises(APIStatusError) as raised:
            client.environments.runs.start_external(ENV_ID, agent_name="a", set_name="ci")
        assert raised.value.status_code == 413
        assert len(api.requests) == 1

    def test_a_body_over_the_apis_limit_is_refused_before_it_is_sent(self, client, api: FakeAPI) -> None:
        tasks = [{"name": f"t{i}", "prompt": "x" * 20_000, "grader": {"kind": "answer"}} for i in range(300)]
        with pytest.raises(ValueError, match=r"5.00 MiB \(5242880 bytes\).*task set"):
            client.environments.runs.start_external(ENV_ID, agent_name="a", tasks=tasks)
        assert api.requests == []

    def test_a_body_at_the_apis_limit_is_sent(self, client, api: FakeAPI) -> None:
        api.on("POST", STARTS, ok(start_body(ATTEMPT_1), 201))
        body = external_run_body("a", None, [{"name": "t", "prompt": ""}], None, None, None)
        room = MAX_REQUEST_BODY_BYTES - len(httpx.Request("POST", "https://x", json=body).content)
        tasks = [{"name": "t", "prompt": "x" * room}]
        client.environments.runs.start_external(ENV_ID, agent_name="a", tasks=tasks)
        assert len(api.calls("POST", STARTS)[0].content) == MAX_REQUEST_BODY_BYTES


def test_a_task_set_page_whose_next_offset_does_not_move_stops_the_read(client, api: FakeAPI) -> None:
    page = {"set_name": "ci", "total": 300, "offset": 100, "next_offset": 100, "tasks": []}
    api.on("GET", f"/{ENV_ID}/task-sets/ci", ok({**page, "offset": 0}), ok(page))
    with pytest.raises(APIResponseValidationError, match="next_offset 100, which does not move forward"):
        client.environments.task_sets.get(ENV_ID, "ci")
    assert [r.url.params["offset"] for r in api.requests] == ["0", "100"]


def test_attempt_tokens_stay_out_of_the_models_reprs() -> None:
    started = ExternalRunStart.model_validate(start_body(ATTEMPT_1, ATTEMPT_2))
    token = started.attempts[0].token
    assert token.startswith("llat_v1_")
    for text in (repr(started), str(started), repr(started.attempts[0]), str(started.attempts[0])):
        assert "llat_v1_" not in text
    assert started.model_dump(mode="json")["attempts"][0]["token"] == token


def _escaped_size(body: object) -> int:
    content = httpx.Request("POST", "https://x", json=body).content
    return len(json.dumps(content.decode("utf-8"), ensure_ascii=False).encode("utf-8"))


class TestEscapedBodySize:
    def test_a_body_under_5_mib_that_escapes_past_the_gateways_limit_is_refused(self, client, api: FakeAPI) -> None:
        prompt = ('{"a": "b", "c": [1, 2, 3]} ' * 1000)[:14000]
        tasks = [{"name": f"t{i}", "prompt": prompt, "grader": {"kind": "answer"}} for i in range(270)]
        body = external_run_body("a", None, tasks, None, None, None)
        assert len(httpx.Request("POST", "https://x", json=body).content) < MAX_REQUEST_BODY_BYTES
        assert _escaped_size(body) > MAX_ESCAPED_BODY_BYTES
        with pytest.raises(ValueError, match=r"once the API's gateway escapes it, where every quote and backslash"):
            client.environments.runs.start_external(ENV_ID, agent_name="a", tasks=tasks)
        assert api.requests == []

    @pytest.mark.skipif(
        "\\u00e9" in httpx.Request("POST", "https://x", json="é").content.decode(),
        reason="this httpx escapes non-ASCII in JSON bodies, so the body really is six bytes a character",
    )
    def test_non_ascii_text_is_not_counted_as_escaped(self, client, api: FakeAPI) -> None:
        api.on("POST", STARTS, ok(start_body(ATTEMPT_1), 201))
        tasks = [{"name": f"t{i}", "prompt": "é" * 8000, "grader": {"kind": "answer"}} for i in range(300)]
        client.environments.runs.start_external(ENV_ID, agent_name="a", tasks=tasks)
        assert len(api.calls("POST", STARTS)) == 1
