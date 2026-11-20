"""B2 / M3: ``models.get()`` / ``benchmarks.get()`` returned 20 rows, and ``add()`` / ``remove()`` dropped the rest.

A project's models and benchmarks lists answer 20 rows a page unless asked for more, and 100 at
most (the API's ``DefaultPageSize`` / ``MaxPageSize``). ``get()`` sent no page size, so it returned
the first 20 of a project's rows, and ``add()`` / ``remove()`` PATCHed the project with those: every
public model or benchmark past the 20th was gone from the project. The fake API here pages as the
real one does, and hides a deprecated row unless asked for it, as the real one does.
"""

from __future__ import annotations

import json
import asyncio
from typing import Any, Set, Dict, List

import httpx
import pytest

from layerlens import LayerLens, AsyncLayerLens

ORG, PROJECT = "691254ecd44a29f6926881a2", "6af478f7fb05d1a566d1a7bb"
PROJECT_PATH = f"/api/v1/organizations/{ORG}/projects/{PROJECT}"
NEW = "6a0b692ced57a8fd01d1433a"


def _id(prefix: str, i: int) -> str:
    return f"{prefix}{i:020x}"


class _ProjectAPI:
    """A project's models or benchmarks list, paged as the API pages it, and the project's PATCH."""

    def __init__(self, kind: str, public: int, custom: int = 0, deprecated: int = 0) -> None:
        self.kind = kind
        self.field = "models" if kind == "models" else "datasets"
        self.public: List[str] = [_id("a0a0", i) for i in range(public)]
        self.deprecated: Set[str] = set(self.public[:deprecated])
        self.custom: List[str] = [_id("c0c0", i) for i in range(custom)]
        self.queries: List[Dict[str, str]] = []
        self.patches: List[Dict[str, Any]] = []
        self.ignore_page = False

    def _row(self, row_id: str) -> Dict[str, Any]:
        row: Dict[str, Any] = {"id": row_id, "key": f"key-{row_id}", "name": f"Row {row_id}", "description": ""}
        if row_id in self.custom:
            row["organization_id"] = ORG
        if row_id in self.deprecated:
            row["deprecated"] = True
        return row

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"{PROJECT_PATH}/{self.kind}":
            params = dict(request.url.params)
            self.queries.append(params)
            if params.get("type") == "custom":
                ids = self.custom
            else:
                every = params.get("include_deprecated") == "true"
                ids = [i for i in self.public if every or i not in self.deprecated]
            size = int(params.get("page_size") or 20)
            size = 20 if size <= 0 else min(size, 100)
            page = 1 if self.ignore_page else int(params.get("page") or 1)
            window = [self._row(i) for i in ids[(page - 1) * size : page * size]]
            data = {self.field: window, "count": len(ids), "total_count": len(ids)}
            return httpx.Response(200, json={"status": "success", "data": data})
        if request.method == "PATCH" and request.url.path == PROJECT_PATH:
            body = json.loads(request.content)
            self.patches.append(body)
            self.public = list(body[self.field])
            return httpx.Response(200, json={"status": "success", "data": {"id": PROJECT}})
        return httpx.Response(404, text="404 page not found")


def _client(api: _ProjectAPI) -> LayerLens:
    client = LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(api)
    return client


def _async_client(api: _ProjectAPI) -> AsyncLayerLens:
    client = AsyncLayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(api)
    return client


KINDS = ["models", "benchmarks"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("rows", [25, 100, 130, 250])
def test_get_returns_every_public_row(kind: str, rows: int) -> None:
    api = _ProjectAPI(kind, public=rows)
    listed = getattr(_client(api), kind).get(type="public")
    assert listed is not None
    assert [r.id for r in listed] == api.public
    assert {q["page_size"] for q in api.queries} == {"100"}


@pytest.mark.parametrize("kind", KINDS)
def test_get_returns_every_custom_row_and_both_kinds_without_a_type(kind: str) -> None:
    api = _ProjectAPI(kind, public=30, custom=42)
    resource = getattr(_client(api), kind)
    custom = resource.get(type="custom")
    assert custom is not None and [r.id for r in custom] == api.custom
    both = resource.get()
    assert both is not None and [r.id for r in both] == api.custom + api.public


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("rows", [25, 130])
def test_add_keeps_every_row_the_project_holds(kind: str, rows: int) -> None:
    api = _ProjectAPI(kind, public=rows, custom=3, deprecated=2)
    before = list(api.public)
    assert getattr(_client(api), kind).add(NEW) is True
    assert api.patches == [{api.field: before + [NEW]}]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("rows", [25, 130])
def test_remove_keeps_every_other_row(kind: str, rows: int) -> None:
    api = _ProjectAPI(kind, public=rows, custom=3, deprecated=2)
    gone = api.public[-1]
    others = api.public[:-1]
    assert getattr(_client(api), kind).remove(gone) is True
    assert api.patches == [{api.field: others}]


@pytest.mark.parametrize("kind", KINDS)
def test_the_async_client_reads_every_page_too(kind: str) -> None:
    api = _ProjectAPI(kind, public=130, custom=42, deprecated=2)
    before = list(api.public)
    resource = getattr(_async_client(api), kind)

    async def main() -> Any:
        listed = await resource.get(type="public")
        custom = await resource.get(type="custom")
        added = await resource.add(NEW)
        removed = await resource.remove(before[-1])
        return listed, custom, added, removed

    listed, custom, added, removed = asyncio.run(main())
    assert [r.id for r in listed] == before[2:]  # the list hides the 2 deprecated rows
    assert [r.id for r in custom] == api.custom
    assert (added, removed) == (True, True)
    assert api.patches == [{api.field: before + [NEW]}, {api.field: before[:-1] + [NEW]}]


@pytest.mark.parametrize("kind", KINDS)
def test_an_api_that_ignores_the_page_does_not_make_it_loop(kind: str) -> None:
    api = _ProjectAPI(kind, public=250)
    api.ignore_page = True
    listed = getattr(_client(api), kind).get(type="public")
    assert listed is not None and [r.id for r in listed] == api.public[:100]
    assert len(api.queries) == 2
