from typing import Union

import httpx

# default timeout is 10 minutes: the client's, which every call uses unless it is given one
DEFAULT_TIMEOUT = httpx.Timeout(timeout=600, connect=5.0)

DEFAULT_BASE_URL = "https://api.layerlens.ai/api/v1"

# The most rows the API serves in one page of a list (its ClampPageSize): asked for more, it
# serves 100. The SDK asks for at most this many, so a page it counts holds what the API sent:
# with 500, `get_many(page_size=500)` got 100 rows and counted pages of 500, and paging by
# `total_pages` skipped rows.
API_MAX_PAGE_SIZE = 100

# The "dirty" router prefix used by the backend for browser/session routes
DIRTY_ROUTER_PREFIX = "/dgklmnr"

# CLI auth endpoints (appended to base URL + dirty prefix)
AUTH_CLI_CONFIG_PATH = DIRTY_ROUTER_PREFIX + "/auth/cli-config"
AUTH_CLI_LOGIN_PATH = DIRTY_ROUTER_PREFIX + "/auth/cli-login"


class NotGiven:
    """An option was not given, where ``None`` is a value (``timeout=None`` is no timeout): the
    client's own is used. The default of ``copy()`` / ``with_options()`` and of every resource
    method's ``timeout``."""

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN = NotGiven()


def timeout_or(timeout: Union[float, httpx.Timeout, None, NotGiven], default: httpx.Timeout) -> httpx.Timeout:
    """``timeout`` as an httpx ``Timeout``, or ``default`` (the client's) when it was not given."""
    if isinstance(timeout, NotGiven):
        return default
    return timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)


def capped(timeout: object, seconds: float) -> httpx.Timeout:
    """``timeout`` with each part at most ``seconds``; a part with no limit gets ``seconds``.

    ``timeout`` is a client's (an httpx ``Timeout``); anything else counts as no limit.
    """
    parts = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(None)

    def cap(value: Union[float, None]) -> float:
        return seconds if value is None else min(value, seconds)

    return httpx.Timeout(connect=cap(parts.connect), read=cap(parts.read), write=cap(parts.write), pool=cap(parts.pool))
