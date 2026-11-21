"""BUG-11: ``benchmarks.add()`` / ``remove()`` failed in a project that holds a custom benchmark.

They read the project's benchmarks of both kinds and PATCHed the project's ``datasets`` with every
id. ``datasets`` lists public benchmarks only, so the API answered 500 for the custom one's id
("no document found in a MongoDB collection datasets"). They now read and send public ids only.
"""

from __future__ import annotations

import json
import asyncio
from typing import Any, Dict, List

import httpx

from layerlens import LayerLens, AsyncLayerLens

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
PROJECT_PATH = f"/api/v1/organizations/{ORG}/projects/{PROJECT}"
CUSTOM = {
    "id": "6ab6d938440559569c90b386",
    "key": "benchaccounting",
    "name": "Bench accounting",
    "organization_id": ORG,
}
PUBLIC = {"id": "67e20572e014f9fa6c49b996", "key": "accountingaudit", "name": "Accounting audit"}
NEW = "6a0b6964ed57a8fd01d1472d"


class _Project:
    """The API: lists a project's benchmarks by type, and takes a PATCH of its public ones."""

    def __init__(self) -> None:
        self.patches: List[Dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"{PROJECT_PATH}/benchmarks":
            kind = request.url.params.get("type")
            listed = [CUSTOM] if kind == "custom" else [PUBLIC] if kind == "public" else [CUSTOM, PUBLIC]
            return httpx.Response(200, json={"status": "success", "data": {"datasets": listed}})
        if request.method == "PATCH" and request.url.path == PROJECT_PATH:
            body = json.loads(request.content)
            self.patches.append(body)
            if CUSTOM["id"] in body.get("datasets", []):  # as the API answered before its own fix
                return httpx.Response(
                    500, json={"status": "error", "error": "no document found in a MongoDB collection datasets"}
                )
            return httpx.Response(200, json={"status": "success", "data": {"id": PROJECT}})
        return httpx.Response(404, text="404 page not found")


def _client(api: _Project) -> LayerLens:
    client = LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(api)
    return client


def _async_client(api: _Project) -> AsyncLayerLens:
    client = AsyncLayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(api)
    return client


def test_add_sends_the_projects_public_benchmarks_and_the_new_one() -> None:
    api = _Project()
    assert _client(api).benchmarks.add(NEW) is True
    assert api.patches == [{"datasets": [PUBLIC["id"], NEW]}]


def test_remove_sends_the_projects_public_benchmarks_left() -> None:
    api = _Project()
    assert _client(api).benchmarks.remove(PUBLIC["id"]) is True
    assert api.patches == [{"datasets": []}]


def test_the_async_client_sends_public_ids_only() -> None:
    api = _Project()
    client = _async_client(api)

    async def main() -> List[bool]:
        return [await client.benchmarks.add(NEW), await client.benchmarks.remove(PUBLIC["id"])]

    assert asyncio.run(main()) == [True, True]
    assert api.patches == [{"datasets": [PUBLIC["id"], NEW]}, {"datasets": []}]
