"""Every CLI invocation shown in the docs and samples names a real command.

A command removed from the CLI (2.0.0 dropped ``space``) must not live on in
documentation or sample scripts, where a reader copies it and gets
"No such command". This finds each ``layerlens <command> [<subcommand>]`` a doc
or sample shows — at the start of a line, inside ``$(...)`` or in inline
backticks — and checks it against the CLI's actual command tree.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from typing import List, Tuple, Optional
from pathlib import Path

import click
import pytest

from layerlens.cli._app import cli

REPO_ROOT = Path(__file__).resolve().parents[2]

SCANNED = ["docs", "samples", "README.md", "src/README.md"]
SUFFIXES = (".md", ".sh", ".py", ".yml", ".yaml", ".txt")

# Root options that consume the following token as their value.
VALUE_OPTIONS = {"--api-key", "--host", "--port", "--format"}

INVOCATION = re.compile(r"(?:^\s*(?:\$\s+)?|\$\(|`)layerlens\s+([^`\n|;&)]*)")


def _tracked_files() -> List[str]:
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("needs a git checkout to list tracked files")
    out = subprocess.run(
        ["git", "ls-files", "--", *SCANNED], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [f for f in out.splitlines() if f.endswith(SUFFIXES)]


def _command_path(args: str) -> Optional[Tuple[str, ...]]:
    """The command (and subcommand, for a group) an invocation names, or None."""
    try:
        tokens = shlex.split(args, comments=True)
    except ValueError:
        tokens = args.split()
    path: List[str] = []
    skip_value = False
    for token in tokens:
        if skip_value:
            skip_value = False
            continue
        if token in VALUE_OPTIONS:
            skip_value = True
            continue
        if token.startswith("-") or token.startswith("<") or token == "\\":
            continue
        if not re.fullmatch(r"[a-z][a-z-]*", token):
            break
        path.append(token)
        if len(path) == 2:
            break
    return tuple(path) or None


def _resolves(path: Tuple[str, ...]) -> bool:
    command = cli.commands.get(path[0])
    if command is None:
        return False
    if isinstance(command, click.Group) and len(path) > 1:
        return path[1] in command.commands
    return True


def test_documented_invocations_name_real_commands() -> None:
    unknown = []
    checked = 0
    for path in _tracked_files():
        for lineno, line in enumerate((REPO_ROOT / path).read_text(errors="replace").splitlines(), 1):
            for match in INVOCATION.finditer(line):
                command = _command_path(match.group(1))
                if command is None:
                    continue
                checked += 1
                if not _resolves(command):
                    unknown.append(f"{path}:{lineno}: layerlens {' '.join(command)}")

    assert checked > 50, f"only {checked} invocations found; the scanner is not seeing the docs"
    assert not unknown, "docs/samples show commands the CLI does not have:\n" + "\n".join(unknown)
