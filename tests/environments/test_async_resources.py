from __future__ import annotations

import asyncio

import httpx
import pytest

from layerlens import LayerLensError, EnvironmentBuildError, EnvironmentConflictError
from layerlens._exceptions import ConflictError
from layerlens.resources.environments._shared import (
    SUBMIT_LESS,
    MAX_ANSWER_BYTES,
    RUN_FEWER_INLINE_TASKS,
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
)

SUBMIT_1 = f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{ATTEMPT_1}/submit"
SUBMIT_2 = f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{ATTEMPT_2}/submit"
RUN = f"/{ENV_ID}/runs/{RUN_ID}"


def test_async_with_starts_and_finishes_the_run(async_client, api: FakeAPI) -> None:
    api.on("POST", f"/{ENV_ID}/external-runs", ok(start_body(ATTEMPT_1, ATTEMPT_2), 201))
    api.on("POST", SUBMIT_1, ok(submitted(ATTEMPT_1)))
    api.on("POST", SUBMIT_2, ok(submitted(ATTEMPT_2, answered=2, finished=True)))
    api.on(
        "POST", f"/{ENV_ID}/external-runs/{RUN_ID}/finish", ok(run_view(row(ATTEMPT_1, "pass"), row(ATTEMPT_2, "pass")))
    )

    async def main() -> None:
        async with async_client.environments.external_run(ENV_ID, agent_name="a", set_name="ci") as run:
            await asyncio.gather(*(attempt.submit("x") for attempt in run.attempts))
            assert all(attempt.submitted for attempt in run.attempts)

    asyncio.run(main())
    assert api.requests[-1].url.path.endswith("/finish")
    assert len(api.calls("POST", SUBMIT_1)) == 1 and len(api.calls("POST", SUBMIT_2)) == 1


def test_an_exception_in_async_with_cancels_the_run(async_client, api: FakeAPI) -> None:
    api.on("POST", f"/{ENV_ID}/external-runs", ok(start_body(ATTEMPT_1), 201))
    api.on("POST", f"/{ENV_ID}/runs/{RUN_ID}/cancel", ok(run_view(row(ATTEMPT_1, "error"), status="cancelled")))

    async def main() -> None:
        async with async_client.environments.external_run(ENV_ID, agent_name="a", set_name="ci"):
            raise RuntimeError("harness broke")

    with pytest.raises(RuntimeError):
        asyncio.run(main())
    assert api.requests[-1].url.path.endswith("/cancel")


def test_a_concurrent_second_submit_of_one_attempt_is_refused(async_client, api: FakeAPI) -> None:
    api.on("POST", f"/{ENV_ID}/external-runs", ok(start_body(ATTEMPT_1), 201))
    api.on("POST", SUBMIT_1, ok(submitted(ATTEMPT_1)))

    async def main() -> list:
        run = await async_client.environments.external_run(ENV_ID, agent_name="a", set_name="ci").start()
        attempt = run.attempts[0]
        return await asyncio.gather(attempt.submit("a"), attempt.submit("b"), return_exceptions=True)

    outcomes = asyncio.run(main())
    assert sum(isinstance(o, LayerLensError) for o in outcomes) == 1
    assert len(api.calls("POST", SUBMIT_1)) == 1


def test_a_gateway_timeout_on_submit_is_settled_from_the_run(async_client, api: FakeAPI) -> None:
    api.on("POST", SUBMIT_1, (504, {"message": "Endpoint request timed out"}), err(409, "already being graded"))
    api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, "pass"), row(ATTEMPT_2, None), status="in_progress")))
    result = asyncio.run(async_client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a"))
    assert result.attempt_id == ATTEMPT_1 and not result.finished


def test_a_conflict_on_an_attempt_graded_before_is_raised(async_client, api: FakeAPI) -> None:
    api.on("POST", SUBMIT_1, err(409, "already submitted"))
    api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, "pass"), row(ATTEMPT_2, None), status="in_progress")))
    with pytest.raises(ConflictError):
        asyncio.run(async_client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "a"))
    assert len(api.calls("POST", SUBMIT_1)) == 1


def test_an_async_answer_past_the_apis_limit_is_refused_naming_the_answer(async_client, api: FakeAPI) -> None:
    with pytest.raises(InvalidArgument, match=f"^answer is {MAX_ANSWER_BYTES + 1} bytes; the limit is 65536$"):
        asyncio.run(async_client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "x" * (MAX_ANSWER_BYTES + 1)))
    assert api.requests == []


def test_async_refusals_of_a_body_too_big_to_send_name_what_to_send_less_of(async_client, api: FakeAPI) -> None:
    # LAY-4454: the remedy is the request's own, on the async client as on the sync one.
    runs = async_client.environments.runs
    with pytest.raises(InvalidArgument) as submit:
        asyncio.run(runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, "4" + " " * (6 << 20)))
    with pytest.raises(InvalidArgument) as start:
        tasks = [{"name": f"t{i}", "prompt": "x" * 20_000} for i in range(300)]
        asyncio.run(runs.start_external(ENV_ID, agent_name="a", tasks=tasks))
    assert str(submit.value).endswith(f": {SUBMIT_LESS}")
    assert str(start.value).endswith(f": {RUN_FEWER_INLINE_TASKS}")
    assert api.requests == []


def test_an_async_submit_held_back_by_a_call_goes_through(async_client, api: FakeAPI) -> None:
    api.on("POST", SUBMIT_1, err(409, "a call to this task is still being served"), ok(submitted(ATTEMPT_1)))
    api.on("GET", RUN, ok(run_view(row(ATTEMPT_1, None), status="in_progress")))
    result = asyncio.run(async_client.environments.runs.submit(ENV_ID, RUN_ID, ATTEMPT_1, outcome="timeout"))
    assert result.attempt_id == ATTEMPT_1


def test_a_failed_build_raises(async_client, api: FakeAPI) -> None:
    failed = {"status": "failed", "steps": [], "refused_because": "the world cannot hold this"}
    api.on("GET", f"/{ENV_ID}/build", ok(failed))
    with pytest.raises(EnvironmentBuildError, match="cannot hold this"):
        asyncio.run(async_client.environments.wait_until_ready(ENV_ID))


def test_a_ready_build_returns_the_environment(async_client, api: FakeAPI) -> None:
    api.on(
        "GET",
        f"/{ENV_ID}/build",
        ok({"status": "building", "steps": []}),
        ok({"status": "ready", "version": 1, "steps": []}),
    )
    api.on("GET", f"/{ENV_ID}", ok({"environment": environment()}))
    found = asyncio.run(async_client.environments.wait_until_ready(ENV_ID, poll_interval_seconds=0.01))
    assert found.id == ENV_ID


def test_an_async_create_conflict_names_the_differences(async_client, api: FakeAPI) -> None:
    api.on("POST", "/worlds", (409, {"status": "error", "error": "differs", "differences": ["seed"]}))
    with pytest.raises(EnvironmentConflictError) as caught:
        asyncio.run(
            async_client.environments.create(
                "support-desk", "zendesk", description="A desk", seed=2, if_not_exists=True
            )
        )
    assert caught.value.differences == ["seed"]
    assert body_of(api.requests[0])["seed"] == 2


def test_async_name_lookup(async_client, api: FakeAPI) -> None:
    api.on("GET", "", ok({"environments": [environment()], "count": 1, "total_count": 1}))
    api.on("GET", f"/{ENV_ID}/tools", ok({"tools": [{"name": "get_ticket", "input_schema": {}}], "count": 1}))
    tools = asyncio.run(async_client.environments.tools("support-desk"))
    assert [t.name for t in tools.tools] == ["get_ticket"]


def test_async_list_can_leave_out_members(async_client, api: FakeAPI) -> None:
    api.on("GET", "", ok({"environments": [environment()], "count": 1, "total_count": 1}))
    asyncio.run(async_client.environments.list(hide_members=True))
    assert api.requests[0].url.params["hide_members"] == "true"


def test_an_async_blank_description_is_refused_before_sending(async_client, api: FakeAPI) -> None:
    with pytest.raises(ValueError, match="description"):
        asyncio.run(async_client.environments.create("support-desk", "zendesk", description=""))
    assert api.requests == []


def test_an_async_start_whose_reply_was_lost_is_sent_again_under_its_key(async_client, api: FakeAPI) -> None:
    def timed_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    busy = (503, {"code": "INTERNAL", "message": "too many jobs are being started"}, {"Retry-After": "1"})
    api.on("POST", f"/{ENV_ID}/external-runs", timed_out, busy, busy, busy, ok(start_body(ATTEMPT_1), 201))

    async def main() -> str:
        started = await async_client.environments.runs.start_external(ENV_ID, agent_name="a", set_name="ci")
        return started.run_id

    assert asyncio.run(main()) == RUN_ID
    keys = {r.headers["idempotency-key"] for r in api.calls("POST", f"/{ENV_ID}/external-runs")}
    assert len(api.calls("POST", f"/{ENV_ID}/external-runs")) == 5 and len(keys) == 1


def test_an_async_body_over_the_apis_limit_is_refused_before_it_is_sent(async_client, api: FakeAPI) -> None:
    tasks = [{"name": f"t{i}", "prompt": "x" * 20_000, "grader": {"kind": "answer"}} for i in range(300)]
    with pytest.raises(ValueError, match=r"the API takes at most 5.00 MiB \(5242880 bytes\)"):
        asyncio.run(async_client.environments.runs.start_external(ENV_ID, agent_name="a", tasks=tasks))
    assert api.requests == []
