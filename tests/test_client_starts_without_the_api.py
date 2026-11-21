"""BUG-03: ``LayerLens()`` read the organization over HTTP in its constructor.

A program written as the instrumentation docs show (``client = LayerLens()`` at module scope,
then ``@trace(client)``) did not start when LayerLens was unreachable (``APIConnectionError``)
or its key was wrong or rotated (``AuthenticationError``). The organization and project are now
read on first use: tracing counts a failed upload as a lost trace and the program runs on, and a
call that needs them still raises what the API said.
"""

from __future__ import annotations

from typing import Any, List, Iterator

import httpx
import pytest

from layerlens import LayerLens, AsyncLayerLens
from layerlens.instrument import trace, _upload
from layerlens._exceptions import LayerLensError, APIConnectionError, AuthenticationError

UNREACHABLE = "http://127.0.0.1:9/api/v1"  # the discard port: nothing listens, connect is refused
ORG = {"id": "6a0000000000000000000001", "name": "Org", "projects": [{"id": "6a0000000000000000000002", "name": "P"}]}


@pytest.fixture(autouse=True)
def _fresh_loss_stats() -> Iterator[None]:
    _upload.reset_upload_loss_stats()
    yield
    _upload.reset_upload_loss_stats()


def _on(client: LayerLens, handler: Any) -> LayerLens:
    client._transport = httpx.MockTransport(handler)
    return client


class _API:
    """Answers GET /organizations as told, and every other call with an empty trace list."""

    def __init__(self, *organization_replies: httpx.Response) -> None:
        self.replies: List[httpx.Response] = list(organization_replies)
        self.organization_reads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/organizations"):
            self.organization_reads += 1
            return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(200, json={"status": "success", "data": {"traces": [], "count": 0, "total_count": 0}})


class TestTheClientStartsWithoutTheAPI:
    def test_constructing_with_layerlens_unreachable_does_not_raise(self) -> None:
        LayerLens(api_key="test-key", base_url=UNREACHABLE, max_retries=0)

    def test_a_traced_function_runs_while_layerlens_is_unreachable_and_its_trace_is_counted_lost(self) -> None:
        client = LayerLens(api_key="test-key", base_url=UNREACHABLE, max_retries=0)

        @trace(client)
        def handle(x: int) -> int:
            return x * 2

        assert handle(21) == 42
        assert _upload.get_upload_loss_stats() == {"upload_error": 1}

    def test_a_call_that_needs_the_organization_still_raises_while_unreachable(self) -> None:
        client = LayerLens(api_key="test-key", base_url=UNREACHABLE, max_retries=0)
        with pytest.raises(APIConnectionError):
            client.organization_id  # noqa: B018
        with pytest.raises(APIConnectionError):
            client.traces.get_many()

    def test_with_a_wrong_key_the_app_runs_and_resource_calls_say_why(self) -> None:
        api = _API(httpx.Response(401, json={"status": "error", "error": "Unknown API key"}))
        client = _on(LayerLens(api_key="rotated-key", base_url="https://api.test/api/v1", max_retries=0), api)

        @trace(client)
        def handle(x: int) -> int:
            return x * 2

        assert handle(21) == 42
        assert _upload.get_upload_loss_stats() == {"upload_error": 1}
        with pytest.raises(AuthenticationError, match="Unknown API key"):
            client.traces.get_many()

    def test_the_organization_is_read_once_on_first_use(self) -> None:
        api = _API(httpx.Response(200, json={"status": "success", "data": ORG}))
        client = _on(LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0), api)
        assert api.organization_reads == 0
        assert client.organization_id == ORG["id"]
        assert client.project_id == ORG["projects"][0]["id"]
        client.traces.get_many()
        assert api.organization_reads == 1

    def test_a_failed_read_is_tried_again_once_the_api_answers(self) -> None:
        api = _API(
            httpx.Response(401, json={"status": "error", "error": "Unknown API key"}),
            httpx.Response(200, json={"status": "success", "data": ORG}),
        )
        client = _on(LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0), api)
        with pytest.raises(AuthenticationError):
            client.organization_id  # noqa: B018
        assert client.organization_id == ORG["id"]
        assert api.organization_reads == 2

    def test_an_organization_without_a_project_is_refused_on_first_use(self) -> None:
        api = _API(httpx.Response(200, json={"status": "success", "data": {**ORG, "projects": []}}))
        client = _on(LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0), api)
        with pytest.raises(LayerLensError, match="is missing project"):
            client.project_id  # noqa: B018

    def test_ids_set_on_the_client_are_used_as_they_are(self) -> None:
        api = _API(httpx.Response(500, json={"status": "error", "error": "down"}))
        client = _on(LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0), api)
        client.organization_id = "org-1"
        client.project_id = "proj-1"
        assert (client.organization_id, client.project_id) == ("org-1", "proj-1")
        assert api.organization_reads == 0

    def test_an_organization_id_set_alone_must_be_the_keys_organization(self) -> None:
        # Its project is read from the key's organization: another organization's id would be
        # paired with a project it does not hold (CodeRabbit on #99).
        api = _API(httpx.Response(200, json={"status": "success", "data": ORG}))
        client = _on(LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0), api)
        client.organization_id = "6a0000000000000000000099"
        with pytest.raises(LayerLensError, match="6a0000000000000000000099"):
            client.project_id  # noqa: B018
        same = _on(LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0), api)
        same.organization_id = ORG["id"]
        assert same.project_id == ORG["projects"][0]["id"]

    def test_the_async_client_starts_while_layerlens_is_unreachable(self) -> None:
        client = AsyncLayerLens(api_key="test-key", base_url=UNREACHABLE, max_retries=0)
        with pytest.raises(APIConnectionError):
            client.organization_id  # noqa: B018

    def test_the_async_client_raises_what_the_api_said_as_the_sync_one_does(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        refused = httpx.Response(
            401,
            json={"status": "error", "error": "Unknown API key"},
            request=httpx.Request("GET", "https://api.test/x"),
        )

        class _Http:
            def __init__(self, **kwargs: Any) -> None:  # noqa: ARG002
                pass

            def __enter__(self) -> "_Http":
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

            def get(self, url: str, **kwargs: Any) -> httpx.Response:  # noqa: ARG002
                return refused

        monkeypatch.setattr("layerlens._client.httpx.Client", _Http)
        client = AsyncLayerLens(api_key="rotated-key", base_url="https://api.test/api/v1", max_retries=0)
        with pytest.raises(AuthenticationError, match="Unknown API key"):
            client.organization_id  # noqa: B018
