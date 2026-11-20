"""The publish workflows run in the repositories that publish.

Both repositories were renamed after 1.11.0: LayerLens/stratix-python is now
LayerLens/layerlens-python, and the private upstream is
LayerLens/layerlens-python-private. publish-to-aws.yaml guarded its jobs with
``startsWith(github.repository, 'LayerLens/stratix-python')``, which neither new
name passes, so the publish that auto-release.yaml dispatches for a release tag
skipped both jobs. A run whose jobs all skip reports success: v2.0.0 would have
been tagged and never reached sdk.layerlens.ai.

The jobs now run in any LayerLens repository (forks still skip), and their first
step names the two that publish and fails on any other. These tests run that
step, and hold every repository a publish workflow compares ``github.repository``
with to a name the step accepts, so a rename that updates one place and misses
another fails here instead of at the next release.
"""

from __future__ import annotations

import os
import re
import shutil
import textwrap
import subprocess
from typing import List
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
PUBLISH_TO_AWS = WORKFLOWS / "publish-to-aws.yaml"
PUBLISH_WORKFLOWS = [PUBLISH_TO_AWS, WORKFLOWS / "publish-sdk.yaml"]
CHECK_STEP = "Check this repository publishes"

PUBLIC = "LayerLens/layerlens-python"
PRIVATE = "LayerLens/layerlens-python-private"

# A repository name compared with github.repository, in either form a guard uses.
COMPARED = re.compile(r"github\.repository\s*==\s*'([^']+)'|startsWith\(\s*github\.repository\s*,\s*'([^']+)'\s*\)")
JOB_IF = re.compile(r"^ {4}if:\s*(.+)$", re.M)
JOB_ENVIRONMENT = re.compile(r"^ {4}environment:\s*(.+)$", re.M)
PRODUCTION = re.compile(r"github\.repository\s*==\s*'([^']+)'\s*&&\s*'production'")


def _compared(text: str) -> List[str]:
    return [a or b for a, b in COMPARED.findall(text)]


def _check_script() -> str:
    lines = PUBLISH_TO_AWS.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == f"- name: {CHECK_STEP}"]
    assert len(starts) == 1, f"publish-to-aws.yaml needs exactly one {CHECK_STEP!r} step"
    start = starts[0]
    checkout = next(i for i, line in enumerate(lines) if "uses: actions/checkout" in line)
    assert start < checkout, "the repository check must run before the validate job does anything else"

    item_indent = len(lines[start]) - len(lines[start].lstrip())
    body: List[str] = []
    run_indent = None
    env_ok = False
    for line in lines[start + 1 :]:
        indent = len(line) - len(line.lstrip())
        if line.strip() and indent <= item_indent:
            break  # the next step
        if run_indent is None:
            env_ok = env_ok or line.strip() == "REPOSITORY: ${{ github.repository }}"
            if line.strip() == "run: |":
                run_indent = indent
            continue
        if line.strip() and indent <= run_indent:
            break
        body.append(line)
    assert env_ok, "the step must read the repository from github.repository, as REPOSITORY"
    assert body, f"the {CHECK_STEP!r} step has no run: | script"
    return textwrap.dedent("\n".join(body))


def _check(repository: str) -> "subprocess.CompletedProcess[str]":
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("needs bash to run the workflow step")
    return subprocess.run(
        [bash, "-c", _check_script()],
        env={**os.environ, "REPOSITORY": repository},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("repository", [PUBLIC, PRIVATE])
def test_the_two_publishing_repositories_pass_the_check(repository: str) -> None:
    result = _check(repository)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "repository",
    [
        "LayerLens/stratix-python",  # the names before the rename
        "LayerLens/stratix-python-private",
        "someone/layerlens-python",  # a fork
        "LayerLens/layerlens-python-docs",  # a prefix match is not a match
    ],
)
def test_any_other_repository_fails_the_check_and_says_why(repository: str) -> None:
    result = _check(repository)

    assert result.returncode == 1
    assert f"::error title=Unknown repository::{repository} is neither" in result.stdout


def test_no_publish_job_skips_on_a_repository_name() -> None:
    # A job skipped by its guard reports success, so a guard that names the repository
    # turns a rename into a release that silently publishes nothing.
    guards = JOB_IF.findall(PUBLISH_TO_AWS.read_text())

    assert len(guards) == 2, "expected the validate and deploy jobs' guards"
    for guard in guards:
        assert guard.strip() == "github.repository_owner == 'LayerLens'"


def test_every_repository_a_publish_workflow_names_passes_the_check() -> None:
    named = sorted({name for path in PUBLISH_WORKFLOWS for name in _compared(path.read_text())})

    assert PUBLIC in named, "the publish workflows name no public repository: the scan is broken"
    refused = [name for name in named if _check(name).returncode != 0]
    assert not refused, f"publish workflows compare github.repository with names the check refuses: {refused}"


def test_the_public_repository_publishes_to_production_and_pypi() -> None:
    environments = JOB_ENVIRONMENT.findall(PUBLISH_TO_AWS.read_text())
    production = [PRODUCTION.search(environment) for environment in environments]

    assert len(environments) == 2 and all(production), environments
    assert {match.group(1) for match in production if match} == {PUBLIC}
    assert set(_compared((WORKFLOWS / "publish-sdk.yaml").read_text())) == {PUBLIC}
