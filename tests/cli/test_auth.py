"""Tests for CLI authentication: credential storage, token refresh, login flow."""

# ruff: noqa: ARG002  # creds_dir fixture is used for its monkeypatch side effect

from __future__ import annotations

import json
import time
import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from layerlens.cli._app import cli
from layerlens.cli._auth import (
    is_token_expired,
    load_credentials,
    save_credentials,
    clear_credentials,
)

from .conftest import _make_runner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return _make_runner()


def _combined(result) -> str:
    """Return stdout + (separately captured) stderr.

    With ``mix_stderr=True`` (Click's modern default) ``result.stderr`` is a
    property that raises ``ValueError`` — ``getattr(result, "stderr", "")``
    only catches ``AttributeError``, so we guard explicitly.
    """
    try:
        stderr = result.stderr or ""
    except (ValueError, AttributeError):
        stderr = ""
    return (result.output or "") + stderr


@pytest.fixture
def creds_dir(tmp_path: Path, monkeypatch):
    """Redirect credential storage to a temp directory."""
    creds_dir = tmp_path / ".layerlens"
    creds_file = creds_dir / "credentials"
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_DIR", creds_dir)
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_FILE", creds_file)
    return creds_dir


@pytest.fixture(autouse=True)
def clear_config_cache(monkeypatch):
    """Reset the in-memory auth config cache between tests."""
    monkeypatch.setattr("layerlens.cli._auth._cached_auth_config", None)


SAMPLE_AUTH_CONFIG = {
    "region": "us-east-1",
    "client_id": "test-client-id",
    "domain": "atlas-test",
    "scopes": "openid profile email",
}


@pytest.fixture
def sample_creds():
    return {
        "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.test",
        "id_token": "id-tok",
        "refresh_token": "refresh-tok",
        "expires_at": time.time() + 3600,
        "token_type": "Bearer",
        "auth_config": SAMPLE_AUTH_CONFIG,
        "user": {"email": "test@example.com", "given_name": "Test"},
    }


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------


class TestCredentialStorage:
    def test_save_and_load(self, creds_dir, sample_creds):
        save_credentials(sample_creds)
        loaded = load_credentials()
        assert loaded is not None
        assert loaded["access_token"] == sample_creds["access_token"]

    def test_load_returns_none_when_missing(self, creds_dir):
        assert load_credentials() is None

    def test_clear_credentials(self, creds_dir, sample_creds):
        save_credentials(sample_creds)
        assert load_credentials() is not None
        clear_credentials()
        assert load_credentials() is None

    def test_clear_when_nothing_stored(self, creds_dir):
        clear_credentials()

    def test_file_permissions(self, creds_dir, sample_creds):
        save_credentials(sample_creds)
        creds_file = creds_dir / "credentials"
        mode = creds_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_dir_permissions(self, creds_dir, sample_creds):
        save_credentials(sample_creds)
        mode = creds_dir.stat().st_mode & 0o777
        assert mode == 0o700

    def test_load_corrupted_file(self, creds_dir):
        creds_dir.mkdir(parents=True, exist_ok=True)
        (creds_dir / "credentials").write_text("not json{{{")
        assert load_credentials() is None


# ---------------------------------------------------------------------------
# Auth config discovery
# ---------------------------------------------------------------------------


class TestAuthConfigDiscovery:
    def test_fetch_from_api(self, creds_dir):
        from layerlens.cli._auth import fetch_auth_config

        mock_resp = MagicMock()
        mock_resp.json.return_value = SAMPLE_AUTH_CONFIG
        mock_resp.raise_for_status = MagicMock()

        with patch("layerlens.cli._auth.httpx.get", return_value=mock_resp) as mock_get:
            result = fetch_auth_config("https://api.example.com/api/v1")

        assert result["client_id"] == "test-client-id"
        assert result["domain"] == "atlas-test"
        mock_get.assert_called_once()

    def test_uses_cached_from_credentials(self, creds_dir, sample_creds):
        from layerlens.cli._auth import fetch_auth_config

        save_credentials(sample_creds)

        with patch("layerlens.cli._auth.httpx.get") as mock_get:
            result = fetch_auth_config()

        mock_get.assert_not_called()
        assert result["client_id"] == "test-client-id"

    def test_caches_in_memory(self, creds_dir):
        from layerlens.cli._auth import fetch_auth_config

        mock_resp = MagicMock()
        mock_resp.json.return_value = SAMPLE_AUTH_CONFIG
        mock_resp.raise_for_status = MagicMock()

        with patch("layerlens.cli._auth.httpx.get", return_value=mock_resp) as mock_get:
            fetch_auth_config("https://api.example.com/api/v1")
            fetch_auth_config("https://api.example.com/api/v1")

        assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# Token expiry
# ---------------------------------------------------------------------------


class TestTokenExpiry:
    def test_not_expired(self):
        creds = {"expires_at": time.time() + 3600}
        assert not is_token_expired(creds)

    def test_expired(self):
        creds = {"expires_at": time.time() - 10}
        assert is_token_expired(creds)

    def test_within_margin(self):
        creds = {"expires_at": time.time() + 240}
        assert is_token_expired(creds)

    def test_missing_expires_at(self):
        assert is_token_expired({})


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


def _jwt(**claims: object) -> str:
    """An unsigned JWT holding ``claims``: the CLI only reads its own token's claims."""

    def part(obj: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256', 'typ': 'JWT'})}.{part(claims)}.c2ln"


API = "https://api.test.com/api/v1"
SUB = "0e7c2b1a-1111-4222-8333-944455556666"


class _RefreshRoute:
    """A stub of the API's POST /dgklmnr/refresh: records each call, answers as told."""

    def __init__(self, status: int = 200, body: object = None, error: Exception | None = None) -> None:
        self.status, self.body, self.error = status, body, error
        self.calls: list = []

    def __call__(self, url: str, **kw: object) -> httpx.Response:
        self.calls.append((url, kw.get("json"), kw.get("data")))
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, json=self.body, request=httpx.Request("POST", url))


@pytest.fixture
def expired_session(creds_dir, sample_creds):
    """A `layerlens login` session whose id token has expired, as stored a day after login."""
    creds = {
        **sample_creds,
        "id_token": _jwt(sub=SUB, exp=int(time.time()) - 60, token_use="id"),
        "expires_at": time.time() - 60,
        "base_url": API,
        "user": {"email": "test@example.com", "given_name": "Test", "sub": SUB},
    }
    save_credentials(creds)
    return creds


class TestTokenRefresh:
    """BUG-04 (decision D-4): the CLI refreshed straight with Cognito, with the client id alone,
    and Cognito answered invalid_client_secret every time: a session never outlived its first
    hour. It now refreshes through the API's POST /dgklmnr/refresh, which holds the secret."""

    def test_an_expired_session_is_refreshed_through_the_api(self, expired_session):
        from layerlens.cli._auth import refresh_access_token

        exp = int(time.time()) + 86400
        new_id_token = _jwt(sub=SUB, exp=exp, token_use="id")
        route = _RefreshRoute(200, {"status": "success", "token": new_id_token, "expires_in": 3600})
        with patch("layerlens.cli._auth.httpx.post", side_effect=route):
            refreshed = refresh_access_token(dict(expired_session))

        assert route.calls == [(f"{API}/dgklmnr/refresh", {"refresh_token": "refresh-tok", "sub": SUB}, None)]
        assert refreshed["id_token"] == new_id_token
        # The token's own expiry, not the access token's hour.
        assert refreshed["expires_at"] == exp
        stored = load_credentials()
        assert stored["id_token"] == new_id_token and stored["expires_at"] == exp
        assert stored["refresh_token"] == "refresh-tok"

    def test_the_sub_comes_from_the_id_token_when_the_stored_user_lacks_it(self, expired_session):
        from layerlens.cli._auth import refresh_access_token

        creds = {**expired_session, "user": {"email": "test@example.com"}}
        route = _RefreshRoute(200, {"status": "success", "token": _jwt(sub=SUB, exp=int(time.time()) + 600)})
        with patch("layerlens.cli._auth.httpx.post", side_effect=route):
            refresh_access_token(creds)
        assert route.calls[0][1] == {"refresh_token": "refresh-tok", "sub": SUB}

    def test_a_401_says_the_session_ended_and_to_log_in(self, expired_session):
        from layerlens.cli._auth import SessionError, refresh_access_token

        route = _RefreshRoute(401, {"status": "error", "error": "Your session has expired. Please sign in again."})
        with patch("layerlens.cli._auth.httpx.post", side_effect=route):
            with pytest.raises(SessionError, match=r"session has ended.*layerlens login"):
                refresh_access_token(dict(expired_session))
        assert load_credentials() == expired_session

    def test_a_refresh_the_api_could_not_do_says_why_and_to_try_again(self, expired_session):
        from layerlens.cli._auth import SessionError, refresh_access_token

        route = _RefreshRoute(503, {"status": "error", "error": "Could not refresh your session. Please try again."})
        with patch("layerlens.cli._auth.httpx.post", side_effect=route):
            with pytest.raises(SessionError, match=r"Could not refresh.*Please try again\. \(HTTP 503\)") as err:
                refresh_access_token(dict(expired_session))
        assert "layerlens login" not in str(err.value)
        assert load_credentials() == expired_session

    def test_an_unreachable_api_says_so(self, expired_session):
        from layerlens.cli._auth import SessionError, refresh_access_token

        route = _RefreshRoute(error=httpx.ConnectError("connection refused"))
        with patch("layerlens.cli._auth.httpx.post", side_effect=route):
            with pytest.raises(SessionError, match=r"Could not refresh.*connection refused"):
                refresh_access_token(dict(expired_session))

    def test_a_session_without_a_refresh_token_has_ended(self, creds_dir):
        from layerlens.cli._auth import SessionError, refresh_access_token

        with pytest.raises(SessionError, match=r"layerlens login"):
            refresh_access_token({"access_token": "tok", "user": {"sub": SUB}})


# ---------------------------------------------------------------------------
# get_valid_token
# ---------------------------------------------------------------------------


class TestGetValidToken:
    def test_never_returns_the_api_key_env_var(self, creds_dir, monkeypatch):
        """An API key is not a login token. Returned here it went out as Bearer to the
        login router, which rejects it (LAY-4220); the CLI sends it as x-api-key instead."""
        from layerlens.cli._auth import get_valid_token

        monkeypatch.setenv("LAYERLENS_API_KEY", "env-key-123")
        assert get_valid_token() is None

    def test_returns_stored_token(self, creds_dir, sample_creds, monkeypatch):
        from layerlens.cli._auth import get_valid_token

        monkeypatch.delenv("LAYERLENS_API_KEY", raising=False)
        save_credentials(sample_creds)
        # get_valid_token prefers id_token (has email + cognito:groups for backend JWT middleware)
        assert get_valid_token() == sample_creds["id_token"]

    def test_returns_none_when_no_creds(self, creds_dir, monkeypatch):
        from layerlens.cli._auth import get_valid_token

        monkeypatch.delenv("LAYERLENS_API_KEY", raising=False)
        assert get_valid_token() is None

    def test_refreshes_expired_token(self, expired_session, monkeypatch):
        from layerlens.cli._auth import get_valid_token

        monkeypatch.delenv("LAYERLENS_API_KEY", raising=False)
        new_id_token = _jwt(sub=SUB, exp=int(time.time()) + 86400)
        route = _RefreshRoute(200, {"status": "success", "token": new_id_token, "expires_in": 3600})
        with patch("layerlens.cli._auth.httpx.post", side_effect=route):
            assert get_valid_token() == new_id_token
            assert get_valid_token() == new_id_token  # valid now: no second refresh
        assert len(route.calls) == 1


class TestCommandsWithAnExpiredSession:
    """What a user sees, through the CLI."""

    def test_whoami_refreshes_and_answers(self, runner, expired_session, monkeypatch):
        monkeypatch.delenv("LAYERLENS_API_KEY", raising=False)
        route = _RefreshRoute(200, {"status": "success", "token": _jwt(sub=SUB, exp=int(time.time()) + 86400)})
        with patch("layerlens.cli._auth.httpx.post", side_effect=route):
            result = runner.invoke(cli, ["whoami"])
        assert result.exit_code == 0, _combined(result)
        assert "test@example.com" in _combined(result)

    def test_whoami_says_the_session_ended(self, runner, expired_session, monkeypatch):
        monkeypatch.delenv("LAYERLENS_API_KEY", raising=False)
        route = _RefreshRoute(401, {"status": "error", "error": "Your session has expired. Please sign in again."})
        with patch("layerlens.cli._auth.httpx.post", side_effect=route):
            result = runner.invoke(cli, ["whoami"])
        assert result.exit_code == 1
        assert "session has ended" in _combined(result) and "layerlens login" in _combined(result)

    def test_a_command_says_the_session_ended_not_that_there_are_no_credentials(
        self, runner, expired_session, monkeypatch
    ):
        monkeypatch.delenv("LAYERLENS_API_KEY", raising=False)
        route = _RefreshRoute(401, {"status": "error", "error": "Your session has expired. Please sign in again."})
        with patch("layerlens.cli._auth.httpx.post", side_effect=route):
            result = runner.invoke(cli, ["trace", "list"])
        assert result.exit_code == 1
        out = _combined(result)
        assert "session has ended" in out and "no credentials found" not in out


# ---------------------------------------------------------------------------
# cli_login
# ---------------------------------------------------------------------------


class TestCLILogin:
    def test_a_session_lasts_as_long_as_the_id_token_it_sends(self, creds_dir):
        # The CLI authenticates with the id token (24 h), not the access token (1 h): the
        # session was marked expired after an hour and refreshed for nothing (BUG-04).
        from layerlens.cli._auth import cli_login

        exp = int(time.time()) + 86400
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "access_token": "access-tok",
            "id_token": _jwt(sub=SUB, exp=exp, token_use="id"),
            "refresh_token": "refresh-tok",
            "expires_in": 3600,
            "user": {"email": "user@example.com", "sub": SUB},
        }
        login_resp.raise_for_status = MagicMock()
        config_resp = MagicMock()
        config_resp.json.return_value = SAMPLE_AUTH_CONFIG
        config_resp.raise_for_status = MagicMock()
        with patch("layerlens.cli._auth.httpx.post", return_value=login_resp):
            with patch("layerlens.cli._auth.httpx.get", return_value=config_resp):
                result = cli_login("user@example.com", "pass123", base_url=API)
        assert result["expires_at"] == exp
        assert load_credentials()["expires_at"] == exp

    def test_login_success(self, creds_dir):
        from layerlens.cli._auth import cli_login

        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {
            "access_token": "access-tok",
            "id_token": "id-tok",
            "refresh_token": "refresh-tok",
            "expires_in": 3600,
            "token_type": "Bearer",
            "user": {"email": "user@example.com", "given_name": "Test"},
        }
        login_resp.raise_for_status = MagicMock()

        config_resp = MagicMock()
        config_resp.json.return_value = SAMPLE_AUTH_CONFIG
        config_resp.raise_for_status = MagicMock()

        with patch("layerlens.cli._auth.httpx.post", return_value=login_resp):
            with patch("layerlens.cli._auth.httpx.get", return_value=config_resp):
                result = cli_login(
                    "user@example.com",
                    "pass123",
                    base_url="https://api.test.com/api/v1",
                )

        assert result["access_token"] == "access-tok"
        assert result["user"]["email"] == "user@example.com"

        loaded = load_credentials()
        assert loaded["access_token"] == "access-tok"

    def test_login_invalid_credentials(self, creds_dir):
        from layerlens.cli._auth import LoginError, cli_login

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("layerlens.cli._auth.httpx.post", return_value=mock_resp):
            with pytest.raises(LoginError, match="Invalid email or password"):
                cli_login("bad@example.com", "wrong")

    def test_login_missing_fields(self, creds_dir):
        from layerlens.cli._auth import LoginError, cli_login

        mock_resp = MagicMock()
        mock_resp.status_code = 400

        with patch("layerlens.cli._auth.httpx.post", return_value=mock_resp):
            with pytest.raises(LoginError, match="required"):
                cli_login("", "")


# ---------------------------------------------------------------------------
# CLI command tests
# ---------------------------------------------------------------------------


class TestLoginCommand:
    def test_login_success(self, runner, creds_dir):
        mock_creds = {
            "access_token": "tok",
            "expires_at": time.time() + 3600,
            "user": {"email": "user@test.com", "given_name": "Test"},
        }

        with patch("layerlens.cli._auth.cli_login", return_value=mock_creds):
            with patch("layerlens.cli._auth.load_credentials", return_value=None):
                result = runner.invoke(cli, ["login"], input="user@test.com\nsecret\n")

        assert result.exit_code == 0
        combined = _combined(result)
        assert "logged in" in combined.lower()

    def test_login_already_logged_in_decline(self, runner, creds_dir):
        existing = {"access_token": "old-tok"}

        with patch("layerlens.cli._auth.load_credentials", return_value=existing):
            result = runner.invoke(cli, ["login"], input="n\n")

        assert result.exit_code == 0

    def test_login_error(self, runner, creds_dir):
        from layerlens.cli._auth import LoginError

        with patch("layerlens.cli._auth.load_credentials", return_value=None):
            with patch(
                "layerlens.cli._auth.cli_login",
                side_effect=LoginError("Invalid email or password."),
            ):
                result = runner.invoke(cli, ["login"], input="bad@test.com\nwrong\n")

        assert result.exit_code != 0
        combined = _combined(result)
        assert "invalid" in combined.lower()


class TestLogoutCommand:
    def test_logout_success(self, runner, creds_dir, sample_creds):
        save_credentials(sample_creds)
        result = runner.invoke(cli, ["logout"])
        assert result.exit_code == 0
        combined = _combined(result)
        assert "logged out" in combined.lower()
        assert load_credentials() is None

    def test_logout_not_logged_in(self, runner, creds_dir):
        result = runner.invoke(cli, ["logout"])
        assert result.exit_code == 0
        combined = _combined(result)
        assert "not currently" in combined.lower()


class TestWhoamiCommand:
    # API-key whoami (verified against the API) is covered in test_rename_cli.py::TestWhoami.

    def test_whoami_not_logged_in(self, runner, creds_dir, monkeypatch):
        monkeypatch.delenv("LAYERLENS_API_KEY", raising=False)
        result = runner.invoke(cli, ["whoami"])
        assert result.exit_code != 0
        combined = _combined(result)
        assert "not logged in" in combined.lower()

    def test_whoami_shows_user_info(self, runner, creds_dir, sample_creds, monkeypatch):
        monkeypatch.delenv("LAYERLENS_API_KEY", raising=False)
        save_credentials(sample_creds)

        with patch("layerlens.cli._auth.get_valid_token", return_value="tok"):
            with patch(
                "layerlens.cli._auth.get_user_info",
                return_value={
                    "email": "user@example.com",
                    "name": "Test User",
                    "sub": "abc-123",
                },
            ):
                result = runner.invoke(cli, ["whoami"])

        assert result.exit_code == 0
        combined = _combined(result)
        assert "user@example.com" in combined
        assert "Test User" in combined
