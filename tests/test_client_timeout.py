"""The client's timeout reaches every call (R1 F1).

Every resource method defaulted its own ``timeout`` to the 600-second ``DEFAULT_TIMEOUT`` and sent
it with the request, where it overrode the client's: ``LayerLens(timeout=30)``,
``with_options(timeout=10)`` and ``copy(timeout=...)`` changed the attribute and reached no call,
so a call to an API that never answered waited ten minutes. These tests call a local server that
answers late, or never, and time what the SDK does. A timeout given to the call still wins.
"""

from __future__ import annotations

import json
import time
import asyncio
import threading
from typing import Any, List, Union, Iterator
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import pytest

from layerlens import LayerLens, PublicClient, AsyncLayerLens, APITimeoutError
from layerlens.models import Evaluation

ORG = {"id": "org-1", "name": "Acme", "projects": [{"id": "proj-1", "name": "Default"}]}
# Long enough that only a timeout ends the wait; the server lets go when the test ends.
NEVER = 60.0


class _LateAPI:
    """An API that answers after ``delay`` seconds: the organization, and an empty list otherwise."""

    def __init__(self) -> None:
        self.delay = NEVER
        self.done = threading.Event()
        # The path of every request, in the order they came.
        self.paths: List[str] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the http.server hook
                api.paths.append(self.path.split("?")[0])
                api.done.wait(api.delay)
                if api.done.is_set():
                    return
                data: Any = ORG if self.path.split("?")[0].endswith("/organizations") else []
                payload = json.dumps({"status": "success", "data": data}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:  # the client gave up first
                    pass

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1"

    def __enter__(self) -> "_LateAPI":
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.done.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def api() -> Iterator[_LateAPI]:
    with _LateAPI() as server:
        yield server


def _client(api: _LateAPI, **options: Any) -> LayerLens:
    return LayerLens(api_key="k", base_url=api.base_url, max_retries=0, **options)


def _times_out_within(seconds: float, call: Any) -> None:
    started = time.monotonic()
    with pytest.raises(APITimeoutError):
        call()
    assert time.monotonic() - started < seconds


class TestTheClientsTimeout:
    def test_reaches_a_resource_call(self, api: _LateAPI) -> None:
        client = _client(api, timeout=0.3)
        client.organization_id, client.project_id = "org-1", "proj-1"

        _times_out_within(5, client.integrations.get_many)

    def test_reaches_the_first_call_through_the_organization_read(self, api: _LateAPI) -> None:
        # The organization is read on first use; it waits the client's timeout, not 30 seconds.
        _times_out_within(5, _client(api, timeout=0.3).integrations.get_many)

    def test_with_options_reaches_the_call(self, api: _LateAPI) -> None:
        client = _client(api)
        client.organization_id, client.project_id = "org-1", "proj-1"

        _times_out_within(5, client.with_options(timeout=0.3).integrations.get_many)

    def test_copy_reaches_the_call(self, api: _LateAPI) -> None:
        client = _client(api)
        client.organization_id, client.project_id = "org-1", "proj-1"

        _times_out_within(5, client.copy(timeout=0.3).traces.get_many)

    def test_reaches_a_call_of_the_async_client(self, api: _LateAPI) -> None:
        client = AsyncLayerLens(api_key="k", base_url=api.base_url, max_retries=0, timeout=0.3)
        client.organization_id, client.project_id = "org-1", "proj-1"

        _times_out_within(5, lambda: asyncio.run(client.integrations.get_many()))
        # The resource call timed out, not an organization read before it.
        assert api.paths == ["/api/v1/organizations/org-1/integrations"]

    def test_with_options_reaches_a_call_of_the_async_client(self, api: _LateAPI) -> None:
        scoped = AsyncLayerLens(api_key="k", base_url=api.base_url, max_retries=0).with_options(timeout=0.3)
        scoped.organization_id, scoped.project_id = "org-1", "proj-1"

        _times_out_within(5, lambda: asyncio.run(scoped.integrations.get_many()))
        assert api.paths == ["/api/v1/organizations/org-1/integrations"]

    def test_reaches_the_first_call_of_the_async_client_through_the_organization_read(self, api: _LateAPI) -> None:
        client = AsyncLayerLens(api_key="k", base_url=api.base_url, max_retries=0, timeout=0.3)

        _times_out_within(5, lambda: asyncio.run(client.integrations.get_many()))
        assert api.paths == ["/api/v1/organizations"]

    def test_reaches_a_call_of_the_public_client(self, api: _LateAPI) -> None:
        public = PublicClient(api_key="k", base_url=api.base_url, max_retries=0, timeout=0.3)

        _times_out_within(5, public.models.get)

    def test_reaches_a_call_of_client_public(self, api: _LateAPI) -> None:
        _times_out_within(5, _client(api, timeout=0.3).public.benchmarks.get)


class TestATimeoutGivenToTheCall:
    def test_wins_over_a_longer_client_timeout(self, api: _LateAPI) -> None:
        client = _client(api)
        client.organization_id, client.project_id = "org-1", "proj-1"

        _times_out_within(5, lambda: client.integrations.get_many(timeout=0.3))

    def test_wins_over_a_shorter_client_timeout(self, api: _LateAPI) -> None:
        api.delay = 0.6
        client = _client(api, timeout=0.2)
        client.organization_id, client.project_id = "org-1", "proj-1"

        assert client.integrations.get_many(timeout=5) is not None

    def test_none_is_no_timeout(self, api: _LateAPI) -> None:
        api.delay = 0.6
        client = _client(api, timeout=0.2)
        client.organization_id, client.project_id = "org-1", "proj-1"

        assert client.integrations.get_many(timeout=None) is not None


def test_the_default_still_answers_a_slow_api(api: _LateAPI) -> None:
    api.delay = 0.3
    client = _client(api)

    assert client.integrations.get_many() is not None
    assert client.organization_id == "org-1"


def _evaluation(client: Union[LayerLens, AsyncLayerLens]) -> Evaluation:
    corpus = Path(__file__).resolve().parent / "contract" / "corpus" / "evaluations_get_many.json"
    row = json.loads(corpus.read_text())["evaluations"][0]
    return Evaluation.model_validate(row).attach_client(client)


class TestTheEvaluationResultHelpers:
    """M1: ``Evaluation.get_results()``, ``get_all_results()`` and their async forms sent no timeout.

    They defaulted ``timeout`` to ``None``, which is no timeout, and passed it on, so with
    ``LayerLens(timeout=1)`` against a stalled API they were still waiting at 8 s while every other
    call timed out at 1 s. The server here answers after 8 s: only a timeout ends the call sooner.
    """

    @pytest.mark.parametrize("helper", ["get_results", "get_all_results"])
    def test_a_sync_helper_uses_the_clients_timeout(self, api: _LateAPI, helper: str) -> None:
        api.delay = 8
        client = _client(api, timeout=0.3)
        client.organization_id, client.project_id = "org-1", "proj-1"

        _times_out_within(5, getattr(_evaluation(client), helper))

    @pytest.mark.parametrize("helper", ["get_results_async", "get_all_results_async"])
    def test_an_async_helper_uses_the_clients_timeout(self, api: _LateAPI, helper: str) -> None:
        api.delay = 8
        client = AsyncLayerLens(api_key="k", base_url=api.base_url, max_retries=0, timeout=0.3)
        client.organization_id, client.project_id = "org-1", "proj-1"
        evaluation = _evaluation(client)

        _times_out_within(5, lambda: asyncio.run(getattr(evaluation, helper)()))

    def test_a_timeout_given_to_a_helper_still_wins(self, api: _LateAPI) -> None:
        api.delay = 8
        client = _client(api)
        client.organization_id, client.project_id = "org-1", "proj-1"

        _times_out_within(5, lambda: _evaluation(client).get_results(timeout=0.3))
