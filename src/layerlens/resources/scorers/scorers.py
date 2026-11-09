from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
from pydantic import ValidationError

from ...models import Scorer, ScorersResponse
from ..._resource import SyncAPIResource, AsyncAPIResource
from ..._constants import DEFAULT_TIMEOUT
from ..._exceptions import LayerLensError

DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


def _unwrap(resp: Any) -> Any:
    if isinstance(resp, dict) and "data" in resp and "status" in resp:
        return resp["data"]
    return resp


def _pascal_to_snake(key: str) -> str:
    """Convert PascalCase key to snake_case."""
    import re

    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)).lower()


def _normalize_keys(d: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a dict's keys from PascalCase to snake_case if needed."""
    if not d or not isinstance(d, dict):
        return d
    # Check if keys are PascalCase (first key starts with uppercase)
    first_key = next(iter(d), "")
    if first_key and first_key[0].isupper():
        return {_pascal_to_snake(k): v for k, v in d.items()}
    return d


def _created(resp: Any) -> "Scorer | str":
    """What the API answered a create with: the whole scorer, or the new scorer's id.

    The API answers ``{"data": {"id": ...}}`` (BUG-05: ``Scorer(**data)`` on that failed for want of
    a name, and the scorer that had been created was reported as a failure). A whole scorer that
    does not validate raises, as does an answer with neither.
    """
    data = _unwrap(resp)
    if isinstance(data, dict):
        data = _normalize_keys(data)
        new_id = data.get("id")
        if "name" in data:
            try:
                return Scorer(**data)
            except ValidationError:
                # The scorer exists: read it back by its id rather than lose it to the reply's shape.
                if isinstance(new_id, str) and new_id:
                    return new_id
                raise
        if isinstance(new_id, str) and new_id:
            return new_id
    raise LayerLensError(f"the API answered the scorer create without the new scorer: {data!r}")


class Scorers(SyncAPIResource):
    def _base_url(self) -> str:
        return f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/scorers"

    def get(self, id: str, *, timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT) -> Optional[Scorer]:
        resp = self._get(f"{self._base_url()}/{id}", timeout=timeout, cast_to=dict)
        data = _unwrap(resp)
        if isinstance(data, dict):
            try:
                return Scorer(**data)
            except Exception:
                return None
        return None

    def get_many(
        self,
        *,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Optional[ScorersResponse]:
        params: Dict[str, Any] = {}
        effective_page_size = min(max(page_size, 1), MAX_PAGE_SIZE) if page_size is not None else DEFAULT_PAGE_SIZE
        effective_page = page if page is not None else DEFAULT_PAGE
        params["page"] = str(effective_page)
        params["page_size"] = str(effective_page_size)

        resp = self._get(self._base_url(), params=params, timeout=timeout, cast_to=dict)
        if not resp or not isinstance(resp, dict):
            return None
        data = _unwrap(resp)
        if not isinstance(data, dict):
            return None

        scorers = [Scorer(**s) if isinstance(s, dict) else s for s in data.get("scorers", [])]
        count: int = data.get("count", len(scorers))
        total_count: int = data.get("total_count", count)
        try:
            return ScorersResponse(scorers=scorers, count=count, total_count=total_count)
        except Exception:
            return None

    def create(
        self,
        *,
        name: str,
        description: str,
        model_id: str,
        prompt: str,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Scorer:
        """Create a scorer and return it, as stored.

        The API answers with the new scorer's id; the scorer is read back by it. If that read
        fails, the scorer still exists, so it is returned as sent, with its id. Raises
        ``LayerLensError`` when the answer holds no new scorer, never returns ``None``.
        """
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "model_id": model_id,
            "prompt": prompt,
        }
        resp = self._post(self._base_url(), body=body, timeout=timeout, cast_to=dict)
        data = _created(resp)
        if isinstance(data, Scorer):
            return data
        try:
            stored = self.get(data, timeout=timeout)
        except LayerLensError:
            stored = None
        return stored or Scorer(id=data, **body)

    def update(
        self,
        id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        model_id: Optional[str] = None,
        prompt: Optional[str] = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> bool:
        body: Dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if model_id is not None:
            body["model_id"] = model_id
        if prompt is not None:
            body["prompt"] = prompt
        try:
            self._patch(f"{self._base_url()}/{id}", body=body, timeout=timeout, cast_to=dict)
            return True
        except Exception:
            return False

    def delete(self, id: str, *, timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT) -> bool:
        try:
            self._delete(f"{self._base_url()}/{id}", timeout=timeout, cast_to=dict)
            return True
        except Exception:
            return False


class AsyncScorers(AsyncAPIResource):
    def _base_url(self) -> str:
        return f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/scorers"

    async def get(self, id: str, *, timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT) -> Optional[Scorer]:
        resp = await self._get(f"{self._base_url()}/{id}", timeout=timeout, cast_to=dict)
        data = _unwrap(resp)
        if isinstance(data, dict):
            try:
                return Scorer(**data)
            except Exception:
                return None
        return None

    async def get_many(
        self,
        *,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Optional[ScorersResponse]:
        params: Dict[str, Any] = {}
        effective_page_size = min(max(page_size, 1), MAX_PAGE_SIZE) if page_size is not None else DEFAULT_PAGE_SIZE
        effective_page = page if page is not None else DEFAULT_PAGE
        params["page"] = str(effective_page)
        params["page_size"] = str(effective_page_size)
        resp = await self._get(self._base_url(), params=params, timeout=timeout, cast_to=dict)
        if not resp or not isinstance(resp, dict):
            return None
        data = _unwrap(resp)
        if not isinstance(data, dict):
            return None
        scorers = [Scorer(**s) if isinstance(s, dict) else s for s in data.get("scorers", [])]
        count: int = data.get("count", len(scorers))
        total_count: int = data.get("total_count", count)
        try:
            return ScorersResponse(scorers=scorers, count=count, total_count=total_count)
        except Exception:
            return None

    async def create(
        self,
        *,
        name: str,
        description: str,
        model_id: str,
        prompt: str,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Scorer:
        """Create a scorer and return it, as stored.

        The API answers with the new scorer's id; the scorer is read back by it. If that read
        fails, the scorer still exists, so it is returned as sent, with its id. Raises
        ``LayerLensError`` when the answer holds no new scorer, never returns ``None``.
        """
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "model_id": model_id,
            "prompt": prompt,
        }
        resp = await self._post(self._base_url(), body=body, timeout=timeout, cast_to=dict)
        data = _created(resp)
        if isinstance(data, Scorer):
            return data
        try:
            stored = await self.get(data, timeout=timeout)
        except LayerLensError:
            stored = None
        return stored or Scorer(id=data, **body)

    async def update(
        self,
        id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        model_id: Optional[str] = None,
        prompt: Optional[str] = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> bool:
        body: Dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if model_id is not None:
            body["model_id"] = model_id
        if prompt is not None:
            body["prompt"] = prompt
        try:
            await self._patch(f"{self._base_url()}/{id}", body=body, timeout=timeout, cast_to=dict)
            return True
        except Exception:
            return False

    async def delete(self, id: str, *, timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT) -> bool:
        try:
            await self._delete(f"{self._base_url()}/{id}", timeout=timeout, cast_to=dict)
            return True
        except Exception:
            return False
