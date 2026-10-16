"""GitHub Actions are pinned to commits where it matters (LAY-4250).

A tag such as ``@v4`` can be moved to different code by whoever controls the
action, and these jobs hold secrets (AWS credentials, ``LAYERLENS_API_KEY``). So:

* in this repository's workflows, every third-party action (anything outside
  ``actions/``) is pinned to a full commit SHA;
* in the samples, which customers copy into their own repositories, every action is.

Dependabot (``.github/dependabot.yml``) keeps the repository's pins current.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import List
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.M)
PINNED = re.compile(r"[^@\s]+@[0-9a-f]{40}")


def _tracked_workflows(*paths: str) -> List[Path]:
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("needs a git checkout to list tracked files")
    out = subprocess.run(
        ["git", "ls-files", "--", *paths], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [REPO_ROOT / f for f in out.splitlines() if f.endswith((".yml", ".yaml"))]


def _unpinned(paths: List[Path], *, first_party_too: bool) -> List[str]:
    wrong = []
    for path in paths:
        for ref in USES.findall(path.read_text()):
            if ref.startswith("./") or "@" not in ref:  # a local action, or not an action reference
                continue
            if not first_party_too and ref.startswith("actions/"):
                continue
            if not PINNED.fullmatch(ref):
                wrong.append(f"{path.relative_to(REPO_ROOT)}: {ref}")
    return wrong


def test_third_party_actions_in_the_workflows_are_pinned_to_a_commit() -> None:
    paths = _tracked_workflows(".github")

    assert any("setup-uv" in p.read_text() for p in paths), "the scan found no workflow using a third-party action"
    wrong = _unpinned(paths, first_party_too=False)
    assert not wrong, "third-party actions referenced by a tag, not a commit:\n" + "\n".join(wrong)


def test_every_action_in_the_samples_is_pinned_to_a_commit() -> None:
    paths = [p for p in _tracked_workflows("samples") if USES.search(p.read_text())]

    assert paths, "the scan found no sample workflow"
    wrong = _unpinned(paths, first_party_too=True)
    assert not wrong, "sample actions referenced by a tag, not a commit:\n" + "\n".join(wrong)
