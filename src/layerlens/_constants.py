import httpx

# default timeout is 10 minutes
DEFAULT_TIMEOUT = httpx.Timeout(timeout=600, connect=5.0)

DEFAULT_BASE_URL = "https://api.layerlens.ai/api/v1"

# The "dirty" router prefix used by the backend for browser/session routes
DIRTY_ROUTER_PREFIX = "/dgklmnr"

# CLI auth endpoints (appended to base URL + dirty prefix)
AUTH_CLI_CONFIG_PATH = DIRTY_ROUTER_PREFIX + "/auth/cli-config"
AUTH_CLI_LOGIN_PATH = DIRTY_ROUTER_PREFIX + "/auth/cli-login"


class NotGiven:
    """The default of an option ``copy()`` / ``with_options()`` was not given, where ``None`` is a
    value (``timeout=None`` is no timeout): the client's own is kept."""

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN = NotGiven()
