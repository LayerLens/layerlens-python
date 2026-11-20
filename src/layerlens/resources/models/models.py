from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

import httpx

from ...models import (
    Model,
    CustomModel,
    PublicModel,
    ModelsResponse,
    CreateModelResponse,
)


def _exclude_custom_models(
    models: List[Model],
    *,
    categories: Optional[List[str]] = None,
    companies: Optional[List[str]] = None,
    regions: Optional[List[str]] = None,
    licenses: Optional[List[str]] = None,
) -> List[Model]:
    """Exclude custom models when filtering by fields they don't have.

    The API correctly filters public models and custom models by name/key,
    but custom models don't have categories/companies/regions/licenses fields,
    so they must be excluded from results when those filters are active.
    """
    if categories:
        cat_set = {c.lower() for c in categories}

        def matches_category(m: Model) -> bool:
            if not isinstance(m, PublicModel):
                return False
            arch = (m.architecture_type or "").lower()
            for cat in cat_set:
                if cat == "open-source" and m.open_weights:
                    return True
                if cat == "closed-source" and not m.open_weights and arch:
                    return True
                if arch and cat == arch:
                    return True
            return False

        models = [m for m in models if matches_category(m)]

    if companies:
        comp_set = {c.lower() for c in companies}
        models = [m for m in models if isinstance(m, PublicModel) and m.company and m.company.lower() in comp_set]

    if regions:
        reg_set = {r.lower() for r in regions}
        models = [m for m in models if isinstance(m, PublicModel) and m.region and m.region.lower() in reg_set]

    if licenses:
        lic_set = {l.lower() for l in licenses}
        models = [m for m in models if isinstance(m, PublicModel) and m.license and m.license.lower() in lic_set]

    return models


from ..._pages import every_page, page_params, every_page_async
from ..._resource import SyncAPIResource, AsyncAPIResource
from ..._constants import NOT_GIVEN, NotGiven


class Models(SyncAPIResource):
    def get(
        self,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
        type: Literal["custom", "public"] | None = None,
        name: Optional[str] = None,
        key: Optional[str] = None,
        categories: Optional[List[str]] = None,
        companies: Optional[List[str]] = None,
        regions: Optional[List[str]] = None,
        licenses: Optional[List[str]] = None,
    ) -> Optional[List[Model]]:
        """Every model of the project that matches the filters, custom and public unless ``type`` says one.

        Returns every matching row: the SDK reads the API's pages for you.
        """

        def fetch(model_type: str) -> List[Model]:
            params = {"type": model_type}
            if name:
                params["name"] = name
            if key:
                params["key"] = key
            if categories:
                params["categories"] = ",".join(categories)
            if companies:
                params["companies"] = ",".join(companies)
            if regions:
                params["regions"] = ",".join(regions)
            if licenses:
                params["licenses"] = ",".join(licenses)
            return self._every_row(params, timeout)

        models: List[Model] = []

        def cast_model(m: Model, model_type: str) -> Model:
            if model_type == "custom":
                return CustomModel(**m.model_dump())
            elif model_type == "public":
                return PublicModel(**m.model_dump())
            return m  # fallback, just base class

        if type is None:  # fetch both
            for t in ["custom", "public"]:
                models.extend([cast_model(m, t) for m in fetch(t)])
        else:  # fetch only one type
            models.extend([cast_model(m, type) for m in fetch(type)])

        models = _exclude_custom_models(
            models,
            categories=categories,
            companies=companies,
            regions=regions,
            licenses=licenses,
        )

        return models

    def get_by_id(self, id: str, *, timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN) -> Optional[Model]:
        """A model, by id: one of the catalog or a custom one of the project.

        Raises :class:`NotFoundError` when no model has this id. Returns None only for a reply that
        holds no model.
        """
        base_url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/models/{id}"

        resp = self._get(
            base_url,
            timeout=timeout,
            cast_to=dict,
        )

        if not isinstance(resp, dict):
            return None

        model = resp.get("data")
        if not isinstance(model, dict):
            return None

        # Detect type dynamically: presence of "organization_id" means custom
        if "organization_id" in model:
            return CustomModel(**model)
        else:
            return PublicModel(**model)

    def get_by_key(
        self,
        key: str,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[Model]:
        """Fetch a single model of the project by its unique key; None when none has this key."""
        models = self.get(timeout=timeout, key=key)

        if not models:
            return None

        for model in models:
            if model.key == key:
                return model
        return None

    def add(
        self,
        *model_ids: str,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> bool:
        """Add public models to the project by their IDs.

        The project's PATCH takes its whole ``models`` list in one payload, and that list holds the
        project's public models only: a custom model belongs to the project it was created in, and
        the API lists it by that. A custom model's id in ``models`` made the project's public model
        list answer 500 for every caller, so the current list is read, and sent, public ids only, as
        :meth:`Benchmarks.add` does (BUG-11). It is read whole, every page and the deprecated models
        too, so no model the project holds is dropped.
        """
        current_ids = self._public_ids(timeout)
        new_ids = list(dict.fromkeys(current_ids + list(model_ids)))
        return self._patch_project_models(new_ids, timeout)

    def remove(
        self,
        *model_ids: str,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> bool:
        """Remove public models from the project's model list (see :meth:`add`).

        A custom model is not in that list: it belongs to its project until ``delete_custom``.
        """
        remove_set = set(model_ids)
        new_ids = [model_id for model_id in self._public_ids(timeout) if model_id not in remove_set]
        return self._patch_project_models(new_ids, timeout)

    def _every_row(
        self,
        params: Dict[str, str],
        timeout: float | httpx.Timeout | None | NotGiven,
    ) -> List[Model]:
        """Every row the project's models list answers for ``params``, read a page at a time."""
        url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/models"

        def page_of(page: int) -> Optional[List[Model]]:
            resp = self._get(
                url,
                params={**params, **page_params(page)},
                timeout=timeout,
                cast_to=ModelsResponse,
            )
            return resp.data.models if isinstance(resp, ModelsResponse) else None

        return every_page(page_of)

    def _public_ids(self, timeout: float | httpx.Timeout | None | NotGiven) -> List[str]:
        """The ids of every public model the project holds, deprecated ones too.

        The PATCH replaces the project's whole list, so ``add()`` and ``remove()`` send back every row
        it holds: the list hides a deprecated model unless asked for it, and it answers 20 rows a page
        unless asked for more.
        """
        rows = self._every_row({"type": "public", "include_deprecated": "true"}, timeout)
        return [str(m.id) for m in rows]

    def _patch_project_models(
        self,
        model_ids: List[str],
        timeout: float | httpx.Timeout | None | NotGiven,
    ) -> bool:
        url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}"
        resp = self._patch(
            url,
            body={"models": model_ids},
            timeout=timeout,
            cast_to=dict,
        )
        if isinstance(resp, dict):
            data = resp.get("data", resp)
            if isinstance(data, dict) and "id" in data:
                return True
        return False

    def create_custom(
        self,
        *,
        name: str,
        key: str,
        description: str,
        api_url: str,
        max_tokens: int,
        api_key: Optional[str] = None,
        extra_payload: Optional[Dict[str, Any]] = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[CreateModelResponse]:
        """Create a custom model backed by an OpenAI-compatible API.

        Args:
            name: Model name (max 256 characters).
            key: Unique model key, lowercase alphanumeric with dots/hyphens/slashes (max 256 characters).
            description: Model description (max 500 characters).
            api_url: Base URL of the OpenAI-compatible API endpoint.
            max_tokens: Maximum number of tokens the model supports.
            api_key: Optional API key for the model provider.
            extra_payload: Optional JSON object merged into every outgoing
                chat-completions request body. Customer values win on conflict
                with our defaults (e.g. set ``{"temperature": 1}`` for
                providers that reject ``temperature: 0``). Keys ``messages``,
                ``model`` and ``stream`` are reserved.
            timeout: Request timeout override.

        Returns:
            CreateModelResponse with model_id, or None on failure.
        """
        base = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}"
        body: Dict[str, Any] = {
            "name": name,
            "key": key,
            "description": description,
            "api_url": api_url,
            "max_tokens": max_tokens,
        }
        if api_key is not None:
            body["api_key"] = api_key
        if extra_payload is not None:
            body["extra_payload"] = extra_payload

        resp = self._post(
            f"{base}/custom-models",
            body=body,
            timeout=timeout,
            cast_to=dict,
        )
        if isinstance(resp, dict) and "data" in resp and "status" in resp:
            resp = resp["data"]
        if isinstance(resp, dict) and "model_id" in resp:
            return CreateModelResponse(**resp)
        return None

    def update_custom(
        self,
        model_id: str,
        *,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        max_tokens: Optional[int] = None,
        extra_payload: Optional[Dict[str, Any]] = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> bool:
        """Update a custom model's mutable fields.

        At least one of ``api_url``, ``api_key``, ``max_tokens`` or
        ``extra_payload`` must be provided. Returns ``True`` on success.

        Primary use case: repointing ``api_url`` for ephemeral vLLM endpoints
        behind cloudflared tunnels whose URL changes between sessions.

        Args:
            model_id: ID of the custom model to update.
            api_url: New base URL for the OpenAI-compatible API endpoint.
            api_key: New API key for the model provider.
            max_tokens: New maximum tokens value.
            extra_payload: New JSON object merged into every outgoing request
                body. Pass ``{}`` to clear the existing payload. See
                ``create_custom`` for semantics.
            timeout: Request timeout override.
        """
        url = (
            f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/custom-models/{model_id}"
        )
        body: Dict[str, Any] = {}
        if api_url is not None:
            body["api_url"] = api_url
        if api_key is not None:
            body["api_key"] = api_key
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if extra_payload is not None:
            body["extra_payload"] = extra_payload
        resp = self._patch(url, body=body, timeout=timeout, cast_to=dict)
        return isinstance(resp, dict) and "data" in resp

    def delete_custom(
        self,
        model_id: str,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> bool:
        """Disable a custom model and detach it from the project.

        The backend tears down the model's S3 yaml artifacts and the AWS
        secret, marks the record as disabled (preserving evaluation
        references), and removes the model ID from ``Project.Models``.
        """
        url = (
            f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/custom-models/{model_id}"
        )
        resp = self._delete(url, timeout=timeout, cast_to=dict)
        return isinstance(resp, dict) and "data" in resp


class AsyncModels(AsyncAPIResource):
    async def get(
        self,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
        type: Literal["custom", "public"] | None = None,
        name: Optional[str] = None,
        key: Optional[str] = None,
        categories: Optional[List[str]] = None,
        companies: Optional[List[str]] = None,
        regions: Optional[List[str]] = None,
        licenses: Optional[List[str]] = None,
    ) -> Optional[List[Model]]:
        """Every model of the project that matches the filters, custom and public unless ``type`` says one.

        Returns every matching row: the SDK reads the API's pages for you.
        """

        async def fetch(model_type: str) -> List[Model]:
            params = {"type": model_type}
            if name:
                params["name"] = name
            if key:
                params["key"] = key
            if categories:
                params["categories"] = ",".join(categories)
            if companies:
                params["companies"] = ",".join(companies)
            if regions:
                params["regions"] = ",".join(regions)
            if licenses:
                params["licenses"] = ",".join(licenses)
            return await self._every_row(params, timeout)

        models: List[Model] = []

        def cast_model(m: Model, model_type: str) -> Model:
            if model_type == "custom":
                return CustomModel(**m.model_dump())
            elif model_type == "public":
                return PublicModel(**m.model_dump())
            return m  # fallback, just base class

        if type is None:  # fetch both
            for t in ["custom", "public"]:
                models.extend([cast_model(m, t) for m in await fetch(t)])
        else:  # fetch only one type
            models.extend([cast_model(m, type) for m in await fetch(type)])

        models = _exclude_custom_models(
            models,
            categories=categories,
            companies=companies,
            regions=regions,
            licenses=licenses,
        )

        return models

    async def get_by_id(
        self, id: str, *, timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN
    ) -> Optional[Model]:
        """A model, by id: one of the catalog or a custom one of the project.

        Raises :class:`NotFoundError` when no model has this id. Returns None only for a reply that
        holds no model.
        """
        base_url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/models/{id}"

        resp = await self._get(
            base_url,
            timeout=timeout,
            cast_to=dict,
        )

        if not isinstance(resp, dict):
            return None

        model = resp.get("data")
        if not isinstance(model, dict):
            return None

        # Detect type dynamically: presence of "organization_id" means custom
        if "organization_id" in model:
            return CustomModel(**model)
        else:
            return PublicModel(**model)

    async def get_by_key(
        self,
        key: str,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[Model]:
        """Fetch a single model of the project by its unique key; None when none has this key."""
        models = await self.get(timeout=timeout, key=key)

        if not models:
            return None

        for model in models:
            if model.key == key:
                return model
        return None

    async def add(
        self,
        *model_ids: str,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> bool:
        """Add public models to the project by their IDs (see :meth:`Models.add`)."""
        current_ids = await self._public_ids(timeout)
        new_ids = list(dict.fromkeys(current_ids + list(model_ids)))
        return await self._patch_project_models(new_ids, timeout)

    async def remove(
        self,
        *model_ids: str,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> bool:
        """Remove public models from the project's model list (see :meth:`Models.add`)."""
        remove_set = set(model_ids)
        new_ids = [model_id for model_id in await self._public_ids(timeout) if model_id not in remove_set]
        return await self._patch_project_models(new_ids, timeout)

    async def _every_row(
        self,
        params: Dict[str, str],
        timeout: float | httpx.Timeout | None | NotGiven,
    ) -> List[Model]:
        """Every row the project's models list answers for ``params``, read a page at a time."""
        url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/models"

        async def page_of(page: int) -> Optional[List[Model]]:
            resp = await self._get(
                url,
                params={**params, **page_params(page)},
                timeout=timeout,
                cast_to=ModelsResponse,
            )
            return resp.data.models if isinstance(resp, ModelsResponse) else None

        return await every_page_async(page_of)

    async def _public_ids(self, timeout: float | httpx.Timeout | None | NotGiven) -> List[str]:
        """The ids of every public model the project holds, deprecated ones too.

        The PATCH replaces the project's whole list, so ``add()`` and ``remove()`` send back every row
        it holds: the list hides a deprecated model unless asked for it, and it answers 20 rows a page
        unless asked for more.
        """
        rows = await self._every_row({"type": "public", "include_deprecated": "true"}, timeout)
        return [str(m.id) for m in rows]

    async def _patch_project_models(
        self,
        model_ids: List[str],
        timeout: float | httpx.Timeout | None | NotGiven,
    ) -> bool:
        url = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}"
        resp = await self._patch(
            url,
            body={"models": model_ids},
            timeout=timeout,
            cast_to=dict,
        )
        if isinstance(resp, dict):
            data = resp.get("data", resp)
            if isinstance(data, dict) and "id" in data:
                return True
        return False

    async def create_custom(
        self,
        *,
        name: str,
        key: str,
        description: str,
        api_url: str,
        max_tokens: int,
        api_key: Optional[str] = None,
        extra_payload: Optional[Dict[str, Any]] = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[CreateModelResponse]:
        """Create a custom model backed by an OpenAI-compatible API.

        Args:
            name: Model name (max 256 characters).
            key: Unique model key, lowercase alphanumeric with dots/hyphens/slashes (max 256 characters).
            description: Model description (max 500 characters).
            api_url: Base URL of the OpenAI-compatible API endpoint.
            max_tokens: Maximum number of tokens the model supports.
            api_key: Optional API key for the model provider.
            extra_payload: Optional JSON object merged into every outgoing
                chat-completions request body. Customer values win on conflict
                with our defaults (e.g. set ``{"temperature": 1}`` for
                providers that reject ``temperature: 0``). Keys ``messages``,
                ``model`` and ``stream`` are reserved.
            timeout: Request timeout override.

        Returns:
            CreateModelResponse with model_id, or None on failure.
        """
        base = f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}"
        body: Dict[str, Any] = {
            "name": name,
            "key": key,
            "description": description,
            "api_url": api_url,
            "max_tokens": max_tokens,
        }
        if api_key is not None:
            body["api_key"] = api_key
        if extra_payload is not None:
            body["extra_payload"] = extra_payload

        resp = await self._post(
            f"{base}/custom-models",
            body=body,
            timeout=timeout,
            cast_to=dict,
        )
        if isinstance(resp, dict) and "data" in resp and "status" in resp:
            resp = resp["data"]
        if isinstance(resp, dict) and "model_id" in resp:
            return CreateModelResponse(**resp)
        return None

    async def update_custom(
        self,
        model_id: str,
        *,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        max_tokens: Optional[int] = None,
        extra_payload: Optional[Dict[str, Any]] = None,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> bool:
        """Update a custom model's mutable fields.

        At least one of ``api_url``, ``api_key``, ``max_tokens`` or
        ``extra_payload`` must be provided. Returns ``True`` on success.

        Args:
            model_id: ID of the custom model to update.
            api_url: New base URL for the OpenAI-compatible API endpoint.
            api_key: New API key for the model provider.
            max_tokens: New maximum tokens value.
            extra_payload: New JSON object merged into every outgoing request
                body. Pass ``{}`` to clear the existing payload. See
                ``create_custom`` for semantics.
            timeout: Request timeout override.
        """
        url = (
            f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/custom-models/{model_id}"
        )
        body: Dict[str, Any] = {}
        if api_url is not None:
            body["api_url"] = api_url
        if api_key is not None:
            body["api_key"] = api_key
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if extra_payload is not None:
            body["extra_payload"] = extra_payload
        resp = await self._patch(url, body=body, timeout=timeout, cast_to=dict)
        return isinstance(resp, dict) and "data" in resp

    async def delete_custom(
        self,
        model_id: str,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> bool:
        """Disable a custom model and detach it from the project."""
        url = (
            f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/custom-models/{model_id}"
        )
        resp = await self._delete(url, timeout=timeout, cast_to=dict)
        return isinstance(resp, dict) and "data" in resp
