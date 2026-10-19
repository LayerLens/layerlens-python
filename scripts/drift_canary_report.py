#!/usr/bin/env python3
"""Turn a red drift-canary row into a GitHub issue a person sees (LAY-4242).

The weekly drift canaries (``.github/workflows/adapter-drift-canary.yaml`` and
``lock-drift-canary.yaml``) run each row with ``continue-on-error``, so their
runs stay green even when rows fail. That hid upstream breaks for two months.
Each row now records its result, and the canary's last job calls ``notify``:

* any red row: open one tracking issue for that canary (label ``drift-canary``),
  or comment on it if it is already open, listing the red rows, their versions
  and the run link;
* every row green: no GitHub call at all, so an open issue isn't spammed.

A row that left no result (its job crashed or was cancelled) counts as red.
Any failing ``gh`` call fails this script, and with it the canary run.

Usage (stdlib only; ``notify`` needs ``gh`` on PATH and ``GH_TOKEN``):

    drift_canary_report.py record --out R.json --row crewai --verdict "FAIL" --red --version crewai==1.15.28
    drift_canary_report.py notify --canary "Adapter Drift Canary" --results DIR \\
        --expect-json '["crewai", "dspy"]' --run-url URL [--assignees "a, b"] [--append-markdown F.md]
"""

from __future__ import annotations

import os
import re
import sys
import json
import argparse
import tempfile
import subprocess
from typing import Any, Dict, List, Optional, Sequence
from dataclasses import field, dataclass

LABEL = "drift-canary"
LABEL_COLOR = "D93F0B"
LABEL_DESCRIPTION = "A weekly drift canary found a break against the latest upstream releases"
NO_RESULT = "no result: the job did not finish"


@dataclass
class RowResult:
    row: str
    red: bool
    verdict: str
    versions: Dict[str, str] = field(default_factory=dict)


def record(out: str, row: str, verdict: str, red: bool, versions: Sequence[str]) -> None:
    """Write one row's result file (``--version name==1.2.3`` per reported package)."""
    pinned = dict(v.split("==", 1) for v in versions)
    with open(out, "w") as fh:
        json.dump({"row": row, "red": red, "verdict": verdict, "versions": pinned}, fh)


def load_results(results_dir: str, expected: Sequence[str]) -> List[RowResult]:
    """Every result file under ``results_dir``, plus a red row for each expected row without one.

    ``actions/download-artifact`` puts each artifact in its own subdirectory, and
    the row files in them may share a name, so this walks the whole tree. Two
    files for one row mean a workflow bug that would hide a result: that raises.
    """
    rows: Dict[str, RowResult] = {}
    seen_in: Dict[str, str] = {}
    for root, dirs, files in os.walk(results_dir):
        dirs.sort()
        for name in sorted(f for f in files if f.endswith(".json")):
            path = os.path.join(root, name)
            with open(path) as fh:
                data = json.load(fh)
            row = data["row"]
            if row in rows:
                raise ValueError(f"two results for row {row!r}: {seen_in[row]} and {path}")
            seen_in[row] = path
            rows[row] = RowResult(row, bool(data["red"]), data["verdict"], dict(data.get("versions") or {}))
    for row in expected:
        rows.setdefault(row, RowResult(row, True, NO_RESULT))
    return [rows[row] for row in sorted(rows)]


def marker(canary: str) -> str:
    """The hidden line that identifies this canary's tracking issue."""
    return f"<!-- drift-canary:{re.sub(r'[^a-z0-9]+', '-', canary.lower()).strip('-')} -->"


def table(red_rows: Sequence[RowResult]) -> str:
    lines = ["| Row | Verdict | Versions |", "|---|---|---|"]
    for r in red_rows:
        versions = ", ".join(f"{name}=={version}" for name, version in sorted(r.versions.items())) or "-"
        lines.append(f"| {r.row} | {r.verdict} | {versions} |")
    return "\n".join(lines)


def issue_body(canary: str, red_rows: Sequence[RowResult], run_url: str, extra: str = "") -> str:
    return "\n\n".join(
        [
            marker(canary),
            f"The weekly **{canary}** has red rows: they fail against the latest upstream "
            "releases, so a customer installing those releases today may hit the same failure.",
            table(red_rows),
            f"Run: {run_url}",
            'Triage each row (CONTRIBUTING.md > "Dependencies and the lock"): fix real breaks '
            "test-first, then move the pins or the lock forward. Later red runs comment here. "
            "Close this issue once the canary is green.",
        ]
        + ([extra] if extra else [])
    )


def comment_body(canary: str, red_rows: Sequence[RowResult], run_url: str, extra: str = "") -> str:
    return "\n\n".join([f"**{canary}** is still red.", table(red_rows), f"Run: {run_url}"] + ([extra] if extra else []))


def gh(*args: str) -> str:
    """Run ``gh``. A failure prints gh's error and raises, so it can't go unnoticed."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        proc.check_returncode()
    return proc.stdout


def _gh_with_body(body: str, *args: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(body)
    try:
        return gh(*args, "--body-file", fh.name)
    finally:
        os.unlink(fh.name)


def open_tracking_issue(canary: str) -> Optional[int]:
    issues: List[Dict[str, Any]] = json.loads(
        gh("issue", "list", "--state", "open", "--label", LABEL, "--json", "number,body", "--limit", "100")
    )
    tag = marker(canary)
    return next((issue["number"] for issue in issues if tag in (issue.get("body") or "")), None)


def notify(canary: str, rows: Sequence[RowResult], run_url: str, assignees: Sequence[str], extra: str = "") -> str:
    """Open or update the tracking issue for red rows; do nothing when every row is green.

    ``extra`` is markdown appended to the issue or comment (e.g. the versions that moved).
    """
    red_rows = [r for r in rows if r.red]
    if not red_rows:
        return "every row is green: no notification"

    number = open_tracking_issue(canary)
    if number is not None:
        _gh_with_body(comment_body(canary, red_rows, run_url, extra), "issue", "comment", str(number))
        return f"commented on #{number}"

    gh("label", "create", LABEL, "--color", LABEL_COLOR, "--description", LABEL_DESCRIPTION, "--force")
    args = ["issue", "create", "--title", f"{canary} is red", "--label", LABEL]
    for person in assignees:
        args += ["--assignee", person]
    if not assignees:
        # An unassigned issue opened by the Actions bot notifies only the people
        # watching the repository's "All Activity"; say so where the run shows it.
        print(  # noqa: T201
            "::warning title=Drift canary issue has no assignee::Only people watching this repository's "
            "All Activity are notified. Set the DRIFT_CANARY_ASSIGNEES repository variable to assign people."
        )
    body = issue_body(canary, red_rows, run_url, extra)
    try:
        url = _gh_with_body(body, *args).strip()
    except subprocess.CalledProcessError:
        if not assignees:
            raise
        # A login GitHub rejects (a typo, someone who left) must not cost the alert.
        print(  # noqa: T201
            "::warning title=Drift canary assignees rejected::Opened the issue without assignees; "
            "check the DRIFT_CANARY_ASSIGNEES repository variable."
        )
        url = _gh_with_body(body, "issue", "create", "--title", f"{canary} is red", "--label", LABEL).strip()
    return f"opened {url}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="write one row's result file")
    rec.add_argument("--out", required=True)
    rec.add_argument("--row", required=True)
    rec.add_argument("--verdict", required=True)
    rec.add_argument("--red", action="store_true", help="the row failed")
    rec.add_argument("--version", action="append", default=[], metavar="NAME==VERSION")

    note = sub.add_parser("notify", help="open or update the tracking issue if any row is red")
    note.add_argument("--canary", required=True, help='workflow name, e.g. "Adapter Drift Canary"')
    note.add_argument("--results", required=True, help="directory of row result files")
    note.add_argument("--expect-json", default="[]", help="JSON list of rows that must have a result")
    note.add_argument("--run-url", required=True)
    note.add_argument("--assignees", default="", help="comma-separated GitHub logins to assign a new issue to")
    note.add_argument("--append-markdown", default=None, help="markdown file to append, if it exists")

    args = parser.parse_args(argv)
    if args.command == "record":
        record(args.out, args.row, args.verdict, args.red, args.version)
        return 0

    rows = load_results(args.results, json.loads(args.expect_json))
    assignees = [a.strip() for a in args.assignees.split(",") if a.strip()]
    extra = ""
    if args.append_markdown and os.path.exists(args.append_markdown):
        with open(args.append_markdown) as fh:
            extra = fh.read().strip()
    print(f"{args.canary}: {notify(args.canary, rows, args.run_url, assignees, extra)}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
