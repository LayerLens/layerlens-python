from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from layerlens.models import RunResult

from .conftest import ATTEMPT_1, ATTEMPT_2, row, run_view


def _run(**overrides: object) -> RunResult:
    return RunResult.model_validate(
        run_view(
            row(ATTEMPT_1, "pass"),
            row(ATTEMPT_2, "fail", evidence="expected 1 | got 2\nsecond line"),
            row("6a91c0e5f1d2a3b4c5d6e803", "timeout", error_source="agent", evidence="ran 600s"),
            row("6a91c0e5f1d2a3b4c5d6e804", None),
            **overrides,
        )
    )


def test_junit_has_one_case_per_task_with_the_right_outcome(tmp_path: Path) -> None:
    path = tmp_path / "results.xml"
    xml = _run().to_junit(str(path))
    assert path.read_text() == xml
    suite = ElementTree.fromstring(xml).find("testsuite")
    assert suite is not None
    assert suite.attrib == {"name": "support-agent", "tests": "4", "failures": "1", "errors": "1", "skipped": "1"}
    cases = {c.attrib["name"]: c for c in suite.findall("testcase")}
    assert list(cases["task-1"]) == []
    failure = cases["task-2"].find("failure")
    assert failure is not None and failure.attrib["type"] == "fail"
    assert "agent answer: the answer" in (failure.text or "")
    assert cases["task-3"].find("error").attrib["type"] == "timeout"
    assert cases["task-4"].find("skipped") is not None


def test_junit_survives_characters_xml_cannot_hold() -> None:
    run = RunResult.model_validate(run_view(row(ATTEMPT_1, "fail", evidence="bad \x00\x1b byte", agent_answer="\x07")))
    ElementTree.fromstring(run.to_junit())


def test_a_verdict_this_sdk_does_not_know_counts_as_a_failure() -> None:
    run = RunResult.model_validate(run_view(row(ATTEMPT_1, "a_verdict_added_later")))
    suite = ElementTree.fromstring(run.to_junit()).find("testsuite")
    assert suite is not None and suite.attrib["failures"] == "1"
    assert [a.attempt_id for a in run.failures()] == [ATTEMPT_1]


def test_a_verdict_the_app_does_not_count_is_neither_a_pass_nor_a_failure() -> None:
    run = RunResult.model_validate(
        run_view(row(ATTEMPT_1, "pass"), row(ATTEMPT_2, "off_script", evidence="the persona broke a line"))
    )
    assert run.uncounted == 1 and run.failures() == []
    suite = ElementTree.fromstring(run.to_junit()).find("testsuite")
    assert suite is not None
    assert (suite.attrib["failures"], suite.attrib["errors"], suite.attrib["skipped"]) == ("0", "0", "1")
    markdown = run.to_markdown()
    assert "1 of 2 not counted" in markdown and "| task-2 |" not in markdown
    assert "1/1 passed (100.0%)" in markdown.splitlines()[0]


def test_a_server_that_sends_no_counted_rates_over_every_row() -> None:
    view = run_view(row(ATTEMPT_1, "pass"), row(ATTEMPT_2, "off_script"))
    del view["counted"]
    assert RunResult.model_validate(view).out_of == 2


def test_markdown_lists_what_did_not_pass_in_a_table_that_stays_a_table() -> None:
    text = _run().to_markdown()
    assert text.startswith("### LayerLens: support-agent — 1/3 passed (33.3%)")
    rows = [line for line in text.splitlines() if line.startswith("| task-")]
    assert [r.split(" | ")[0] for r in rows] == ["| task-2", "| task-3", "| task-4"]
    assert "expected 1 \\| got 2 second line" in rows[0]
    assert all(r.count(" | ") == 3 for r in rows)


def test_markdown_says_why_a_run_failed() -> None:
    text = _run(status="failure", error="1 of the run's 4 tasks ended in an error on the platform's side").to_markdown()
    assert "**The run failed:** 1 of the run's 4 tasks" in text


def test_a_cost_under_a_cent_is_not_shown_as_zero() -> None:
    assert "<$0.01" in _run(cost_usd=0.0004).to_markdown()
    assert "| - |" in _run(cost_usd=None).to_markdown()


# Text from the API, quoted from an agent's answer or a trace, as an attacker would write it.
MARKUP = (
    "![x](https://evil.example/p.png) [click](https://evil.example) <img src=x> **bold** `code` _u_ ~s~ $x$ & \\ "
    "see HTTPS://evil.example/login or WWW.evil.example"
)
# The same text with every character that would make it markup escaped, so it renders as written.
ESCAPED = (
    r"\!\[x\](https\://evil.example/p.png) \[click\](https\://evil.example) \<img src=x\> \*\*bold\*\* "
    r"\`code\` \_u\_ \~s\~ \$x\$ \& \\ see HTTPS\://evil.example/login or WWW\.evil.example"
)


def test_markdown_shows_text_from_the_api_as_text_never_as_markup() -> None:
    run = RunResult.model_validate(
        run_view(row(ATTEMPT_1, "fail", evidence=MARKUP), agent_name="agent_<b>", label="v1 `rm` | x")
    )
    text = run.to_markdown()
    assert text.startswith(r"### LayerLens: agent\_\<b\> — ")
    assert f"| task-1 | fail | {ESCAPED} |" in text
    # Inside a code span a backslash is shown, not read: a backtick cannot close the span early.
    assert "label `v1 'rm' | x`" in text
