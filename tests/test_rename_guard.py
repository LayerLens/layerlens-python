"""Customer-facing code, docs and samples teach LayerLens, not Stratix (LAY-4220).

The deprecated names still work (see ``test_rename_compat.py``), but nothing a
customer reads or copies should teach them: a sample using ``Stratix()`` would
hand every reader a DeprecationWarning. This scans the tracked customer-facing
files for the deprecated identifiers, environment variables and command name.

The only places allowed to name them are the compatibility shims themselves and
lines that link the migration guide, which has to say what was renamed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import List
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

SCANNED = ["src", "samples", "docs", "README.md", "CONTRIBUTING.md", "SECURITY.md"]

DEPRECATED = re.compile(
    r"\b(?:Async)?Stratix(?:Error)?\b"
    r"|\bAsyncAtlas\b|\bAtlasError\b|\bAtlas\("
    r"|\bLAYERLENS_(?:STRATIX|ATLAS)_(?:API_KEY|BASE_URL)\b"
    r"|\bstratix (?:trace|judge|evaluate|evaluations|integration|scorer|bulk|ci|login|logout|whoami"
    r"|completion|replay|synthetic|--version|--help)\b"
)

# The compatibility layer has to name what it keeps working.
SHIMS = {
    "src/layerlens/__init__.py",
    "src/layerlens/_client.py",
    "src/layerlens/_exceptions.py",
    "src/layerlens/_deprecation.py",
    "src/layerlens/_env.py",
    "src/layerlens/cli/_client.py",
    "src/layerlens/instrument/adapters/protocols/agui/event_mapper.py",
}

MIGRATION_GUIDE = "docs/migrating-from-stratix.md"

TEXT_SUFFIXES = (
    ".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".sh", ".ipynb", ".example", ".cfg", ".ini",
    ".ts", ".tsx", ".js", ".jsx", ".json", ".html", ".css",
)  # fmt: skip


def _tracked_files() -> List[str]:
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("needs a git checkout to list tracked files")
    out = subprocess.run(
        ["git", "ls-files", "--", *SCANNED],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # Extensionless files are notes like a vendored sample's UPSTREAM provenance.
    return [f for f in out.splitlines() if f.endswith(TEXT_SUFFIXES) or "." not in Path(f).name]


def test_migration_guide_exists() -> None:
    guide = REPO_ROOT / MIGRATION_GUIDE
    assert guide.is_file(), f"{MIGRATION_GUIDE} is linked from the README and docs"
    text = guide.read_text()
    for old, new in [
        ("Stratix", "LayerLens"),
        ("AsyncStratix", "AsyncLayerLens"),
        ("StratixError", "LayerLensError"),
        ("LAYERLENS_STRATIX_API_KEY", "LAYERLENS_API_KEY"),
        ("LAYERLENS_STRATIX_BASE_URL", "LAYERLENS_BASE_URL"),
    ]:
        assert old in text and new in text, f"migration guide must map {old} -> {new}"


def test_customer_facing_files_use_the_new_names() -> None:
    offenders = []
    for path in _tracked_files():
        if path in SHIMS or path == MIGRATION_GUIDE:
            continue
        for lineno, line in enumerate((REPO_ROOT / path).read_text(errors="replace").splitlines(), 1):
            if "migrating-from-stratix" in line:
                continue
            match = DEPRECATED.search(line)
            if match:
                offenders.append(f"{path}:{lineno}: {match.group(0)!r} in {line.strip()[:100]}")

    assert not offenders, "deprecated Stratix names in customer-facing files:\n" + "\n".join(offenders)
