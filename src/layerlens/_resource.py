from __future__ import annotations

import time
import asyncio
import inspect
import functools
from typing import TYPE_CHECKING, Any, Callable, Awaitable

if TYPE_CHECKING:
    from ._client import LayerLens, AsyncLayerLens
    from ._public_client import PublicClient, AsyncPublicClient


class SyncAPIResource:
    _client: LayerLens

    def __init__(self, client: LayerLens) -> None:
        self._client = client
        self._get = client.get_cast
        self._post = client.post_cast
        self._patch = client.patch_cast
        self._delete = client.delete_cast

    def _sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _after_organization(method: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(method)
    async def awaiting_organization(self: AsyncOrganizationScoped, *args: Any, **kwargs: Any) -> Any:
        client = self._organization_client()
        # A test double for the client has no such method: nothing to read then.
        read = getattr(type(client), "_organization_scope_async", None)
        if read is not None:
            await read(client)
        return await method(self, *args, **kwargs)

    awaiting_organization._awaits_organization = True  # type: ignore[attr-defined]
    return awaiting_organization


class AsyncOrganizationScoped:
    """Every coroutine method a subclass defines awaits the client's organization and project first.

    Async resources build their URLs from ``client.organization_id`` / ``project_id``. Read the
    first time without this, those properties made a blocking HTTP call on the event loop. Each
    coroutine now awaits :meth:`AsyncLayerLens._organization_scope_async` (a no-op once the ids are
    known), so the properties only ever return ids already read. An async generator method reads
    them through the coroutines it awaits.
    """

    # False on a resource none of whose URLs holds the ids, so it never waits for them.
    _reads_organization: bool = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls._reads_organization:
            return
        for name, member in list(vars(cls).items()):
            if inspect.iscoroutinefunction(member) and not getattr(member, "_awaits_organization", False):
                setattr(cls, name, _after_organization(member))

    def _organization_client(self) -> Any:
        raise NotImplementedError


class AsyncAPIResource(AsyncOrganizationScoped):
    _client: AsyncLayerLens

    def _organization_client(self) -> Any:
        return self._client

    def __init__(self, client: AsyncLayerLens) -> None:
        self._client = client
        self._get = client.get_cast
        self._post = client.post_cast
        self._patch = client.patch_cast
        self._delete = client.delete_cast

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SyncPublicAPIResource:
    _client: PublicClient

    def __init__(self, client: PublicClient) -> None:
        self._client = client
        self._get = client.get_cast

    def _sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class AsyncPublicAPIResource:
    _client: AsyncPublicClient

    def __init__(self, client: AsyncPublicClient) -> None:
        self._client = client
        self._get = client.get_cast

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
