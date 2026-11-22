"""M4: what the SDK says about a composite's apps, now that an API key never sees or uses them.

API main (LAY-4519) leaves a composite's apps out of an API key's environment list and refuses
them on every agent and Automation route with a 409 naming the composite; every ``env create``
makes a one-app composite. ``env list --hide-members`` and ``list(hide_members=True)`` stay, so
callers don't break, and say they change nothing.
"""

from __future__ import annotations

from click.testing import CliRunner

from layerlens.cli._app import cli
from layerlens.resources.environments.environments import Environments, AsyncEnvironments


def test_hide_members_says_it_is_kept_for_compatibility(runner: CliRunner) -> None:
    shown = " ".join(runner.invoke(cli, ["env", "list", "--help"]).output.split())
    assert "Kept for compatibility: an API key's list already leaves out a composite's apps" in shown
    assert "members of a composite" not in shown
    assert "--search support --hide-members" not in shown


def test_the_docstrings_say_the_same() -> None:
    for method in (Environments.list, AsyncEnvironments.list):
        doc = " ".join((method.__doc__ or "").split())
        assert "changes nothing" in doc, method
    created = " ".join((Environments.create.__doc__ or "").split())
    assert "member environment" not in created
    assert "refuse the app with a 409 naming its composite" in created
