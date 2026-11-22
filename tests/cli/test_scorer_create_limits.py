"""``layerlens scorer create --help`` states the lengths the API takes.

The help said a description takes 10-500 characters; the API takes 10-280 (``api/contracts/
scorer.go``: name 3-64, description 10-280, prompt at least 10), so a 281-500 character description
the help allowed was refused (docs re-run 2026-11-10, CL-34d). The CLI checks no lengths itself; the
API's refusal says which field is too long.
"""

from __future__ import annotations

from click.testing import CliRunner

from layerlens.cli._app import cli


def test_the_help_states_the_apis_limits() -> None:
    result = CliRunner().invoke(cli, ["scorer", "create", "--help"])
    assert result.exit_code == 0
    shown = " ".join(result.output.split())
    assert "Scorer name (3-64 chars)." in shown
    assert "Scorer description (10-280 chars)." in shown
    assert "Scoring prompt (at least 10 chars)." in shown
    assert "500" not in shown
