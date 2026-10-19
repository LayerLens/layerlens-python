#!/usr/bin/env python3
"""Real-framework matrix runner (LAY-3580 / T6; canary mode LAY-3581 / T7).

Creates an isolated uv venv for one framework row from ``frameworks.toml``,
installs the repo (editable) plus the row's pinned framework versions, and
runs that framework's real-framework test modules.

A row FAILS when any test fails/errors, when nothing was collected, or when
anything skipped — a skip inside a row whose framework IS installed means the
suite silently stopped covering something (the B2 failure mode this matrix
exists to catch).

``--result-json PATH`` also writes the row's result (row, red, verdict, installed
versions) for ``scripts/drift_canary_report.py``, which the drift canary uses to
open a tracking issue when a row is red (LAY-4242).

Usage:
    python tests/matrix/run_matrix.py --framework crewai
    python tests/matrix/run_matrix.py --framework crewai --latest   # drift canary
    python tests/matrix/run_matrix.py --list
"""

from __future__ import annotations

import os
import re
import sys
import json
import shutil
import argparse
import tempfile
import subprocess
from typing import Any, Dict, List

try:
    import tomllib  # py >= 3.11
except ImportError:  # pragma: no cover - py3.9/3.10
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPEC_PATH = os.path.join(REPO_ROOT, "tests", "matrix", "frameworks.toml")


def load_spec() -> Dict[str, Any]:
    with open(SPEC_PATH, "rb") as f:
        return tomllib.load(f)["frameworks"]


def _run(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, **kwargs)


def _installed_versions(python: str, names: List[str]) -> Dict[str, str]:
    script = (
        "import json, importlib.metadata as md, sys\n"
        "names = json.loads(sys.argv[1])\n"
        "out = {}\n"
        "for n in names:\n"
        "    try:\n"
        "        out[n] = md.version(n)\n"
        "    except Exception:\n"
        "        pass\n"
        "print(json.dumps(out))\n"
    )
    proc = subprocess.run([python, "-c", script, json.dumps(names)], capture_output=True, text=True)
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}


def row_result(name: str, versions: Dict[str, str], returncode: int, stdout: str) -> Dict[str, Any]:
    """The row's verdict from pytest's exit code and terminal summary ("N passed, M skipped, ...")."""

    def _count(label: str) -> int:
        m = re.search(rf"(\d+) {label}\b", stdout)  # \b: "error" must not also match "2 errors"
        return int(m.group(1)) if m else 0

    passed = _count("passed")
    failed = _count("failed") + _count("error") + _count("errors")
    skipped = _count("skipped")
    xfailed = _count("xfailed")
    tests = passed + failed + skipped + xfailed

    if tests == 0:
        verdict = "FAIL (collected nothing)"
    elif returncode != 0 or failed:
        verdict = f"FAIL: {failed} failed, {skipped} skipped of {tests}"
    elif skipped > 0:
        verdict = f"FAIL (skips inside an installed-framework row): {skipped} skipped of {tests}"
    else:
        verdict = "pass"
    return {"row": name, "red": verdict != "pass", "verdict": verdict, "versions": versions}


def install_failed_result(name: str) -> Dict[str, Any]:
    return {"row": name, "red": True, "verdict": "FAIL: dependency install failed", "versions": {}}


def write_result(path: str, result: Dict[str, Any]) -> None:
    with open(path, "w") as f:
        json.dump(result, f)


def run_row(
    name: str,
    row: Dict[str, Any],
    *,
    latest: bool,
    keep_venv: bool,
    venv_dir: str | None,
    result_json: str | None = None,
) -> int:
    workdir = venv_dir or tempfile.mkdtemp(prefix=f"layerlens-matrix-{name}-")
    venv = os.path.join(workdir, "venv")
    python = os.path.join(venv, "bin", "python")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")

    pins: List[str] = list(row["install"])
    if latest:
        # Strip version specifiers — the drift canary floats to latest.
        pins = [
            re.split(r"[=<>!~\[]", p, maxsplit=1)[0] + (p[p.index("[") : p.index("]") + 1] if "[" in p else "")
            for p in pins
        ]

    # Installing the SDK with the row's extras applies the extras' own bounds.
    extras = row.get("extras") or []
    project = f"{REPO_ROOT}[{','.join(extras)}]" if extras else REPO_ROOT

    try:
        if (
            _run(["uv", "venv", "--python", str(row.get("python", "3.11")), venv]).returncode != 0
            or _run(["uv", "pip", "install", "--python", python, "-e", project, "pytest", *pins]).returncode != 0
        ):
            print(f"::error::[{name}] dependency install failed (pins={pins})")
            if result_json:
                write_result(result_json, install_failed_result(name))
            return 2

        dist_names = [re.split(r"[=<>!~\[]", p, maxsplit=1)[0] for p in row["install"]]
        versions = _installed_versions(python, dist_names)
        print(f"[{name}] installed: {versions}")

        proc = _run(
            [python, "-m", "pytest", *row["tests"], "-q", "-rs"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)

        result = row_result(name, versions, proc.returncode, proc.stdout)
        verdict = result["verdict"]
        line = f"| {name} | {', '.join(f'{k}=={v}' for k, v in versions.items()) or '?'} | {verdict} |"
        print(f"[{name}] -> {verdict}")
        if summary_path:
            with open(summary_path, "a") as f:
                f.write(line + "\n")
        if result_json:
            write_result(result_json, result)

        return 1 if result["red"] else 0
    finally:
        if not keep_venv and venv_dir is None:
            shutil.rmtree(workdir, ignore_errors=True)


#: Paths that affect every adapter — any hit runs the full matrix.
_CORE_PATTERNS = re.compile(
    r"^(src/layerlens/instrument/[^/]+\.py"
    r"|src/layerlens/instrument/adapters/_[^/]+\.py"
    r"|tests/matrix/"
    r"|\.github/workflows/adapter-matrix\.yaml)"
)

#: Rows whose files don't all contain the row name.
_ROW_ALIASES = {
    "vector_store": ["vector_store", "chromadb"],
    "semantic_kernel": ["semantic_kernel", "ms_agent_framework"],
    # The embedding floor exercises the vector_store adapter too, so a
    # vector_store.py source change must also run the embedding row.
    "embedding": ["embedding", "vector_store"],
    # The marvin row also runs the pydantic-ai adapter's tests in the
    # pydantic-ai the `[marvin]` extra installs.
    "marvin": ["marvin", "pydantic_ai"],
    # The OpenRouter adapter runs on the openai SDK too.
    "openai": ["openai", "openrouter"],
    # The SDK without extras: any source change, or a change to its dependency bounds.
    "core": ["src/layerlens/", "pyproject.toml"],
}


def pick_rows(rows: List[str], changed: List[str]) -> List[str]:
    """Return the rows a PR diff touches (full list on core changes)."""
    changed = [c.strip() for c in changed if c.strip()]
    if any(_CORE_PATTERNS.match(path) for path in changed):
        return rows
    picked = []
    for row in rows:
        needles = _ROW_ALIASES.get(row, [row])
        if any(needle in path for needle in needles for path in changed):
            picked.append(row)
    return picked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--framework", help="row name from frameworks.toml")
    parser.add_argument("--latest", action="store_true", help="drift-canary mode: ignore pins, install latest")
    parser.add_argument("--list", action="store_true", help="print row names (for CI matrix generation)")
    parser.add_argument(
        "--pick",
        action="store_true",
        help="read changed file paths on stdin, print the JSON list of rows to run (PR path filter)",
    )
    parser.add_argument("--keep-venv", action="store_true")
    parser.add_argument("--result-json", default=None, help="also write the row's result here (drift canary)")
    parser.add_argument("--venv-dir", default=None)
    args = parser.parse_args()

    spec = load_spec()
    if args.list:
        print(json.dumps(sorted(spec)))
        return 0
    if args.pick:
        print(json.dumps(pick_rows(sorted(spec), sys.stdin.read().splitlines())))
        return 0
    if not args.framework:
        parser.error("--framework is required (or use --list)")
    if args.framework not in spec:
        parser.error(f"unknown framework {args.framework!r}; known: {sorted(spec)}")

    if os.environ.get("GITHUB_STEP_SUMMARY") and os.environ.get("MATRIX_SUMMARY_HEADER"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("| framework | versions | verdict |\n|---|---|---|\n")

    return run_row(
        args.framework,
        spec[args.framework],
        latest=args.latest,
        keep_venv=args.keep_venv,
        venv_dir=args.venv_dir,
        result_json=args.result_json,
    )


if __name__ == "__main__":
    sys.exit(main())
