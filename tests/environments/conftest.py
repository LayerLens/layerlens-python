"""A fake Automation API behind real ``LayerLens`` clients.

The clients are the real ones: only their transport is an ``httpx.MockTransport``, so
headers, retries, envelopes and error mapping all run as they do against the API.
"""

from __future__ import annotations

import json
import inspect
import threading
from typing import Any, Dict, List, Tuple, Union, Callable, Iterator, Optional
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from layerlens import LayerLens, AsyncLayerLens
from layerlens.models import Project, Organization

ORG = "691254ecd44a29f6926881a2"
PROJECT = "691254eed44a29f6926881a3"
BASE = f"/api/v1/env/organizations/{ORG}/projects/{PROJECT}/environments"
ENV_ID = "6adcba8583c75b47f74ebd08"
RUN_ID = "6a91c0e5f1d2a3b4c5d6e7f8"
ATTEMPT_1 = "6a91c0e5f1d2a3b4c5d6e801"
ATTEMPT_2 = "6a91c0e5f1d2a3b4c5d6e802"

Reply = Union[Tuple[int, Any], Tuple[int, Any, Dict[str, str]], Callable[[httpx.Request], Any]]


def ok(data: Any, status: int = 200) -> Tuple[int, Any]:
    return status, {"status": "success", "data": data}


def err(status: int, message: str, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any, Dict[str, str]]:
    return status, {"status": "error", "error": message}, headers or {}


class FakeAPI:
    """Routes ``(method, path under BASE)`` to queued replies; the last reply repeats.

    ``run exec --parallel`` calls it from several threads at once.
    """

    def __init__(self) -> None:
        self.routes: Dict[Tuple[str, str], List[Reply]] = {}
        self.requests: List[httpx.Request] = []
        self._lock = threading.Lock()

    def on(self, method: str, path: str, *replies: Reply) -> None:
        with self._lock:
            self.routes.setdefault((method, path), []).extend(replies)

    def calls(self, method: str, path: str) -> List[httpx.Request]:
        with self._lock:
            return [r for r in self.requests if r.method == method and _relative(r) == path]

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
            queue = self.routes.get((request.method, _relative(request)))
            if not queue:
                return httpx.Response(404, text="404 page not found")
            reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, httpx.Response):
            return reply
        status, body = reply[0], reply[1]
        headers = reply[2] if len(reply) > 2 else {}
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)


def _relative(request: httpx.Request) -> str:
    path = request.url.path
    return path[len(BASE) :] if path.startswith(BASE) else path


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content) if request.content else None


_ORGANIZATION = Organization(id=ORG, name="Org", projects=[Project(id=PROJECT, name="Starter")])


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture(autouse=True)
def _no_sleep() -> Iterator[None]:
    with patch("layerlens._base_client.time.sleep"), patch("layerlens._resource.time.sleep"):
        yield


def make_client(api: FakeAPI, **kw: Any) -> LayerLens:
    client = LayerLens(api_key="test-key", base_url="https://api.test/api/v1", **kw)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(api.handle)
    return client


@pytest.fixture
def client(api: FakeAPI) -> LayerLens:
    return make_client(api)


@pytest.fixture
def async_client(api: FakeAPI) -> Iterator[AsyncLayerLens]:
    async def no_sleep(_: float) -> None:
        return None

    client = AsyncLayerLens(api_key="test-key", base_url="https://api.test/api/v1")
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(api.handle)
    with patch("asyncio.sleep", no_sleep):
        yield client


@pytest.fixture
def runner() -> CliRunner:
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


def environment(**overrides: Any) -> Dict[str, Any]:
    return {
        "id": ENV_ID,
        "name": "support-desk",
        "type": "zendesk",
        "current_version": 1,
        "environment_version": 1,
        "tool_count": 18,
        "created_at": "2026-10-26T12:00:00.123Z",
        "updated_at": "2026-10-26T12:05:00Z",
        "world_hash": "abc",
        "generator_version": "1.2.3",
        **overrides,
    }


def run_view(*attempts: Dict[str, Any], **overrides: Any) -> Dict[str, Any]:
    rows = list(attempts)
    counts = {"pass": 0, "fail": 0, "error": 0, "timeout": 0}
    pending = 0
    for row in rows:
        if row.get("verdict") is None:
            pending += 1
        else:
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    total = len(rows)
    counted = sum(counts[v] for v in ("pass", "fail", "error", "timeout"))
    view = {
        "run_id": RUN_ID,
        "kind": "external",
        "status": "success",
        "stop_reason": "finished",
        "error": None,
        "environment_version": 4,
        "agent_name": "support-agent",
        "label": "3f2c1ab",
        "model": "external:support-agent",
        "set_name": None,
        "total": total,
        "passed": counts["pass"],
        "failed": counts["fail"],
        "errored": counts["error"],
        "timed_out": counts["timeout"],
        "pending": pending,
        "counted": counted,
        "pass_rate": counts["pass"] / counted if counted else None,
        "cost_usd": 0.0,
        "attempts": rows,
    }
    view.update(overrides)
    return view


def row(attempt_id: str, verdict: Optional[str], **overrides: Any) -> Dict[str, Any]:
    graded = verdict is not None
    return {
        "attempt_id": attempt_id,
        "task_id": "6a8f2c1d9e0b4a7f3c5d6e1" + attempt_id[-1],
        "task_name": f"task-{attempt_id[-1]}",
        "task_version": 1,
        "verdict": verdict,
        "grader": "answer",
        "evidence": "expected 9900001017, the agent answered 9900001019" if verdict == "fail" else None,
        "error_source": None,
        "agent_answer": "the answer" if graded else None,
        "agent_answer_truncated": False,
        "cost_usd": 0.0 if graded else None,
        "trace_id": "6a91c0f2a7b8c9d0e1f2a3b4" if graded else None,
        "agent_trace_id": None,
        **overrides,
    }


def start_body(*attempt_ids: str) -> Dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "attempts": [
            {
                "attempt_id": a,
                "task_id": "6a8f2c1d9e0b4a7f3c5d6e1" + a[-1],
                "task_name": f"task-{a[-1]}",
                "prompt": f"Prompt {a[-1]}",
                "base_url": f"https://api.test/api/v1/attempts/{a}",
                "token": f"llat_v1_{RUN_ID}_{a}_" + "m" * 43,
                "call_url": f"https://api.test{BASE}/{ENV_ID}/call?run_id={RUN_ID}&task_id=x",
                "expires_at": "2026-10-27T09:14:22.000Z",
            }
            for a in attempt_ids
        ],
    }


def submitted(attempt_id: str, answered: int = 1, total: int = 2, finished: bool = False) -> Dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "task_id": "6a8f2c1d9e0b4a7f3c5d6e1" + attempt_id[-1],
        "attempt_id": attempt_id,
        "answered": answered,
        "total": total,
        "finished": finished,
    }
