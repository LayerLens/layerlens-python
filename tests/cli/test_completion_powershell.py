"""``layerlens completion powershell`` prints a profile snippet that sets PowerShell completion up.

The snippet registered its own completer that ran ``layerlens`` with ``powershell_source`` and
offered each line it printed as a completion. That output is a whole ``Register-ArgumentCompleter``
script, so Tab offered lines of the script, not commands (docs E2E 2026-11-10, L5-41). The snippet
is now click's own: load that script into the session with ``Invoke-Expression``. click has had
PowerShell completion since 8.5 (Python 3.10+); with an older click the command says so and exits 1
instead of printing a snippet that cannot work.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner
from click.shell_completion import get_completion_class

from layerlens.cli._app import cli

SNIPPET = (
    "$env:_LAYERLENS_COMPLETE = 'powershell_source'\n"
    "layerlens | Out-String | Invoke-Expression\n"
    "Remove-Item Env:_LAYERLENS_COMPLETE"
)


def _with_powershell_completion(monkeypatch: pytest.MonkeyPatch, supported: bool) -> None:
    """Make the installed click have PowerShell completion, or not, whichever release it is."""

    def lookup(shell: str) -> Any:
        if shell != "powershell":
            return get_completion_class(shell)
        return (get_completion_class(shell) or object) if supported else None

    monkeypatch.setattr("click.shell_completion.get_completion_class", lookup)


def test_the_snippet_loads_clicks_completion_script(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_powershell_completion(monkeypatch, supported=True)
    result = CliRunner().invoke(cli, ["--quiet", "completion", "powershell"], env={"_": "/usr/local/bin/layerlens"})
    assert result.exit_code == 0, result.output
    assert result.output == f"Add this to your PowerShell profile (the file $PROFILE names):\n\n{SNIPPET}\n"


@pytest.mark.skipif(
    get_completion_class("powershell") is None, reason="this click has no PowerShell completion (it came in 8.5)"
)
def test_what_the_snippet_runs_prints_the_script_that_registers_the_completer() -> None:
    # The snippet's premise, on click itself: the script Invoke-Expression loads.
    script = CliRunner().invoke(cli, [], env={"_LAYERLENS_COMPLETE": "powershell_source"}, prog_name="layerlens")
    assert script.output.startswith("Register-ArgumentCompleter -Native -CommandName layerlens -ScriptBlock {")


def test_with_a_click_that_has_no_powershell_completion_it_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_powershell_completion(monkeypatch, supported=False)
    result = CliRunner().invoke(cli, ["--quiet", "completion", "powershell"])
    assert result.exit_code == 1
    assert "PowerShell completion needs click 8.5 or later (Python 3.10 or later); this environment has click" in (
        result.output
    )
    assert "Register-ArgumentCompleter" not in result.output and "Invoke-Expression" not in result.output
