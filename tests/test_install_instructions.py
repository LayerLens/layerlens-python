"""Every instruction to install layerlens gets this SDK, from the LayerLens index (LAY-4250).

``layerlens`` on public PyPI is an unrelated project (navi-04/LayerLens):

* a bare ``pip install layerlens`` installs it;
* ``pip install --extra-index-url https://sdk.layerlens.ai/package layerlens`` merges
  the two indexes and installs whichever ``layerlens`` has the higher version, so a
  higher-versioned release on PyPI would replace this SDK;
* ``--index-url https://sdk.layerlens.ai/package`` alone fails, because that index
  hosts only ``layerlens`` and pip cannot find its dependencies.

So the docs install with uv (``uv pip install --extra-index-url …``), which takes
each package from the first index that has it. The one pip form allowed is the
two-step one the installation guide documents: ``pip install --no-deps --index-url
<the LayerLens index> layerlens``, then, on the next line, a plain ``pip install``
that adds the dependencies and keeps the installed ``layerlens``.

A reader copies these lines into a terminal or a CI job, so a wrong one ships the
wrong code into their environment. This finds every install command for layerlens
in the docs, samples, README and the SDK's own messages, and every requirements
file that depends on it.
"""

from __future__ import annotations

import re
import sys
import shutil
import subprocess
from typing import Set, List, Tuple
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # py3.9/3.10: tomli ships with pytest's dependencies
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]

SCANNED = ["README.md", "docs", "samples", "src", "vscode-extension", "tests/e2e/live/README.md"]
SUFFIXES = (".md", ".py", ".sh", ".yml", ".yaml", ".txt", ".ts", ".json", ".toml")

INDEX_URL = "https://sdk.layerlens.ai/package"
EXTRA_INDEX = f"--extra-index-url {INDEX_URL}"
PIP_STEP_ONE = f"pip install --no-deps --index-url {INDEX_URL} layerlens"

# `layerlens` as a package to install: not part of sdk.layerlens.ai, layerlens.cli or layerlens-vscode.
PACKAGE = r"(?<![\w./-])layerlens(?![\w./-])"
# A command ends at a backtick: prose that mentions `pip install` and, later, `layerlens` is not one.
INSTALL_COMMAND = re.compile(
    r"\b(?:uv pip|pip3?|pipx|python3? -m pip)\s+install\b[^`]*" + PACKAGE + r"|\b(?:uv|poetry)\s+add\b[^`]*" + PACKAGE
)
UV_INSTALL = re.compile(r"\buv pip install\b")
REQUIREMENT = re.compile(r"^\s*" + PACKAGE + r"\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;]|$)")
REQUIREMENTS_FILE = re.compile(r"requirements.*\.(txt|lock|in)")


def _tracked_files(*paths: str) -> List[Path]:
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("needs a git checkout to list tracked files")
    out = subprocess.run(
        ["git", "ls-files", "--", *paths], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [REPO_ROOT / f for f in out.splitlines()]


def _install_commands() -> List[Tuple[Path, int, str, str]]:
    """(path, lineno, line, previous non-blank line) for every install command for layerlens."""
    found = []
    for path in _tracked_files(*SCANNED):
        if not path.name.endswith(SUFFIXES):
            continue
        previous = ""
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if INSTALL_COMMAND.search(line):
                found.append((path.relative_to(REPO_ROOT), lineno, line.strip(), previous))
            if line.strip():
                previous = line.strip()
    return found


def test_install_commands_use_uv_with_the_layerlens_index() -> None:
    commands = _install_commands()
    wrong = []
    for path, lineno, line, previous in commands:
        if UV_INSTALL.search(line):
            ok = EXTRA_INDEX in line
        else:
            # pip only as the documented two-step: layerlens alone from the LayerLens
            # index with --no-deps, then the dependencies on the very next line.
            ok = line.endswith(PIP_STEP_ONE) or (previous.endswith(PIP_STEP_ONE) and "index-url" not in line)
        if not ok:
            wrong.append(f"{path}:{lineno}: {line}")

    assert len(commands) > 100, f"only {len(commands)} install commands found; the scanner is not seeing the docs"
    assert not wrong, f"install commands that are not `uv pip install {EXTRA_INDEX} …`:\n" + "\n".join(wrong)


def test_install_commands_double_quote_the_extras() -> None:
    # zsh, the default shell on macOS, globs an unquoted `layerlens[cli]` and fails
    # with "no matches found" before pip runs; Windows cmd.exe passes single quotes
    # through, so pip gets `'layerlens[cli]'`, which is not a requirement. Double
    # quotes work in bash, zsh, PowerShell and cmd.exe.
    unquoted = re.compile(r'(?<!")' + PACKAGE + r"\[")
    wrong = [f"{path}:{lineno}: {line}" for path, lineno, line, _ in _install_commands() if unquoted.search(line)]

    assert not wrong, "install commands with an unquoted extra:\n" + "\n".join(wrong)


def _defined_extras() -> Set[str]:
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        return set(tomllib.load(f)["project"]["optional-dependencies"])


def test_install_commands_name_extras_that_exist() -> None:
    # pip and uv only warn about an unknown extra and install without it, so the
    # sample that needed it then fails on import.
    defined = _defined_extras()
    wrong = []
    for path, lineno, line, _ in _install_commands():
        for extras in re.findall(PACKAGE + r"\[([^\]]+)\]", line):
            unknown = [e.strip() for e in extras.split(",") if e.strip() not in defined and e.strip() != "%s"]
            if unknown:
                wrong.append(f"{path}:{lineno}: {unknown} in {line}")

    assert defined, "no extras read from pyproject.toml"
    assert not wrong, "install commands naming extras pyproject.toml does not define:\n" + "\n".join(wrong)


def _requirements_files_with_layerlens() -> List[Path]:
    return [
        path
        for path in _tracked_files("samples", "docs")
        if REQUIREMENTS_FILE.fullmatch(path.name) and any(REQUIREMENT.match(l) for l in path.read_text().splitlines())
    ]


def test_requirements_files_that_need_layerlens_use_the_layerlens_index() -> None:
    wrong = [
        str(path.relative_to(REPO_ROOT))
        for path in _requirements_files_with_layerlens()
        if not any(line.strip() == EXTRA_INDEX for line in path.read_text().splitlines())
    ]

    assert not wrong, f"requirements files that pull layerlens without an `{EXTRA_INDEX}` line:\n" + "\n".join(wrong)


def test_requirements_files_that_need_layerlens_are_installed_with_uv() -> None:
    # pip applies the file's --extra-index-url line the way it applies the flag:
    # it merges the indexes, so a higher-versioned `layerlens` on PyPI would win.
    names = {path.name for path in _requirements_files_with_layerlens()}
    assert names, "no requirements file pulls layerlens; the check below would check nothing"
    install_r = re.compile(r"\b(?P<tool>uv pip|pip3?|python3? -m pip)\s+install\b[^`\n]*?\s-r\s+(?P<file>[^\s`]+)")
    wrong = []
    for path in _tracked_files(*SCANNED):
        if not path.name.endswith(SUFFIXES):
            continue
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            for m in install_r.finditer(line):
                if Path(m.group("file")).name in names and m.group("tool") != "uv pip":
                    wrong.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert not wrong, "requirements files with layerlens installed with pip instead of uv:\n" + "\n".join(wrong)
