"""The root options also work after the subcommand (LAY-4250).

``--api-key``, ``--host``, ``--port``, ``--format``, ``-v`` and ``-q`` belong to
the root group, so ``layerlens judge result abc123 --format json`` (the form every
``--help`` example used) failed with "No such option: --format". Every command
now accepts them after its name too. A value given there wins over one given
before the subcommand, as the later and more specific of the two.

Requests are captured at the httpx layer, so these tests assert what actually
leaves the process.
"""

# ruff: noqa: ARG002  # fixtures used for their monkeypatch side effects

from __future__ import annotations

import sys
import json
import shlex
from types import SimpleNamespace
from typing import Any, Set, List, Tuple, Optional
from pathlib import Path

import click
import httpx
import pytest
from click.testing import CliRunner

from layerlens._version import __version__
from layerlens.cli._app import cli

from .conftest import _make_runner

ORG = {"id": "org-1", "name": "Acme", "projects": [{"id": "proj-1", "name": "Default"}]}
TRACE = {
    "id": "trace-1",
    "organization_id": "org-1",
    "project_id": "proj-1",
    "created_at": "2026-10-14T00:00:00Z",
    "filename": "run.jsonl",
}

CommandPath = Tuple[str, ...]

# The banner's ASCII art always contains this run of characters.
BANNER_MARK = "|_____/_/"


def _stderr(result: Any) -> str:
    try:
        return result.stderr or ""
    except (ValueError, AttributeError):
        return ""


@pytest.fixture
def runner() -> CliRunner:
    return _make_runner()


@pytest.fixture
def no_stored_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds_dir = tmp_path / ".layerlens"
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_DIR", creds_dir)
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_FILE", creds_dir / "credentials")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fake LayerLens API (API-key router) that records every request."""
    state = SimpleNamespace(requests=[])

    def send(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        state.requests.append(request)
        path = request.url.path
        if path.endswith("/organizations"):
            return httpx.Response(200, json={"data": ORG}, request=request)
        if path.endswith("/traces"):
            return httpx.Response(
                200,
                json={"status": "success", "data": {"traces": [TRACE], "count": 1, "total_count": 1}},
                request=request,
            )
        return httpx.Response(404, json={"error": f"unexpected path {path}"}, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    return state


def _sent_keys(requests: List[httpx.Request]) -> Set[Optional[str]]:
    assert requests, "the CLI sent no request"
    return {r.headers.get("x-api-key") for r in requests}


def _api_roots(requests: List[httpx.Request]) -> Set[str]:
    """``scheme://host[:port]/api/v1`` of every request."""
    assert requests, "the CLI sent no request"
    for r in requests:
        assert r.url.path.startswith("/api/v1/"), r.url
    return {f"{r.url.scheme}://{r.url.netloc.decode()}/api/v1" for r in requests}


class TestFormat:
    def test_format_after_the_subcommand(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        result = runner.invoke(cli, ["trace", "list", "--format", "json"], env={"LAYERLENS_API_KEY": "k"})

        assert result.exit_code == 0, result.output + _stderr(result)
        assert [t["id"] for t in json.loads(result.stdout)] == ["trace-1"]

    def test_format_after_the_subcommand_wins_over_the_root_one(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        result = runner.invoke(
            cli, ["--format", "table", "trace", "list", "--format", "json"], env={"LAYERLENS_API_KEY": "k"}
        )

        assert result.exit_code == 0, result.output + _stderr(result)
        assert [t["id"] for t in json.loads(result.stdout)] == ["trace-1"]

    def test_root_format_still_applies_when_the_subcommand_does_not_repeat_it(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        result = runner.invoke(cli, ["--format", "json", "trace", "list"], env={"LAYERLENS_API_KEY": "k"})

        assert result.exit_code == 0, result.output + _stderr(result)
        assert [t["id"] for t in json.loads(result.stdout)] == ["trace-1"]


class TestApiKey:
    def test_api_key_after_the_subcommand_is_sent_as_an_api_key(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        result = runner.invoke(cli, ["trace", "list", "--api-key", "sub-key"])

        assert result.exit_code == 0, result.output + _stderr(result)
        assert _sent_keys(server.requests) == {"sub-key"}
        assert all("authorization" not in r.headers for r in server.requests)

    def test_api_key_after_the_subcommand_wins_over_the_root_one(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        result = runner.invoke(cli, ["--api-key", "root-key", "trace", "list", "--api-key", "sub-key"])

        assert result.exit_code == 0, result.output + _stderr(result)
        assert _sent_keys(server.requests) == {"sub-key"}

    def test_api_key_after_the_subcommand_wins_over_the_environment(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        result = runner.invoke(cli, ["trace", "list", "--api-key", "sub-key"], env={"LAYERLENS_API_KEY": "env-key"})

        assert result.exit_code == 0, result.output + _stderr(result)
        assert _sent_keys(server.requests) == {"sub-key"}

    def test_api_key_before_the_subcommand_wins_over_the_environment(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        # The copy of --api-key after the subcommand must not read the environment
        # variable: if it did, the environment would override the key given here.
        result = runner.invoke(cli, ["--api-key", "root-key", "trace", "list"], env={"LAYERLENS_API_KEY": "env-key"})

        assert result.exit_code == 0, result.output + _stderr(result)
        assert _sent_keys(server.requests) == {"root-key"}


class TestHostAndPort:
    def test_host_after_the_subcommand(self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace) -> None:
        result = runner.invoke(cli, ["trace", "list", "--host", "staging.example.com"], env={"LAYERLENS_API_KEY": "k"})

        assert result.exit_code == 0, result.output + _stderr(result)
        assert _api_roots(server.requests) == {"https://staging.example.com/api/v1"}

    def test_host_and_port_after_the_subcommand(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        result = runner.invoke(
            cli, ["trace", "list", "--host", "localhost", "--port", "8080"], env={"LAYERLENS_API_KEY": "k"}
        )

        assert result.exit_code == 0, result.output + _stderr(result)
        assert _api_roots(server.requests) == {"http://localhost:8080/api/v1"}

    def test_port_after_the_subcommand_combines_with_the_root_host(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        result = runner.invoke(
            cli, ["--host", "localhost", "trace", "list", "--port", "8080"], env={"LAYERLENS_API_KEY": "k"}
        )

        assert result.exit_code == 0, result.output + _stderr(result)
        assert _api_roots(server.requests) == {"http://localhost:8080/api/v1"}

    @pytest.mark.parametrize(
        "argv", [["--port", "8080", "trace", "list"], ["trace", "list", "--port", "8080"]], ids=["before", "after"]
    )
    def test_port_without_a_host_is_an_error(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace, argv: List[str]
    ) -> None:
        # It used to be ignored silently, and the request went to the default host.
        result = runner.invoke(cli, argv, env={"LAYERLENS_API_KEY": "k"})

        assert result.exit_code == 2
        assert "--port needs --host" in result.output + _stderr(result)
        assert server.requests == []

    def test_port_before_host_after_the_subcommand(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        # The check runs once both are parsed, so their order does not matter.
        result = runner.invoke(
            cli, ["trace", "list", "--port", "8080", "--host", "localhost"], env={"LAYERLENS_API_KEY": "k"}
        )

        assert result.exit_code == 0, result.output + _stderr(result)
        assert _api_roots(server.requests) == {"http://localhost:8080/api/v1"}

    def test_host_after_the_subcommand_wins_over_the_root_one(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        result = runner.invoke(
            cli,
            ["--host", "prod.example.com", "trace", "list", "--host", "staging.example.com"],
            env={"LAYERLENS_API_KEY": "k"},
        )

        assert result.exit_code == 0, result.output + _stderr(result)
        assert _api_roots(server.requests) == {"https://staging.example.com/api/v1"}


class TestVerboseAndQuiet:
    def test_verbose_after_the_subcommand(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        result = runner.invoke(cli, ["trace", "list", "-v"], env={"LAYERLENS_API_KEY": "k"})

        assert result.exit_code == 0, result.output + _stderr(result)
        assert "Showing 1 of 1 traces" in _stderr(result)

    def test_without_verbose_there_is_no_count_line(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
    ) -> None:
        result = runner.invoke(cli, ["trace", "list"], env={"LAYERLENS_API_KEY": "k"})

        assert result.exit_code == 0, result.output + _stderr(result)
        assert "Showing" not in _stderr(result)

    @pytest.fixture
    def terminal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pretend stderr is a terminal; the banner is only shown on one."""
        monkeypatch.setattr("layerlens.cli._command._stderr_is_terminal", lambda: True)

    def test_banner_is_shown_on_a_terminal(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace, terminal: None
    ) -> None:
        result = runner.invoke(cli, ["trace", "list"], env={"LAYERLENS_API_KEY": "k"})

        assert result.exit_code == 0, result.output + _stderr(result)
        assert BANNER_MARK in _stderr(result)
        assert f"v{__version__}" in _stderr(result)
        assert BANNER_MARK not in result.stdout

    @pytest.mark.parametrize(
        "argv",
        [["-q", "trace", "list"], ["trace", "list", "-q"], ["trace", "list", "--quiet"]],
        ids=["before", "after", "after-long"],
    )
    def test_quiet_suppresses_the_banner(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace, terminal: None, argv: List[str]
    ) -> None:
        result = runner.invoke(cli, argv, env={"LAYERLENS_API_KEY": "k"})

        assert result.exit_code == 0, result.output + _stderr(result)
        assert BANNER_MARK not in _stderr(result)

    def test_banner_is_shown_once(
        self, runner: CliRunner, no_stored_login: None, server: SimpleNamespace, terminal: None
    ) -> None:
        result = runner.invoke(cli, ["trace", "list"], env={"LAYERLENS_API_KEY": "k"})

        assert _stderr(result).count(BANNER_MARK) == 1


class TestEntryPointVerboseFlag:
    """``layerlens -v <command>`` is verbose mode, not ``--version``."""

    def _run_main(self, argv: List[str], capsys: pytest.CaptureFixture[str]) -> Tuple[Any, str, str]:
        from layerlens.cli import main

        capsys.readouterr()
        old_argv = sys.argv
        sys.argv = ["layerlens", *argv]
        try:
            with pytest.raises(SystemExit) as exc:
                main()
        finally:
            sys.argv = old_argv
        out, err = capsys.readouterr()
        return exc.value.code, out, err

    def test_leading_verbose_flag_runs_the_command(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_stored_login: None,
        server: SimpleNamespace,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("LAYERLENS_API_KEY", "k")

        code, out, err = self._run_main(["-v", "trace", "list"], capsys)

        assert code == 0, err
        assert f"layerlens {__version__}" not in out
        assert "trace-1" in out
        assert "Showing 1 of 1 traces" in err

    @pytest.mark.parametrize("flag", ["-v", "--version"])
    def test_version_flag_alone_prints_the_version(self, flag: str, capsys: pytest.CaptureFixture[str]) -> None:
        code, out, _ = self._run_main([flag], capsys)

        assert code == 0
        assert out.strip() == f"layerlens {__version__}"


def _all_command_paths(group: click.Group, prefix: CommandPath = ()) -> List[CommandPath]:
    paths: List[CommandPath] = []
    for name, command in group.commands.items():
        path = (*prefix, name)
        paths.append(path)
        if isinstance(command, click.Group):
            paths.extend(_all_command_paths(command, path))
    return paths


def _command_at(path: CommandPath) -> click.Command:
    command: click.Command = cli
    for name in path:
        assert isinstance(command, click.Group)
        command = command.commands[name]
    return command


LEAF_PATHS = [p for p in _all_command_paths(cli) if not isinstance(_command_at(p), click.Group)]

GLOBAL_OPTION_ARGS = [
    ["--api-key", "k"],
    ["--host", "example.com"],
    ["--port", "8080"],
    ["--format", "json"],
    ["--format", "table"],
    ["-v"],
    ["--verbose"],
    ["-q"],
    ["--quiet"],
]


@pytest.mark.parametrize("path", LEAF_PATHS, ids=lambda p: " ".join(p))
@pytest.mark.parametrize("option", GLOBAL_OPTION_ARGS, ids=lambda o: " ".join(o))
def test_every_command_accepts_every_root_option_after_its_name(
    runner: CliRunner, path: CommandPath, option: List[str]
) -> None:
    # click rejects an unknown option while parsing, before it acts on the eager
    # --help; so exit 0 here means the option was accepted, without running anything.
    result = runner.invoke(cli, [*path, *option, "--help"])

    assert result.exit_code == 0, result.output + _stderr(result)


def _help_examples() -> List[str]:
    examples = []
    for path in [(), *_all_command_paths(cli)]:
        help_text = _command_at(path).help or ""
        if "Examples:" not in help_text:
            continue
        for line in help_text.split("Examples:", 1)[1].splitlines():
            line = line.strip()
            if line.startswith("layerlens "):
                examples.append(line)
    return examples


HELP_EXAMPLES = _help_examples()


def test_the_help_examples_were_found() -> None:
    assert len(HELP_EXAMPLES) > 50, HELP_EXAMPLES


@pytest.mark.parametrize("example", HELP_EXAMPLES)
def test_every_help_example_parses(runner: CliRunner, example: str) -> None:
    # Appending --help parses the example exactly as typed and stops before running
    # it (see above). A shell redirection (`>> $GITHUB_STEP_SUMMARY`) parses as
    # extra arguments, which click only rejects after --help has exited.
    argv = shlex.split(example)[1:]

    result = runner.invoke(cli, [*argv, "--help"])

    assert result.exit_code == 0, f"`{example}` does not parse:\n{result.output}{_stderr(result)}"


@pytest.mark.parametrize("decls", [["--format"], ["--output", "-q"], ["--port"]])
def test_a_command_cannot_redeclare_a_root_option(decls: List[str]) -> None:
    from layerlens.cli._command import LayerLensCommand

    with pytest.raises(TypeError, match="root option"):
        LayerLensCommand("clashing", params=[click.Option(decls)])


def test_a_command_still_runs_through_ctx_invoke(
    no_stored_login: None, server: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    # Code that embeds the CLI can run a command with ctx.invoke, which calls its
    # callback directly; the root options it accepts must not reach that callback.
    obj = {"api_key": "k", "api_key_source": "--api-key", "output_format": "json", "verbose": False, "quiet": True}
    obj.update({"host": None, "port": None, "base_url": None})

    with click.Context(cli, obj=obj) as ctx:
        ctx.invoke(_command_at(("trace", "list")))

    assert [t["id"] for t in json.loads(capsys.readouterr().out)] == ["trace-1"]


@pytest.mark.parametrize(
    "args",
    [
        ["--api-key", "flag-key", "--host", "localhost", "--port", "8080", "trace", "get"],
        ["trace", "get", "--api-key", "flag-key", "--host", "localhost", "--port", "8080"],
    ],
    ids=["before", "after"],
)
def test_completion_uses_the_options_on_the_command_line(
    no_stored_login: None, server: SimpleNamespace, args: List[str]
) -> None:
    # Completing the trace ID calls the API with the key and host typed so far.
    # (click's completion machinery is called directly: click 8.0 ends a completion
    # run through main() with os._exit, which would end the test process.)
    from click.shell_completion import ShellComplete

    completions = ShellComplete(cli, {}, "layerlens", "_LAYERLENS_COMPLETE").get_completions(args, "")

    assert [c.value for c in completions] == ["trace-1"]
    assert _sent_keys(server.requests) == {"flag-key"}
    assert _api_roots(server.requests) == {"http://localhost:8080/api/v1"}
