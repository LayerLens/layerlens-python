"""Credential storage, token refresh, and CLI login via backend."""

from __future__ import annotations

import json
import time
import base64
import binascii
from typing import Any, Dict, Optional
from pathlib import Path

import httpx

from .._env import find_base_url
from .._constants import DEFAULT_BASE_URL, AUTH_CLI_LOGIN_PATH, DIRTY_ROUTER_PREFIX, AUTH_CLI_CONFIG_PATH

CREDENTIALS_DIR = Path.home() / ".layerlens"
CREDENTIALS_FILE = CREDENTIALS_DIR / "credentials"

# How many seconds before expiry to trigger a proactive refresh
TOKEN_REFRESH_MARGIN = 300  # 5 minutes

# The API's session refresh, the one the web app uses: it holds the app client's secret and
# computes the SECRET_HASH Cognito requires from the user's sub (BUG-04, decision D-4).
AUTH_REFRESH_PATH = DIRTY_ROUTER_PREFIX + "/refresh"

_SESSION_ENDED = "Your `layerlens login` session has ended. Run `layerlens login` to sign in again."


class SessionError(Exception):
    """The stored login session could not be refreshed. The message says why, and what to do."""


# ---------------------------------------------------------------------------
# Credential storage (JSON, atomic writes)
# ---------------------------------------------------------------------------


def _ensure_dir() -> None:
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_DIR.chmod(0o700)


def load_credentials() -> Optional[Dict[str, Any]]:
    """Load stored credentials from ~/.layerlens/credentials."""
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        data = json.loads(CREDENTIALS_FILE.read_text())
        if not isinstance(data, dict):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def save_credentials(creds: Dict[str, Any]) -> None:
    """Persist credentials to ~/.layerlens/credentials with restrictive perms."""
    _ensure_dir()
    tmp = CREDENTIALS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(creds, indent=2))
    tmp.chmod(0o600)
    tmp.rename(CREDENTIALS_FILE)


def clear_credentials() -> None:
    """Remove stored credentials."""
    if CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.unlink()


# ---------------------------------------------------------------------------
# Auth config discovery
# ---------------------------------------------------------------------------

_cached_auth_config: Optional[Dict[str, str]] = None


def _get_base_url() -> str:
    found = find_base_url()
    return found.value if found is not None else DEFAULT_BASE_URL


def fetch_auth_config(base_url: Optional[str] = None) -> Dict[str, str]:
    """Fetch Cognito OAuth2 config from the backend discovery endpoint.

    Returns dict with keys: region, client_id, domain, scopes.
    Caches the result in memory for the process lifetime.
    """
    global _cached_auth_config

    if _cached_auth_config is not None:
        return _cached_auth_config

    # Also check if stored credentials already have the config cached
    creds = load_credentials()
    if creds and "auth_config" in creds:
        cached: Dict[str, str] = creds["auth_config"]
        _cached_auth_config = cached
        return cached

    url = (base_url or _get_base_url()).rstrip("/") + AUTH_CLI_CONFIG_PATH
    resp = httpx.get(url, timeout=15)
    resp.raise_for_status()
    fetched: Dict[str, str] = resp.json()

    _cached_auth_config = fetched
    return fetched


def _cognito_base_url(config: Dict[str, str]) -> str:
    return f"https://{config['domain']}.auth.{config['region']}.amazoncognito.com"


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def is_token_expired(creds: Dict[str, Any]) -> bool:
    """Check whether the access token is expired or about to expire."""
    expires_at: float = creds.get("expires_at", 0)
    return bool(time.time() >= (expires_at - TOKEN_REFRESH_MARGIN))


def _token_claims(token: Any) -> Dict[str, Any]:
    """The claims of a JWT the CLI holds, unverified: only to read its own ``exp`` and ``sub``."""
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (binascii.Error, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _session_expires_at(id_token: Any, expires_in: Any) -> float:
    """When the session must be refreshed: the id token's own ``exp``, since the CLI sends the id
    token (24 h), not the access token whose ``expires_in`` (1 h) the login reply gives."""
    exp = _token_claims(id_token).get("exp")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        return float(exp)
    return time.time() + (expires_in if isinstance(expires_in, (int, float)) else 3600)


def _api_sentence(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), str) and body["error"]:
        return f"{body['error']} (HTTP {resp.status_code})"
    return f"HTTP {resp.status_code}"


def refresh_access_token(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Refresh a ``layerlens login`` session through the API, store it and return it.

    Posts the refresh token and the user's ``sub`` to the API's ``POST /dgklmnr/refresh``,
    which holds the app client's secret (the CLI's own refresh with Cognito, without it, was
    refused every time). The new id token is stored with its own expiry. Raises
    :class:`SessionError`: the session has ended (the API answers 401, or nothing stored can
    refresh it), so ``layerlens login`` again; or the refresh could not be done now.
    """
    refresh_token = creds.get("refresh_token")
    stored_user = creds.get("user")
    sub = stored_user.get("sub") if isinstance(stored_user, dict) else None
    if not sub:
        sub = _token_claims(creds.get("id_token")).get("sub")
    if not refresh_token or not isinstance(sub, str) or not sub:
        raise SessionError(_SESSION_ENDED)

    url = (creds.get("base_url") or _get_base_url()).rstrip("/") + AUTH_REFRESH_PATH
    try:
        resp = httpx.post(url, json={"refresh_token": refresh_token, "sub": sub}, timeout=30)
    except httpx.HTTPError as exc:
        raise SessionError(f"Could not refresh your login session: {exc}. Try again.") from exc
    if resp.status_code == 401:
        raise SessionError(_SESSION_ENDED)
    if resp.status_code != 200:
        raise SessionError(f"Could not refresh your login session: {_api_sentence(resp)}")
    try:
        body = resp.json()
    except ValueError:
        body = None
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise SessionError("Could not refresh your login session: the API's answer held no token.")

    assert isinstance(body, dict)
    creds["id_token"] = token
    creds["expires_at"] = _session_expires_at(token, body.get("expires_in"))
    names = body.get("user")
    if isinstance(names, dict) and isinstance(creds.get("user"), dict):
        creds["user"].update({k: v for k, v in names.items() if isinstance(v, str)})
    save_credentials(creds)
    return creds


def get_valid_token() -> Optional[str]:
    """Return a valid ID token for Bearer auth, refreshing transparently if needed.

    The backend JWT middleware expects claims (email, cognito:groups) that are
    present in the Cognito **ID token**, not the access token.

    Only tokens from ``layerlens login`` are returned. An API key (from
    ``LAYERLENS_API_KEY``) is not a token: sent as Bearer to the login router it
    is rejected, so the CLI sends it as ``x-api-key`` instead (see ``_client.get_client``).
    ``None`` when there is no stored session; :class:`SessionError` when it could not be
    refreshed.
    """
    creds = load_credentials()
    if creds is None:
        return None

    if is_token_expired(creds):
        creds = refresh_access_token(creds)

    # Prefer id_token (has email + cognito:groups claims needed by backend JWT middleware)
    return creds.get("id_token") or creds.get("access_token")


# ---------------------------------------------------------------------------
# Email/password login via backend /auth/cli-login
# ---------------------------------------------------------------------------


class LoginError(Exception):
    """Raised when login fails."""


def cli_login(email: str, password: str, base_url: Optional[str] = None) -> Dict[str, Any]:
    """Authenticate with email/password via the backend CLI login endpoint.

    Returns the stored credentials dict.
    """
    url = (base_url or _get_base_url()).rstrip("/") + AUTH_CLI_LOGIN_PATH

    resp = httpx.post(
        url,
        json={"email": email, "password": password},
        timeout=30,
    )

    if resp.status_code == 401:
        raise LoginError("Invalid email or password.")
    if resp.status_code == 400:
        raise LoginError("Email and password are required.")
    resp.raise_for_status()

    body = resp.json()

    # Also fetch auth config for future token refresh
    try:
        auth_config = fetch_auth_config(base_url)
    except httpx.HTTPError:
        auth_config = None

    creds: Dict[str, Any] = {
        "access_token": body["access_token"],
        "id_token": body.get("id_token"),
        "refresh_token": body.get("refresh_token"),
        "expires_at": _session_expires_at(body.get("id_token"), body.get("expires_in", 3600)),
        "token_type": body.get("token_type", "Bearer"),
        "user": body.get("user"),
        "base_url": base_url or _get_base_url(),
    }
    if auth_config:
        creds["auth_config"] = auth_config

    save_credentials(creds)
    return creds


# ---------------------------------------------------------------------------
# User-info helper
# ---------------------------------------------------------------------------


def get_user_info(access_token: str) -> Optional[Dict[str, Any]]:
    """Return user info from stored credentials or Cognito userInfo endpoint."""
    creds = load_credentials()

    # First try stored user info from login response
    if creds and creds.get("user"):
        user_info: Dict[str, Any] = creds["user"]
        return user_info

    # Fall back to Cognito userInfo endpoint
    auth_config = (creds or {}).get("auth_config")
    if not auth_config:
        try:
            auth_config = fetch_auth_config()
        except httpx.HTTPError:
            return None

    userinfo_url = f"{_cognito_base_url(auth_config)}/oauth2/userInfo"
    try:
        resp = httpx.get(
            userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        resp.raise_for_status()
        result: Dict[str, Any] = resp.json()
        return result
    except httpx.HTTPError:
        return None
