"""``models.add()`` / ``remove()`` broke the model list of a project that has a custom model.

They read the project's models of both kinds and PATCHed the project's ``models`` with every id.
A custom model belongs to its project already (the API lists it by its ``project_id``), and
``models`` lists the project's public models: the API stored the custom id there, and from then
on ``GET …/models?type=public`` answered 500 (``no document found in a MongoDB collection models``)
for every caller, the app included. They now read and send public ids only, as
``benchmarks.add()`` / ``remove()`` do since BUG-11.
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
    "id": "6ac39ed52c3c761d47b277ca",
    "key": "acme/support-bot",
    "name": "Support bot",
    "description": "",
    "max_tokens": 4096,
    "api_url": "https://models.acme.example/v1",
    "disabled": False,
    "organization_id": ORG,
    "project_id": PROJECT,
}
PUBLIC = {
    "id": "688130a7e014f9fa7019cdbd",
    "key": "google/gemini-2.5-flash-lite",
    "name": "Gemini 2.5 Flash Lite",
    "description": "",
}
NEW = "6a0b692ced57a8fd01d1433a"
CATALOG = {
    PUBLIC["id"]: PUBLIC,
    NEW: {"id": NEW, "key": "openai/gpt-4o-mini", "name": "GPT-4o mini", "description": ""},
}


class _Project:
    """The API: lists a project's models by type, and takes a PATCH of its ``models``.

    It keeps what a PATCH sends, as the API does, and answers the public list 500 once a custom
    model's id is among them.
    """

    def __init__(self) -> None:
        self.models: List[str] = [PUBLIC["id"]]
        self.patches: List[Dict[str, Any]] = []

    def _listed(self, kind: str) -> List[Dict[str, Any]]:
        if kind == "custom":
            return [CUSTOM]
        return [CATALOG[model_id] for model_id in self.models]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"{PROJECT_PATH}/models":
            kind = request.url.params.get("type", "public")
            if kind == "public" and CUSTOM["id"] in self.models:
                return httpx.Response(
                    500,
                    json={"status": "error", "error": "no document found in a MongoDB collection models"},
                )
            listed = self._listed(kind)
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"models": listed, "count": len(listed), "total_count": len(listed)},
                },
            )
        if request.method == "PATCH" and request.url.path == PROJECT_PATH:
            body = json.loads(request.content)
            self.patches.append(body)
            self.models = list(body.get("models", []))
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


def test_add_sends_the_projects_public_models_and_the_new_one() -> None:
    api = _Project()
    assert _client(api).models.add(NEW) is True
    assert api.patches == [{"models": [PUBLIC["id"], NEW]}]


def test_the_project_still_lists_its_models_after_an_add() -> None:
    api = _Project()
    client = _client(api)
    client.models.add(NEW)
    listed = client.models.get()
    assert listed is not None
    assert {m.id for m in listed} == {CUSTOM["id"], PUBLIC["id"], NEW}


def test_remove_sends_the_projects_public_models_left() -> None:
    api = _Project()
    assert _client(api).models.remove(PUBLIC["id"]) is True
    assert api.patches == [{"models": []}]


def test_the_async_client_sends_public_ids_only() -> None:
    api = _Project()
    client = _async_client(api)

    async def main() -> List[bool]:
        return [await client.models.add(NEW), await client.models.remove(PUBLIC["id"])]

    assert asyncio.run(main()) == [True, True]
    assert api.patches == [{"models": [PUBLIC["id"], NEW]}, {"models": [NEW]}]
