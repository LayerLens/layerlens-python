"""Sending a file to a presigned storage URL (trace and benchmark uploads).

A presigned URL's query string is its credential and signature (``X-Amz-Credential``,
``X-Amz-Signature``, ...): whoever has the URL can write that object until it expires. The PUT
raised httpx's ``HTTPStatusError``, whose text quotes the whole URL, and httpx logs every request's
full URL at INFO level. Here no error the SDK raises carries the query, and httpx's log lines
show these URLs without it.

The PUT is retried as the client retries its own calls: a 429 or 5xx (S3's "503 Slow Down") is
sent again after half a second, then twice as long each time, up to 8 seconds, and never sooner
than a ``Retry-After``; so is a connection that was refused or dropped, since a PUT of the same
bytes can be sent twice. A timeout is not retried. Only a 2xx answer stores the file: a redirect is
not followed, so it raises as an error does.
"""

from __future__ import annotations

import re
import time
import asyncio
import logging
from typing import Any, Dict, Type, Tuple, Union, Optional
from urllib.parse import urlsplit, parse_qsl

import httpx

from ._exceptions import (
    ConflictError,
    NotFoundError,
    APIStatusError,
    RateLimitError,
    APITimeoutError,
    BadRequestError,
    APIConnectionError,
    AuthenticationError,
    InternalServerError,
    PaymentRequiredError,
    PermissionDeniedError,
    UnprocessableEntityError,
)
from ._base_client import MAX_RETRIES, MAX_RETRY_DELAY, RETRY_STATUS_CODES, INITIAL_RETRY_DELAY, parse_retry_after

# Query keys that sign a URL: AWS SigV4, Google Cloud Storage, Azure SAS.
_SIGNING_KEYS = frozenset(
    {
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "x-goog-signature",
        "x-goog-credential",
        "sig",
        "signature",
    }
)

_STATUS_ERRORS: Dict[int, Type[APIStatusError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    402: PaymentRequiredError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
    429: RateLimitError,
}

# A connection refused, reset or dropped before an answer: sent again. Not a timeout.
_RETRIED_ERRORS: Tuple[Type[Exception], ...] = (httpx.NetworkError, httpx.RemoteProtocolError)


# A URL inside log text, up to whitespace or a quote.
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+")


def is_signed(url: Any) -> bool:
    """Whether ``url`` (a string or an ``httpx.URL``) carries a signature in its query."""
    if not isinstance(url, str) and type(url).__name__ != "URL":
        return False
    query = urlsplit(str(url)).query
    return any(key.lower() in _SIGNING_KEYS for key, _ in parse_qsl(query, keep_blank_values=True))


def _without_signed_queries(text: str) -> str:
    return _URL_IN_TEXT.sub(lambda m: without_query(m.group(0)) if is_signed(m.group(0)) else m.group(0), text)


def without_query(url: Union[str, httpx.URL]) -> str:
    """``url`` without its query string and fragment."""
    return urlsplit(str(url))._replace(query="", fragment="").geturl()


class _SignedURLFilter(logging.Filter):
    """Shows a signed URL in a log line without its query string.

    In the arguments, and in the message itself: another filter on the logger may have formatted
    the record already (litellm's SecretRedactionFilter does, on the "httpx" logger).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and any(is_signed(arg) for arg in args):
            record.args = tuple(without_query(str(arg)) if is_signed(arg) else arg for arg in args)
        if isinstance(record.msg, str) and "://" in record.msg:
            record.msg = _without_signed_queries(record.msg)
        return True


# The loggers httpx logs each request's URL on: "httpx" since 0.24, "httpx._client" in 0.23. A
# logger's filters don't see the records of its children, so each gets the filter.
_HTTPX_LOGGERS = ("httpx", "httpx._client")


def _hide_signed_urls_in_httpx_logs() -> None:
    for name in _HTTPX_LOGGERS:
        log = logging.getLogger(name)
        if not any(isinstance(existing, _SignedURLFilter) for existing in log.filters):
            # First, so the record is clean before any other filter formats or copies it.
            log.filters.insert(0, _SignedURLFilter())


def _max_retries(client: Any) -> int:
    retries = getattr(client, "_max_retries", None)
    return retries if isinstance(retries, int) else MAX_RETRIES


def _status_error(response: httpx.Response, url: str, what: str) -> APIStatusError:
    """The SDK's error for a refused PUT, on a request and a response that hold no query, nor the
    body (S3's SignatureDoesNotMatch body quotes the signed request). A header that holds a signed
    URL, a redirect's ``Location``, shows it without its query."""
    request = httpx.Request("PUT", without_query(url))
    headers = [
        (name, without_query(value) if is_signed(value) else _without_signed_queries(value))
        for name, value in response.headers.multi_items()
    ]
    shown = httpx.Response(response.status_code, headers=headers, request=request)
    reason = f" {response.reason_phrase}" if response.reason_phrase else ""
    message = f"the {what} could not be stored: the storage service answered {response.status_code}{reason}"
    status = response.status_code
    cls = _STATUS_ERRORS.get(status, InternalServerError if status >= 500 else APIStatusError)
    return cls(message, response=shown, body=None)


def _connection_error(url: str, what: str) -> APIConnectionError:
    return APIConnectionError(
        message=f"Connection error: the {what} could not be sent to storage.",
        request=httpx.Request("PUT", without_query(url)),
    )


def _stored(response: Optional[httpx.Response], timed_out: bool, url: str, what: str, retries_left: int) -> bool:
    """Whether the PUT stored the file (True) or is to be sent again (False); raises the SDK's error
    when it can't be.

    Called after the ``except`` blocks: an error raised inside one kept httpx's error as its
    ``__context__`` (``from None`` only hides it from the traceback), and that error's request
    holds the signed URL.
    """
    if timed_out:
        raise APITimeoutError(request=httpx.Request("PUT", without_query(url)))
    if response is None:  # refused or dropped
        if retries_left <= 0:
            raise _connection_error(url, what)
        return False
    if 200 <= response.status_code < 300:  # a redirect is not followed, so it stores nothing
        return True
    if response.status_code not in RETRY_STATUS_CODES or retries_left <= 0:
        raise _status_error(response, url, what)
    return False


def _pause(response: Optional[httpx.Response], delay: float) -> float:
    retry_after = parse_retry_after(response.headers.get("retry-after")) if response is not None else None
    return min(delay if retry_after is None else retry_after, MAX_RETRY_DELAY)


def put_presigned(
    url: str,
    content: bytes,
    *,
    content_type: str,
    timeout: Union[float, httpx.Timeout, None],
    client: Any,
    what: str,
) -> None:
    """PUT ``content`` to the presigned ``url``, retried as ``client`` retries its own calls."""
    _hide_signed_urls_in_httpx_logs()
    retries_left, delay = _max_retries(client), INITIAL_RETRY_DELAY
    while True:
        response: Optional[httpx.Response] = None
        timed_out = False
        try:
            response = httpx.put(url, content=content, headers={"Content-Type": content_type}, timeout=timeout)
        except httpx.TimeoutException:
            timed_out = True
        except _RETRIED_ERRORS:
            response = None  # refused or dropped
        if _stored(response, timed_out, url, what, retries_left):
            return
        time.sleep(_pause(response, delay))
        delay, retries_left = min(delay * 2, MAX_RETRY_DELAY), retries_left - 1


async def put_presigned_async(
    url: str,
    content: bytes,
    *,
    content_type: str,
    timeout: Union[float, httpx.Timeout, None],
    client: Any,
    what: str,
) -> None:
    """:func:`put_presigned` on an async client's event loop."""
    _hide_signed_urls_in_httpx_logs()
    retries_left, delay = _max_retries(client), INITIAL_RETRY_DELAY
    async with httpx.AsyncClient() as http:
        while True:
            response: Optional[httpx.Response] = None
            timed_out = False
            try:
                response = await http.put(url, content=content, headers={"Content-Type": content_type}, timeout=timeout)
            except httpx.TimeoutException:
                timed_out = True
            except _RETRIED_ERRORS:
                response = None  # refused or dropped
            if _stored(response, timed_out, url, what, retries_left):
                return
            await asyncio.sleep(_pause(response, delay))
            delay, retries_left = min(delay * 2, MAX_RETRY_DELAY), retries_left - 1
