"""CLI side of the Stratix -> LayerLens rename (LAY-4220).

Covers which credential the CLI sends and *how*: an API key must go out as
``x-api-key`` on the normal ``/api/v1`` router, because the ``/dgklmnr`` router
only verifies Cognito login tokens. Every request is captured at the httpx
layer, so these tests assert what actually leaves the process.
"""

# ruff: noqa: ARG002  # fixtures used for their monkeypatch side effects

from __future__ import annotations

import os
import re
import sys
import json
import time
import warnings
import threading
import subprocess
from types import SimpleNamespace
from typing import Any, List
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import click
import httpx
import pytest

from layerlens.cli._app import cli
from layerlens.cli._auth import save_credentials
from layerlens.cli._banner import banner

from .conftest import _make_runner

REPO_ROOT = Path(__file__).resolve().parents[2]

ORG = {"id": "org-1", "name": "Acme", "projects": [{"id": "proj-1", "name": "Default"}]}
TRACE = {
    "id": "trace-1",
    "organization_id": "org-1",
    "project_id": "proj-1",
    "created_at": "2026-10-12T00:00:00Z",
    "filename": "run.jsonl",
}

ALL_ENV_VARS = (
    "LAYERLENS_API_KEY",
    "LAYERLENS_BASE_URL",
    "LAYERLENS_STRATIX_API_KEY",
    "LAYERLENS_STRATIX_BASE_URL",
    "LAYERLENS_ATLAS_API_KEY",
    "LAYERLENS_ATLAS_BASE_URL",
)


def _stderr(result: Any) -> str:
    try:
        return result.stderr or ""
    except (ValueError, AttributeError):
        return ""


@pytest.fixture
def runner():
    return _make_runner()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """stderr is a terminal: a person reads it, so the CLI's notices are printed."""
    monkeypatch.setattr("layerlens.cli._notice.stderr_is_terminal", lambda: True)


@pytest.fixture
def creds_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    creds_dir = tmp_path / ".layerlens"
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_DIR", creds_dir)
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_FILE", creds_dir / "credentials")
    return creds_dir


@pytest.fixture
def stored_login(creds_dir: Path) -> None:
    save_credentials(
        {
            "access_token": "access-tok",
            "id_token": "id-tok",
            "refresh_token": "refresh-tok",
            "expires_at": time.time() + 3600,
            "token_type": "Bearer",
            "user": {"email": "someone@example.com"},
        }
    )


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fake LayerLens API that records every request the CLI sends."""
    state = SimpleNamespace(requests=[], status=200, with_trace=False)

    def send(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        state.requests.append(request)
        if state.status != 200:
            return httpx.Response(state.status, json={"error": "Invalid x-api-key header"}, request=request)
        path = request.url.path
        if path.endswith("/organizations"):
            # The login router lists organizations; the API-key router returns the key's own.
            body = {"data": [ORG]} if "/dgklmnr/" in path else {"data": ORG}
            return httpx.Response(200, json=body, request=request)
        if path.endswith("/traces"):
            traces = [TRACE] if state.with_trace else []
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"traces": traces, "count": len(traces), "total_count": len(traces)},
                },
                request=request,
            )
        return httpx.Response(404, json={"error": f"unexpected path {path}"}, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    return state


def _assert_sent_as_api_key(requests: List[httpx.Request], key: str) -> None:
    assert requests, "the CLI sent no request"
    for request in requests:
        assert request.headers.get("x-api-key") == key, request.url
        assert "authorization" not in request.headers, request.url
        assert "/dgklmnr" not in request.url.path, request.url


class TestApiKeyCredentials:
    def test_layerlens_api_key_is_sent_as_an_api_key(self, runner, clean_env, creds_dir, server) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "key-123")

        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        assert "No traces found." in result.output
        _assert_sent_as_api_key(server.requests, "key-123")
        assert "deprecated" not in _stderr(result)

    def test_api_key_flag_is_sent_as_an_api_key(self, runner, clean_env, creds_dir, server) -> None:
        result = runner.invoke(cli, ["--api-key", "flag-key", "trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        _assert_sent_as_api_key(server.requests, "flag-key")

    @pytest.mark.parametrize("legacy", ["LAYERLENS_STRATIX_API_KEY", "LAYERLENS_ATLAS_API_KEY"])
    def test_legacy_variable_still_works_and_says_what_to_rename(
        self, runner, clean_env, creds_dir, server, on_a_terminal, legacy: str
    ) -> None:
        clean_env.setenv(legacy, "old-key")

        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        _assert_sent_as_api_key(server.requests, "old-key")
        assert f"warning: {legacy} is deprecated" in _stderr(result)
        assert "LAYERLENS_API_KEY" in _stderr(result)
        # The notice never pollutes stdout, which scripts parse. (``result.output``
        # also contains stderr from click 8.2 on, so check stdout explicitly.)
        assert "deprecated" not in result.stdout

    @pytest.mark.parametrize("legacy", ["LAYERLENS_STRATIX_API_KEY", "LAYERLENS_STRATIX_BASE_URL"])
    def test_legacy_variable_notice_is_not_printed_when_stderr_is_not_a_terminal(
        self, runner, clean_env, creds_dir, server, legacy: str
    ) -> None:
        # In CI stderr is a pipe: a step capturing 2>&1 must get what 1.11.0 printed (R4 F1).
        clean_env.setenv("LAYERLENS_STRATIX_API_KEY", "old-key")
        clean_env.setenv(legacy, "https://staging.example.com/api/v1" if "URL" in legacy else "old-key")

        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        _assert_sent_as_api_key(server.requests, "old-key")
        assert _stderr(result) == ""

    def test_api_key_wins_over_a_stored_login_token(self, runner, clean_env, stored_login, server) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "key-123")

        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        _assert_sent_as_api_key(server.requests, "key-123")

    def test_stored_login_token_is_sent_as_bearer_to_the_login_router(
        self, runner, clean_env, stored_login, server
    ) -> None:
        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        assert server.requests
        for request in server.requests:
            assert request.headers.get("authorization") == "Bearer id-tok"
            assert "x-api-key" not in request.headers
            assert "/dgklmnr/" in request.url.path

    def test_no_credentials_fails_with_every_way_to_fix_it(self, runner, clean_env, creds_dir, server) -> None:
        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 1
        assert "--api-key" in _stderr(result)
        assert "LAYERLENS_API_KEY" in _stderr(result)
        assert "layerlens login" in _stderr(result)
        assert server.requests == []

    def test_legacy_base_url_variable_still_works_and_says_what_to_rename(
        self, runner, clean_env, creds_dir, server, on_a_terminal
    ) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "key-123")
        clean_env.setenv("LAYERLENS_STRATIX_BASE_URL", "https://staging.example.com/api/v1")

        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        assert {r.url.host for r in server.requests} == {"staging.example.com"}
        assert "warning: LAYERLENS_STRATIX_BASE_URL is deprecated" in _stderr(result)


class TestCanonicalAndLegacyBothSet:
    """R1 F2: with both set to different values the new variable wins, and a person is told."""

    SENTENCE = (
        "warning: LAYERLENS_STRATIX_API_KEY is set but ignored: LAYERLENS_API_KEY is set and wins. Unset one of them."
    )

    def test_on_a_terminal_the_ignored_key_is_named(self, runner, clean_env, creds_dir, server, on_a_terminal) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "new-key")
        clean_env.setenv("LAYERLENS_STRATIX_API_KEY", "old-key")

        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        _assert_sent_as_api_key(server.requests, "new-key")
        assert self.SENTENCE in _stderr(result)
        assert "is set but ignored" not in result.stdout

    def test_on_a_terminal_the_ignored_base_url_is_named(
        self, runner, clean_env, creds_dir, server, on_a_terminal
    ) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "new-key")
        clean_env.setenv("LAYERLENS_BASE_URL", "https://new.example.com/api/v1")
        clean_env.setenv("LAYERLENS_ATLAS_BASE_URL", "https://old.example.com/api/v1")

        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        assert {r.url.host for r in server.requests} == {"new.example.com"}
        assert (
            "warning: LAYERLENS_ATLAS_BASE_URL is set but ignored: LAYERLENS_BASE_URL is set and wins. "
            "Unset one of them."
        ) in _stderr(result)

    def test_in_ci_nothing_extra_is_printed(self, runner, clean_env, creds_dir, server) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "new-key")
        clean_env.setenv("LAYERLENS_STRATIX_API_KEY", "old-key")
        clean_env.setenv("LAYERLENS_BASE_URL", "https://new.example.com/api/v1")
        clean_env.setenv("LAYERLENS_STRATIX_BASE_URL", "https://old.example.com/api/v1")

        with warnings.catch_warnings():
            warnings.simplefilter("always")
            result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        _assert_sent_as_api_key(server.requests, "new-key")
        assert _stderr(result) == ""

    def test_a_key_given_as_an_option_wins_and_no_variable_is_named(
        self, runner, clean_env, creds_dir, server, on_a_terminal
    ) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "new-key")
        clean_env.setenv("LAYERLENS_STRATIX_API_KEY", "old-key")

        result = runner.invoke(cli, ["trace", "list", "--api-key", "flag-key"])

        assert result.exit_code == 0, result.output + _stderr(result)
        _assert_sent_as_api_key(server.requests, "flag-key")
        assert "is set but ignored" not in _stderr(result)


class TestWhoami:
    def test_api_key_is_verified_against_the_api(self, runner, clean_env, creds_dir, server) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "key-123")

        result = runner.invoke(cli, ["whoami"])

        assert result.exit_code == 0, result.output + _stderr(result)
        _assert_sent_as_api_key(server.requests, "key-123")
        combined = result.output + _stderr(result)
        assert "LAYERLENS_API_KEY" in combined
        assert "org-1" in combined
        assert "proj-1" in combined

    def test_rejected_api_key_fails(self, runner, clean_env, creds_dir, server) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "revoked-key")
        server.status = 401

        result = runner.invoke(cli, ["whoami"])

        assert result.exit_code == 1
        assert "Error" in _stderr(result)
        assert "Authenticated" not in result.output + _stderr(result)

    def test_legacy_api_key_variable_is_recognised(self, runner, clean_env, creds_dir, server, on_a_terminal) -> None:
        clean_env.setenv("LAYERLENS_STRATIX_API_KEY", "old-key")

        result = runner.invoke(cli, ["whoami"])

        assert result.exit_code == 0, result.output + _stderr(result)
        assert "org-1" in result.output + _stderr(result)
        assert "warning: LAYERLENS_STRATIX_API_KEY is deprecated" in _stderr(result)


def _run_entry_point(
    entry: str, *args: str, extra_env: "dict[str, str] | None" = None, prog: str = "prog"
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ALL_ENV_VARS}
    env.update(extra_env or {})
    code = f"import sys; sys.argv = [{prog!r}, *{list(args)!r}]; from layerlens.cli import {entry}; {entry}()"
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)


class TestCommandName:
    def test_version_names_layerlens(self) -> None:
        from layerlens._version import __version__

        proc = _run_entry_point("main", "--version")

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == f"layerlens {__version__}"

    @pytest.mark.parametrize("args", [("--version",), ("--help",), ("trace", "--help")])
    def test_stratix_command_in_a_pipe_prints_exactly_what_layerlens_prints(self, args: tuple) -> None:
        # Captured output (CI, $(...)) is not a terminal: no notice, as in 1.11.0 (R4 F1).
        new = _run_entry_point("main", *args)
        old = _run_entry_point("legacy_main", *args)

        assert old.returncode == new.returncode == 0, old.stderr
        assert old.stdout == new.stdout
        assert old.stderr == new.stderr
        assert "deprecated" not in old.stderr

    @pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal")
    def test_stratix_command_on_a_terminal_prints_the_notice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A real terminal, not a patched check: stderr is a pseudo-terminal's end.
        import pty

        from layerlens.cli import legacy_main

        controller, terminal = pty.openpty()
        with os.fdopen(terminal, "w") as stderr:
            monkeypatch.setattr(sys, "stderr", stderr)
            monkeypatch.setattr(sys, "argv", ["stratix", "--version"])
            with pytest.raises(SystemExit):
                legacy_main()
            os.set_blocking(controller, False)
            written = os.read(controller, 4096)
        os.close(controller)

        assert b"warning: the 'stratix' command is deprecated; use 'layerlens' instead" in written

    def test_stratix_shell_completion_is_not_interrupted_by_the_notice(self) -> None:
        # The shell calls the program on every TAB; a notice there lands in the prompt.
        completion = {"_STRATIX_COMPLETE": "bash_complete", "COMP_WORDS": "stratix tr", "COMP_CWORD": "1"}

        # click reads the completion variable named after the program, so the
        # process has to be called `stratix` for _STRATIX_COMPLETE to apply.
        proc = _run_entry_point("legacy_main", extra_env=completion, prog="stratix")

        assert proc.returncode == 0, proc.stderr
        # A completion reply ("plain,trace"), not the help text that lists `trace` too.
        assert proc.stdout.splitlines() == ["plain,trace"], proc.stdout
        # Our notice must not appear. (Other packages may warn at import time, e.g.
        # requests' RequestsDependencyWarning in CI; that is not ours to silence.)
        assert "deprecated" not in proc.stderr, proc.stderr

    def test_console_scripts_point_at_the_right_entry_points(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        scripts = pyproject.split("[project.scripts]", 1)[1].split("\n[", 1)[0]

        assert re.search(r'^layerlens = "layerlens\.cli:main"$', scripts, re.M)
        assert re.search(r'^stratix = "layerlens\.cli:legacy_main"$', scripts, re.M)

    def test_banner_no_longer_spells_stratix(self) -> None:
        old_art_first_line = r"  ____ _____ ____      _  _____ _____  __"

        text = banner("9.9.9")

        assert old_art_first_line not in text
        assert "v9.9.9" in text


def _run_in_process(entry: Any, argv: List[str], capsys: pytest.CaptureFixture[str]) -> "tuple[Any, str, str]":
    capsys.readouterr()
    old_argv = sys.argv
    sys.argv = ["prog", *argv]
    try:
        with pytest.raises(SystemExit) as exc:
            entry()
    finally:
        sys.argv = old_argv
    out, err = capsys.readouterr()
    return exc.value.code, out, err


def test_stratix_in_ci_with_the_legacy_key_prints_byte_identical_output(
    clean_env: pytest.MonkeyPatch, creds_dir: Path, server: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CI case: `stratix --format json ...` with LAYERLENS_STRATIX_API_KEY exported.

    A job that parses stdout must get exactly what `layerlens` prints with the new variable.
    stderr is not a terminal in CI, so the deprecation notices are not printed at all: a step
    that captures `2>&1` parses the same JSON as on 1.11.0 (R4 F1).
    """
    from layerlens.cli import main, legacy_main

    server.with_trace = True
    argv = ["--format", "json", "trace", "list"]

    clean_env.setenv("LAYERLENS_API_KEY", "key-123")
    new_code, new_out, new_err = _run_in_process(main, argv, capsys)

    clean_env.delenv("LAYERLENS_API_KEY")
    clean_env.setenv("LAYERLENS_STRATIX_API_KEY", "key-123")
    old_code, old_out, old_err = _run_in_process(legacy_main, argv, capsys)

    assert new_code == old_code == 0, new_err + old_err
    assert '"trace-1"' in new_out
    assert old_out == new_out
    assert new_err == old_err == ""
    _assert_sent_as_api_key(server.requests, "key-123")


def test_stratix_on_a_terminal_with_the_legacy_key_names_both_renames_on_stderr_only(
    clean_env: pytest.MonkeyPatch,
    creds_dir: Path,
    server: SimpleNamespace,
    on_a_terminal: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from layerlens.cli import legacy_main

    server.with_trace = True
    clean_env.setenv("LAYERLENS_STRATIX_API_KEY", "key-123")

    code, out, err = _run_in_process(legacy_main, ["--format", "json", "trace", "list"], capsys)

    assert code == 0, err
    assert '"trace-1"' in out
    assert "deprecated" not in out
    assert "warning: the 'stratix' command is deprecated" in err
    assert "warning: LAYERLENS_STRATIX_API_KEY is deprecated" in err


class _IntegrationsAPI(BaseHTTPRequestHandler):
    """Answers the organization and the integration list, as the API does."""

    def do_GET(self) -> None:  # noqa: N802 - the http.server hook
        if self.path.split("?")[0].endswith("/organizations"):
            body: Any = {"status": "success", "data": ORG}
        else:
            body = {
                "status": "success",
                "data": [{"id": "int-1", "organization_id": "org-1", "name": "Slack", "type": "slack"}],
            }
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        pass


def test_sample_09_capture_with_the_legacy_variables_parses_as_json() -> None:
    """1.11.0's samples/cli/09: `OUTPUT=$(stratix --format json integration list 2>&1)`, then json.load.

    With LAYERLENS_STRATIX_API_KEY and LAYERLENS_STRATIX_BASE_URL set, 2.0.0 printed three notices
    first, so the JSON failed to parse and the script tested nothing, exiting 0 (R4 F1).
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _IntegrationsAPI)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        env = {k: v for k, v in os.environ.items() if k not in ALL_ENV_VARS}
        env["LAYERLENS_STRATIX_API_KEY"] = "key-123"
        env["LAYERLENS_STRATIX_BASE_URL"] = f"http://127.0.0.1:{httpd.server_address[1]}/api/v1"
        code = (
            "import sys; sys.argv = ['stratix', '--format', 'json', 'integration', 'list']; "
            "from layerlens.cli import legacy_main; legacy_main()"
        )
        proc = subprocess.run(
            [sys.executable, "-W", "ignore", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=60,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert proc.returncode == 0, proc.stdout
    integrations = json.loads(proc.stdout)
    assert [i["id"] for i in integrations] == ["int-1"]


def _all_command_paths(group: click.Group, prefix: tuple = ()) -> List[tuple]:
    paths: List[tuple] = []
    for name, command in group.commands.items():
        path = (*prefix, name)
        paths.append(path)
        if isinstance(command, click.Group):
            paths.extend(_all_command_paths(command, path))
    return paths


@pytest.mark.parametrize("path", [()] + _all_command_paths(cli), ids=lambda p: " ".join(p) or "root")
def test_help_text_teaches_layerlens(runner, path: tuple) -> None:
    result = runner.invoke(cli, [*path, "--help"])

    assert result.exit_code == 0, result.output
    assert "stratix" not in result.output.lower(), result.output


def test_ci_report_names_layerlens() -> None:
    from datetime import datetime, timezone

    from layerlens.cli.commands.ci import Report, _as_markdown

    report = _as_markdown(Report(datetime.now(timezone.utc), [], [], [], latest=True))

    assert "## LayerLens CI report" in report
    assert "*Generated by `layerlens ci report`" in report
    assert "stratix" not in report.lower()
