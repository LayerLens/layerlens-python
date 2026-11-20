from __future__ import annotations

import json
import math
import time
import asyncio
import logging
import weakref
from typing import Any, Dict, Type, Union, TypeVar, Optional
from email.utils import mktime_tz, parsedate_tz

import httpx
from httpx import URL

from . import _exceptions
from ._utils import SensitiveHeadersFilter
from ._constants import NotGiven

ResponseT = TypeVar("ResponseT")

MAX_RETRIES = 2
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
INITIAL_RETRY_DELAY = 0.5
MAX_RETRY_DELAY = 8.0


log: logging.Logger = logging.getLogger(__name__)
log.addFilter(SensitiveHeadersFilter())


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """The seconds a ``Retry-After`` header asks to wait, or ``None`` without a usable one.

    RFC 9110 allows delay-seconds (``120``) or an HTTP-date (``Wed, 21 Oct 2026 07:28:00
    GMT``), which is read against the local clock, and is ``0`` once it has passed. A
    negative or non-finite number, or anything else, is no delay at all.
    """
    if value is None or not value.strip():
        return None
    try:
        seconds = float(value)
    except ValueError:
        when = parsedate_tz(value)
        if when is None:
            return None
        try:
            at = mktime_tz(when)
        except (ValueError, OverflowError):  # a date no calendar holds, such as the year 10000
            return None
        return max(float(at) - time.time(), 0.0)
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _request_for_error(err: httpx.RequestError, method: str, url: str) -> httpx.Request:
    """Best-effort httpx.Request for a transport error.

    ``err.request`` is set by httpx when the error is raised during a real
    request, but raises ``RuntimeError`` when unset (e.g. a synthetic error in a
    test); fall back to a minimal request so the SDK exception always has one.
    """
    try:
        return err.request
    except RuntimeError:
        return httpx.Request(method, url)


def _use_the_clients_timeout_unless_given(request: Dict[str, Any]) -> None:
    """Drop a ``timeout`` that was not given, so httpx uses the client's.

    Every resource method's ``timeout`` defaults to ``NOT_GIVEN``. They used to default to the
    600-second ``DEFAULT_TIMEOUT``, which, sent with every request, overrode the client's own:
    ``LayerLens(timeout=30)`` and ``with_options(timeout=10)`` reached no call (R1 F1). A timeout
    given to the call (``None`` included: no timeout) still wins.
    """
    if isinstance(request.get("timeout"), NotGiven):
        del request["timeout"]


class BaseClient(httpx.Client):
    _max_retries: int

    def __init__(
        self,
        *,
        base_url: URL | str,
        headers: Optional[Dict[str, str]] = None,
        timeout: Union[float, httpx.Timeout, None] = None,
        max_retries: int = MAX_RETRIES,
        **kwargs: Any,
    ):
        self._max_retries = max_retries
        super().__init__(base_url=base_url, headers=headers, timeout=timeout, **kwargs)

    @property
    def auth_headers(self) -> dict[str, str]:
        return {}

    @property
    def default_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **self.auth_headers,
        }

    def _request_cast(
        self,
        method: str,
        url: str,
        *,
        cast_to: Optional[Type[ResponseT]] = None,
        body: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        max_retries: Optional[int] = None,
        **kwargs: Any,
    ) -> Union[ResponseT, httpx.Response]:
        combined_headers = {**self.default_headers, **(headers or {})}
        retries_left = self._max_retries if max_retries is None else max_retries
        delay = INITIAL_RETRY_DELAY
        _use_the_clients_timeout_unless_given(kwargs)

        while True:
            try:
                response = super().request(
                    method=method,
                    url=url,
                    json=body,
                    params=params,
                    headers=combined_headers,
                    **kwargs,
                )
            except httpx.TimeoutException as err:
                raise _exceptions.APITimeoutError(request=_request_for_error(err, method, url)) from err
            except httpx.RequestError as err:
                raise _exceptions.APIConnectionError(request=_request_for_error(err, method, url)) from err

            if response.status_code in RETRY_STATUS_CODES and retries_left > 0:
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                sleep_time = min(delay if retry_after is None else retry_after, MAX_RETRY_DELAY)
                log.debug(
                    "Retrying request after %.1fs (status %d, %d retries left)",
                    sleep_time,
                    response.status_code,
                    retries_left,
                )
                time.sleep(sleep_time)
                delay = min(delay * 2, MAX_RETRY_DELAY)
                retries_left -= 1
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as err:
                log.debug("Encountered httpx.HTTPStatusError", exc_info=True)
                log.debug("Re-raising status error")
                raise self._make_status_error_from_response(err.response) from None

            if cast_to:
                try:
                    data = response.json()
                    return cast_to(**data)
                except Exception as err:
                    raise _exceptions.APIResponseValidationError(response, None) from err
            return response

    def get_cast(
        self,
        url: str,
        *,
        cast_to: Optional[Type[ResponseT]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Union[ResponseT, httpx.Response]:
        return self._request_cast("GET", url, cast_to=cast_to, params=params, headers=headers, **kwargs)

    def post_cast(
        self,
        url: str,
        *,
        cast_to: Optional[Type[ResponseT]] = None,
        body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Union[ResponseT, httpx.Response]:
        return self._request_cast("POST", url, cast_to=cast_to, body=body, headers=headers, **kwargs)

    def patch_cast(
        self,
        url: str,
        *,
        cast_to: Optional[Type[ResponseT]] = None,
        body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Union[ResponseT, httpx.Response]:
        return self._request_cast("PATCH", url, cast_to=cast_to, body=body, headers=headers, **kwargs)

    def delete_cast(
        self,
        url: str,
        *,
        cast_to: Optional[Type[ResponseT]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Union[ResponseT, httpx.Response]:
        return self._request_cast("DELETE", url, cast_to=cast_to, params=params, headers=headers, **kwargs)

    def _make_status_error_from_response(
        self,
        response: httpx.Response,
    ) -> _exceptions.APIStatusError:
        err_text = response.text.strip()
        body = err_text

        try:
            body = json.loads(err_text)
            err_msg = f"Error code: {response.status_code} - {body}"
        except Exception:
            err_msg = err_text or f"Error code: {response.status_code}"

        return self._make_status_error(err_msg, body=body, response=response)

    def _make_status_error(
        self,
        err_msg: str,
        *,
        body: object,
        response: httpx.Response,
    ) -> _exceptions.APIStatusError:
        raise NotImplementedError()


def _drop_pooled_connections(transport: Any) -> bool:
    """Forget the connections pooled in an httpx transport, without closing them.

    True when the transport's pool is one this knows: httpcore 1.x keeps its connections in
    ``_connections``, 0.x in ``_pool``; requests the dead loop left queued (``_requests``) go
    too. False for any other transport (a MockTransport, or a layout a later httpcore changes);
    the caller then closes it instead.
    """
    pool = getattr(transport, "_pool", None)
    known = False
    for name in ("_connections", "_pool", "_requests"):
        held = getattr(pool, name, None) if pool is not None else None
        if isinstance(held, list):
            held.clear()
            known = known or name != "_requests"
    return known


class BaseAsyncClient(httpx.AsyncClient):
    _max_retries: int

    def __init__(
        self,
        *,
        base_url: URL | str,
        headers: Optional[Dict[str, str]] = None,
        timeout: Union[float, httpx.Timeout, None] = None,
        max_retries: int = MAX_RETRIES,
        **kwargs: Any,
    ):
        self._max_retries = max_retries
        # The event loop of the last request (see _leave_a_closed_loop).
        self._last_loop: Optional["weakref.ReferenceType[asyncio.AbstractEventLoop]"] = None
        super().__init__(base_url=base_url, headers=headers, timeout=timeout, **kwargs)

    @property
    def auth_headers(self) -> dict[str, str]:
        return {}

    @property
    def default_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **self.auth_headers,
        }

    async def _leave_a_closed_loop(self) -> None:
        """Drop the pooled connections when the event loop of the last request is closed.

        A pooled keep-alive connection belongs to the event loop that opened it. One client used
        by two successive ``asyncio.run()`` calls (a module-level client, say) reused the first
        run's connection in the second, which raised ``RuntimeError: Event loop is closed``. The
        connections are dropped from the pool without being closed: a close can't run on the dead
        loop (httpcore before 1.0.3 tried each one before emptying its pool, failed on the first,
        and left them pooled). Their sockets close when they are garbage collected. Clients shared
        by loops running at once are left alone: httpx does not support that.
        """
        loop = asyncio.get_running_loop()
        previous = self._last_loop
        self._last_loop = weakref.ref(loop)
        if previous is None:
            return  # the client's first request
        last = previous()
        if last is loop or (last is not None and not last.is_closed()):
            return
        for transport in [self._transport, *getattr(self, "_mounts", {}).values()]:
            if transport is None or _drop_pooled_connections(transport):
                continue
            try:  # a pool this code does not know: close it, as httpcore >= 1.0.3 empties it first
                await transport.aclose()
            except Exception:  # its connections belong to the closed loop
                log.debug("Closing the connections of a closed event loop failed", exc_info=True)

    async def aclose(self) -> None:
        """Close the client, a module-level one too, from any event loop.

        Closing it in an ``asyncio.run()`` after the one that used it raised ``RuntimeError: Event
        loop is closed``, as closing the earlier loop's pooled connections can't run there.
        """
        await self._leave_a_closed_loop()
        await super().aclose()

    async def _request_cast(
        self,
        method: str,
        url: str,
        *,
        cast_to: Optional[Type[ResponseT]] = None,
        body: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        max_retries: Optional[int] = None,
        **kwargs: Any,
    ) -> Union[ResponseT, httpx.Response]:
        combined_headers = {**self.default_headers, **(headers or {})}
        retries_left = self._max_retries if max_retries is None else max_retries
        delay = INITIAL_RETRY_DELAY
        _use_the_clients_timeout_unless_given(kwargs)
        await self._leave_a_closed_loop()

        while True:
            try:
                response = await super().request(
                    method=method,
                    url=url,
                    json=body,
                    params=params,
                    headers=combined_headers,
                    **kwargs,
                )
            except httpx.TimeoutException as err:
                raise _exceptions.APITimeoutError(request=_request_for_error(err, method, url)) from err
            except httpx.RequestError as err:
                raise _exceptions.APIConnectionError(request=_request_for_error(err, method, url)) from err

            if response.status_code in RETRY_STATUS_CODES and retries_left > 0:
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                sleep_time = min(delay if retry_after is None else retry_after, MAX_RETRY_DELAY)
                log.debug(
                    "Retrying request after %.1fs (status %d, %d retries left)",
                    sleep_time,
                    response.status_code,
                    retries_left,
                )
                await asyncio.sleep(sleep_time)
                delay = min(delay * 2, MAX_RETRY_DELAY)
                retries_left -= 1
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as err:
                log.debug("Encountered httpx.HTTPStatusError", exc_info=True)
                log.debug("Re-raising status error")
                raise self._make_status_error_from_response(err.response) from None

            if cast_to:
                try:
                    data = response.json()
                    return cast_to(**data)
                except Exception as err:
                    raise _exceptions.APIResponseValidationError(response, None) from err
            return response

    async def get_cast(
        self,
        url: str,
        *,
        cast_to: Optional[Type[ResponseT]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Union[ResponseT, httpx.Response]:
        return await self._request_cast("GET", url, cast_to=cast_to, params=params, headers=headers, **kwargs)

    async def post_cast(
        self,
        url: str,
        *,
        cast_to: Optional[Type[ResponseT]] = None,
        body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Union[ResponseT, httpx.Response]:
        return await self._request_cast("POST", url, cast_to=cast_to, body=body, headers=headers, **kwargs)

    async def patch_cast(
        self,
        url: str,
        *,
        cast_to: Optional[Type[ResponseT]] = None,
        body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Union[ResponseT, httpx.Response]:
        return await self._request_cast("PATCH", url, cast_to=cast_to, body=body, headers=headers, **kwargs)

    async def delete_cast(
        self,
        url: str,
        *,
        cast_to: Optional[Type[ResponseT]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Union[ResponseT, httpx.Response]:
        return await self._request_cast("DELETE", url, cast_to=cast_to, params=params, headers=headers, **kwargs)

    def _make_status_error_from_response(
        self,
        response: httpx.Response,
    ) -> _exceptions.APIStatusError:
        err_text = response.text.strip()
        body = err_text

        try:
            body = json.loads(err_text)
            err_msg = f"Error code: {response.status_code} - {body}"
        except Exception:
            err_msg = err_text or f"Error code: {response.status_code}"

        return self._make_status_error(err_msg, body=body, response=response)

    def _make_status_error(
        self,
        err_msg: str,
        *,
        body: object,
        response: httpx.Response,
    ) -> _exceptions.APIStatusError:
        raise NotImplementedError()
