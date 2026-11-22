import os
from unittest import mock

import pytest

from layerlens.instrument import _upload

# Enable the ``pytester`` fixture so the schema-lock enforcement (LAY-3613) can be
# guarded by an in-process inner pytest run (see test_event_schema.py).
pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _upload_sync_mode():
    """Force synchronous uploads in all tests so assertions don't race the worker thread."""
    _upload._sync_mode = True
    yield
    _upload._sync_mode = False


@pytest.fixture(autouse=True)
def _no_login_session_from_the_home_directory(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the developer's `layerlens login` session (~/.layerlens/credentials) out of the tests.

    A CLI command without an API key falls back to that session: a test would refresh it
    against a real API and send its token wherever the test points the CLI.
    """
    home = tmp_path_factory.mktemp("layerlens-home") / ".layerlens"
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_DIR", home)
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_FILE", home / "credentials")


@pytest.fixture(autouse=True)
def _no_ambient_trace_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the LAYERLENS_TRACE_TAGS of the shell or CI job running the tests off their traces."""
    monkeypatch.delenv("LAYERLENS_TRACE_TAGS", raising=False)


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "live: run against the real LayerLens API")
    config.addinivalue_line(
        "markers",
        "privacy_evidence: proves no PII/secrets/payment/delegation content leaves the "
        "SDK under capture_content=False (doubles as SOC2/GDPR evidence)",
    )
    config.addinivalue_line(
        "markers",
        "invariant: a structural/runtime contract guard (keys-must-match, layer "
        "suppression, no-content sweep, secret scrub, cost pricing). Run as a "
        "required CI gate via `-m invariant` so the build fails if an invariant breaks.",
    )


@pytest.fixture
def env_vars():
    """Clean environment variables for testing."""
    env_keys = [
        "LAYERLENS_API_KEY",
        "LAYERLENS_BASE_URL",
        "LAYERLENS_STRATIX_API_KEY",
        "LAYERLENS_STRATIX_BASE_URL",
        "LAYERLENS_ATLAS_API_KEY",
        "LAYERLENS_ATLAS_BASE_URL",
    ]
    original_values = {key: os.environ.get(key) for key in env_keys}

    # Clear environment variables
    for key in env_keys:
        if key in os.environ:
            del os.environ[key]

    yield

    # Restore original values
    for key, value in original_values.items():
        if value is not None:
            os.environ[key] = value
        elif key in os.environ:
            del os.environ[key]


@pytest.fixture
def mock_env_vars():
    """Mock environment variables with test values."""
    with mock.patch.dict(
        os.environ,
        {
            "LAYERLENS_API_KEY": "test-api-key",
        },
    ):
        yield


@pytest.fixture
def live_api_key(request: pytest.FixtureRequest) -> str:
    """A real LayerLens API key for an opt-in live test, or skip.

    Live tests talk to a real API (production, unless a base URL is set), so they
    need two explicit opt-ins: selecting them with ``-m live`` *and* a key in the
    environment, under the canonical or a legacy name. A key exported for everyday
    use (the CLI and the VS Code extension read ``LAYERLENS_API_KEY``) must never
    turn a plain ``pytest`` run into API traffic.
    """
    from layerlens._env import find_api_key

    if "live" not in (request.config.getoption("markexpr") or ""):
        pytest.skip("live API test: select it with -m live")
    found = find_api_key()
    if found is None:
        pytest.skip("LAYERLENS_API_KEY not set")
    return found.value
