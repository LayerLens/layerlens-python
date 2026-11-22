"""The drift canaries reach a person when a row is red (LAY-4242).

Both weekly canaries (Adapter Drift Canary, Lock Drift Canary) run every row as
``continue-on-error``, so their runs are green even when rows fail: the crewai
and dspy rows failed every week for two months and nobody saw. Each row now
records a result file, and ``scripts/drift_canary_report.py`` turns a red row
into a tracking issue. These tests drive the script's ``main`` with a fake
``gh`` executable on ``PATH`` (the GitHub API is the external boundary) that
logs every call and answers ``issue list`` from a canned list of open issues.
"""

from __future__ import annotations

import os
import sys
import json
import textwrap
import subprocess
import importlib.util
from types import ModuleType
from typing import Any, Dict, List
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_URL = "https://github.com/LayerLens/stratix-python-private/actions/runs/123"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "drift_canary_report", REPO_ROOT / "scripts" / "drift_canary_report.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["drift_canary_report"] = module
    spec.loader.exec_module(module)
    return module


report = _load_script()


class FakeGh:
    """A ``gh`` on PATH that logs each call and serves ``issue list`` from ``open_issues``."""

    def __init__(self, bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.log = bin_dir / "gh-calls.jsonl"
        self.issues = bin_dir / "open-issues.json"
        self.failing = bin_dir / "failing-commands.json"
        script = bin_dir / "gh"
        script.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent(
                f"""
                import sys, json
                args = sys.argv[1:]
                body = None
                if "--body-file" in args:
                    with open(args[args.index("--body-file") + 1]) as fh:
                        body = fh.read()
                with open({str(self.log)!r}, "a") as fh:
                    fh.write(json.dumps({{"args": args, "body": body}}) + "\\n")
                with open({str(self.failing)!r}) as fh:
                    for entry in json.load(fh):
                        command, *flags = entry.split()[:2], *entry.split()[2:]
                        if args[:2] == command and all(flag in args for flag in flags):
                            sys.exit("HTTP 403: Resource not accessible by integration")
                if args[:2] == ["issue", "list"]:
                    with open({str(self.issues)!r}) as fh:
                        print(fh.read())
                elif args[:2] == ["issue", "create"]:
                    print("https://github.com/LayerLens/stratix-python-private/issues/77")
                """
            )
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        self.serve_open_issues([])
        self.fail_on([])

    def calls(self) -> List[Dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def commands(self) -> List[List[str]]:
        return [call["args"][:2] for call in self.calls()]

    def serve_open_issues(self, issues: List[Dict[str, Any]]) -> None:
        self.issues.write_text(json.dumps(issues))

    def fail_on(self, commands: List[str]) -> None:
        """Make these ``gh`` commands exit non-zero: "issue create", or "issue create --assignee"
        to fail only when the call carries that flag."""
        self.failing.write_text(json.dumps(commands))


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return FakeGh(bin_dir, monkeypatch)


@pytest.fixture
def results(tmp_path: Path) -> Path:
    path = tmp_path / "results"
    path.mkdir()
    return path


def _record(results: Path, row: str, *, red: bool, verdict: str, versions: "Dict[str, str] | None" = None) -> None:
    report.main(
        ["record", "--out", str(results / f"{row}.json"), "--row", row, "--verdict", verdict]
        + (["--red"] if red else [])
        + [arg for name, version in (versions or {}).items() for arg in ("--version", f"{name}=={version}")]
    )


def _notify(results: Path, *expected: str, assignees: str = "", append: "Path | None" = None) -> int:
    args = ["notify", "--canary", "Adapter Drift Canary", "--results", str(results)]
    args += ["--expect-json", json.dumps(list(expected)), "--run-url", RUN_URL, "--assignees", assignees]
    if append is not None:
        args += ["--append-markdown", str(append)]
    return report.main(args)


class TestGreenRun:
    def test_a_green_run_makes_no_github_call(self, gh: FakeGh, results: Path) -> None:
        _record(results, "crewai", red=False, verdict="pass", versions={"crewai": "1.15.28"})
        _record(results, "dspy", red=False, verdict="pass", versions={"dspy": "3.4.1"})

        assert _notify(results, "crewai", "dspy") == 0

        assert gh.calls() == []

    def test_a_green_run_leaves_an_open_tracking_issue_alone(self, gh: FakeGh, results: Path) -> None:
        gh.serve_open_issues([{"number": 12, "body": "<!-- drift-canary:adapter-drift-canary -->\nold"}])
        _record(results, "crewai", red=False, verdict="pass")

        assert _notify(results, "crewai") == 0

        assert gh.calls() == []


class TestDownloadedArtifactLayout:
    """``download-artifact`` puts each row's artifact in its own directory.

    Every row's file may have the same name there, as each workflow job writes
    one; merging them into one directory kept only the last row's result.
    """

    def _record_in_artifact(self, results: Path, artifact: str, row: str, **kwargs: Any) -> None:
        (results / artifact).mkdir(exist_ok=True)
        report.main(
            ["record", "--out", str(results / artifact / "result.json"), "--row", row, "--verdict", "pass"]
            + (["--red"] if kwargs.get("red") else [])
        )

    def test_green_rows_in_separate_artifacts_make_no_github_call(self, gh: FakeGh, results: Path) -> None:
        for row in ("crewai", "dspy", "openai"):
            self._record_in_artifact(results, f"drift-result-{row}", row)

        assert _notify(results, "crewai", "dspy", "openai") == 0

        assert gh.calls() == []

    def test_only_the_red_row_is_reported(self, gh: FakeGh, results: Path) -> None:
        self._record_in_artifact(results, "drift-result-crewai", "crewai")
        self._record_in_artifact(results, "drift-result-dspy", "dspy", red=True)

        _notify(results, "crewai", "dspy")

        body = next(call["body"] for call in gh.calls() if call["args"][:2] == ["issue", "create"])
        assert "| dspy |" in body
        assert "crewai" not in body

    def test_two_results_for_one_row_fail_the_report(self, gh: FakeGh, results: Path) -> None:
        # Two jobs claiming one row means a workflow bug: one result would hide the other.
        self._record_in_artifact(results, "drift-result-a", "crewai")
        self._record_in_artifact(results, "drift-result-b", "crewai", red=True)

        with pytest.raises(ValueError, match="crewai"):
            _notify(results, "crewai")
        assert gh.calls() == []


class TestRedRunWithoutAnOpenIssue:
    def test_opens_one_issue_listing_the_red_rows_versions_and_run(self, gh: FakeGh, results: Path) -> None:
        _record(results, "crewai", red=True, verdict="FAIL (1 failed)", versions={"crewai": "1.15.28"})
        _record(results, "dspy", red=False, verdict="pass", versions={"dspy": "3.4.1"})

        assert _notify(results, "crewai", "dspy") == 0

        creates = [call for call in gh.calls() if call["args"][:2] == ["issue", "create"]]
        assert len(creates) == 1
        args, body = creates[0]["args"], creates[0]["body"]
        assert args[args.index("--title") + 1] == "Adapter Drift Canary is red"
        assert args[args.index("--label") + 1] == "drift-canary"
        assert "| crewai | FAIL (1 failed) | crewai==1.15.28 |" in body
        assert "dspy" not in body, "green rows are not listed"
        assert RUN_URL in body
        assert "<!-- drift-canary:adapter-drift-canary -->" in body

    def test_creates_the_label_before_using_it(self, gh: FakeGh, results: Path) -> None:
        _record(results, "crewai", red=True, verdict="FAIL")

        _notify(results, "crewai")

        commands = gh.commands()
        assert commands.index(["label", "create"]) < commands.index(["issue", "create"])

    def test_assigns_the_configured_people(self, gh: FakeGh, results: Path) -> None:
        _record(results, "crewai", red=True, verdict="FAIL")

        _notify(results, "crewai", assignees="alice, bob")

        create = next(call["args"] for call in gh.calls() if call["args"][:2] == ["issue", "create"])
        assert [create[i + 1] for i, arg in enumerate(create) if arg == "--assignee"] == ["alice", "bob"]

    def test_an_issue_without_assignees_warns_that_only_watchers_hear_of_it(
        self, gh: FakeGh, results: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _record(results, "crewai", red=True, verdict="FAIL")

        _notify(results, "crewai")

        assert "::warning title=Drift canary issue has no assignee::" in capsys.readouterr().out

    def test_an_issue_with_assignees_does_not_warn(
        self, gh: FakeGh, results: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _record(results, "crewai", red=True, verdict="FAIL")

        _notify(results, "crewai", assignees="alice")

        assert "::warning" not in capsys.readouterr().out

    def test_a_rejected_assignee_still_opens_the_issue_unassigned(
        self, gh: FakeGh, results: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A typo'd or departed login in DRIFT_CANARY_ASSIGNEES must not cost the alert.
        gh.fail_on(["issue create --assignee"])
        _record(results, "crewai", red=True, verdict="FAIL")

        assert _notify(results, "crewai", assignees="no-such-login") == 0

        creates = [call["args"] for call in gh.calls() if call["args"][:2] == ["issue", "create"]]
        assert len(creates) == 2
        assert "--assignee" in creates[0] and "--assignee" not in creates[1]
        assert "::warning title=Drift canary assignees rejected::" in capsys.readouterr().out

    def test_a_row_that_left_no_result_is_red(self, gh: FakeGh, results: Path) -> None:
        # The job crashed or was cancelled before writing its result.
        _record(results, "crewai", red=False, verdict="pass")

        _notify(results, "crewai", "dspy")

        body = next(call["body"] for call in gh.calls() if call["args"][:2] == ["issue", "create"])
        assert "| dspy | no result: the job did not finish |" in body

    def test_appends_the_moved_versions_to_the_issue(self, gh: FakeGh, results: Path, tmp_path: Path) -> None:
        # The Lock Drift Canary's re-lock job writes which versions moved from uv.lock.
        moved = tmp_path / "moved.md"
        moved.write_text("### Versions that moved\n\nUpdated dspy v2.6.13 -> v3.4.1\n")
        _record(results, "python-3.12", red=True, verdict="suite RED")

        _notify(results, "python-3.12", append=moved)

        body = next(call["body"] for call in gh.calls() if call["args"][:2] == ["issue", "create"])
        assert "Updated dspy v2.6.13 -> v3.4.1" in body

    def test_a_missing_markdown_file_is_skipped(self, gh: FakeGh, results: Path, tmp_path: Path) -> None:
        # The re-lock job that would write it failed or never ran; still notify.
        _record(results, "re-lock", red=True, verdict="re-lock FAILED")

        _notify(results, "re-lock", append=tmp_path / "absent.md")

        assert ["issue", "create"] in gh.commands()


class TestRedRunWithAnOpenIssue:
    def test_comments_on_the_open_issue_instead_of_opening_another(self, gh: FakeGh, results: Path) -> None:
        gh.serve_open_issues(
            [
                {"number": 5, "body": "<!-- drift-canary:lock-drift-canary -->\nthe other canary"},
                {"number": 12, "body": "<!-- drift-canary:adapter-drift-canary -->\nlast week"},
            ]
        )
        _record(results, "crewai", red=True, verdict="FAIL (1 failed)", versions={"crewai": "1.15.28"})

        assert _notify(results, "crewai") == 0

        assert ["issue", "create"] not in gh.commands()
        comments = [call for call in gh.calls() if call["args"][:2] == ["issue", "comment"]]
        assert len(comments) == 1
        assert comments[0]["args"][2] == "12"
        assert "| crewai | FAIL (1 failed) | crewai==1.15.28 |" in comments[0]["body"]
        assert RUN_URL in comments[0]["body"]


class TestGitHubFailuresAreRaised:
    """A notification that silently failed would be as invisible as no notification."""

    def test_an_unreadable_issue_list_fails_the_report(self, gh: FakeGh, results: Path) -> None:
        gh.issues.write_text("this is not json")
        _record(results, "crewai", red=True, verdict="FAIL")

        with pytest.raises(json.JSONDecodeError):
            _notify(results, "crewai")

    @pytest.mark.parametrize("command", ["issue list", "label create", "issue create"])
    def test_a_rejected_call_while_opening_an_issue_fails_the_report(
        self, gh: FakeGh, results: Path, command: str
    ) -> None:
        gh.fail_on([command])
        _record(results, "crewai", red=True, verdict="FAIL")

        with pytest.raises(subprocess.CalledProcessError):
            _notify(results, "crewai")

    def test_a_rejected_comment_fails_the_report(self, gh: FakeGh, results: Path) -> None:
        gh.serve_open_issues([{"number": 12, "body": "<!-- drift-canary:adapter-drift-canary -->"}])
        gh.fail_on(["issue comment"])
        _record(results, "crewai", red=True, verdict="FAIL")

        with pytest.raises(subprocess.CalledProcessError):
            _notify(results, "crewai")


class TestRecord:
    def test_writes_the_row_result(self, results: Path) -> None:
        _record(results, "python-3.12", red=True, verdict="suite RED", versions={"openai": "2.54.0"})

        assert json.loads((results / "python-3.12.json").read_text()) == {
            "row": "python-3.12",
            "red": True,
            "verdict": "suite RED",
            "versions": {"openai": "2.54.0"},
        }
