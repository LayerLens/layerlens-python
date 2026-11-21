"""JUnit XML and Markdown renderings of a run, for CI systems and job summaries."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Optional
from xml.etree import ElementTree

if TYPE_CHECKING:
    from .environment import RunResult, RunAttempt, RunSummary, RunComparison

# XML 1.0 cannot carry these code points, even escaped.
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

_MARKDOWN_EVIDENCE_CHARS = 300


def _xml_text(text: Optional[str]) -> str:
    return _XML_INVALID.sub("�", text or "")


def _suite_name(run: RunSummary) -> str:
    return run.agent_name or run.model or "run"


def _case_name(attempt: RunAttempt) -> str:
    return attempt.task_name or attempt.task_id


def _details(attempt: RunAttempt) -> str:
    lines: List[str] = []
    if attempt.evidence:
        lines.append(attempt.evidence)
    if attempt.error_source:
        lines.append(f"error source: {attempt.error_source}")
    if attempt.agent_answer:
        clipped = " (clipped)" if attempt.agent_answer_truncated else ""
        lines.append(f"agent answer{clipped}: {attempt.agent_answer}")
    if attempt.trace_id:
        lines.append(f"trace: {attempt.trace_id}")
    return "\n".join(lines)


def run_to_junit(run: RunResult, *, suite_name: Optional[str] = None) -> str:
    name = suite_name or _suite_name(run)
    failures = errors = skipped = 0
    suite = ElementTree.Element("testsuite", name=_xml_text(name))
    properties = ElementTree.SubElement(suite, "properties")
    for key, value in (
        ("run_id", run.run_id),
        ("kind", run.kind),
        ("status", run.status),
        ("model", run.model),
        ("label", run.label),
        ("environment_version", str(run.environment_version)),
        ("pass_rate", None if run.pass_rate is None else f"{run.pass_rate:.4f}"),
    ):
        if value:
            ElementTree.SubElement(properties, "property", name=key, value=_xml_text(value))

    for attempt in run.attempts:
        case = ElementTree.SubElement(suite, "testcase", name=_xml_text(_case_name(attempt)), classname=_xml_text(name))
        verdict = attempt.verdict
        if verdict is None:
            skipped += 1
            ElementTree.SubElement(case, "skipped", message="not graded yet")
        elif not attempt.counted:
            skipped += 1
            ElementTree.SubElement(case, "skipped", message=_xml_text(f"{verdict}: not counted"))
        elif verdict == "pass":
            continue
        elif verdict in ("error", "timeout"):
            errors += 1
            element = ElementTree.SubElement(
                case, "error", message=_xml_text(attempt.evidence or verdict), type=verdict
            )
            element.text = _xml_text(_details(attempt))
        else:
            failures += 1
            element = ElementTree.SubElement(
                case, "failure", message=_xml_text(attempt.evidence or verdict), type=verdict
            )
            element.text = _xml_text(_details(attempt))

    counts = {
        "tests": str(len(run.attempts)),
        "failures": str(failures),
        "errors": str(errors),
        "skipped": str(skipped),
    }
    suite.attrib.update(counts)
    suites = ElementTree.Element("testsuites", attrib={"name": "layerlens env", **counts})
    suites.append(suite)
    if hasattr(ElementTree, "indent"):  # Python 3.9+
        ElementTree.indent(suites)
    body = ElementTree.tostring(suites, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'


# What text from the API (evidence, an agent's answer, names, a judge's reasoning quoting a trace)
# could otherwise turn into on a CI job page: an image, a link, an HTML tag, emphasis, math, a code
# span or another table cell. CommonMark reads a backslash before any ASCII punctuation as "this
# character, as text", in a table cell too. GitHub also links a bare `https://…` or `www.…`:
# escaping the scheme's colon or the dot after `www` stops that (checked with cmark-gfm). It still
# links an email address, to itself (`mailto:`), whatever is escaped.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_~\[\]<>&|$!]|:(?=//)|(?<=[Ww][Ww][Ww])\.)")


def _cell(text: Optional[str], limit: Optional[int] = None) -> str:
    """``text`` on one line of Markdown, shown exactly as written; ``-`` when there is none."""
    value = " ".join((text or "").split())
    if limit is not None and len(value) > limit:
        value = value[: limit - 1] + "…"
    return _MARKDOWN_SPECIAL.sub(r"\\\1", value) or "-"


def _code(text: Optional[str], *, table: bool = False) -> str:
    """``text`` for inside a code span, which shows a backslash instead of reading it.

    A backtick would end the span, so it becomes ``'``; in a table cell a pipe would end the cell
    even inside a span, and there (only) GitHub reads ``\\|`` as a pipe.
    """
    value = " ".join((text or "").split()).replace("`", "'")
    return value.replace("|", "\\|") if table else value


def _percent(rate: Optional[float]) -> str:
    return "-" if rate is None else f"{rate * 100:.1f}%"


def _dollars(cost: Optional[float]) -> str:
    if cost is None:
        return "-"
    return "<$0.01" if 0 < cost < 0.005 else f"${cost:.2f}"


def run_to_markdown(run: RunResult, *, title: Optional[str] = None) -> str:
    heading = title or f"LayerLens: {_suite_name(run)}"
    graded = run.total - run.pending
    lines = [
        f"### {_cell(heading)} \u2014 {run.passed}/{run.out_of} passed ({_percent(run.pass_rate)})",
        "",
        "| Status | Passed | Failed | Errored | Timed out | Pending | Cost |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        f"| {_cell(run.status)} | {run.passed} | {run.failed} | {run.errored} | {run.timed_out} | {run.pending} | "
        f"{_dollars(run.cost_usd)} |",
        "",
    ]
    about = [f"run `{_code(run.run_id)}`", f"environment version {run.environment_version}"]
    if run.label:
        about.append(f"label `{_code(run.label)}`")
    if run.model:
        about.append(f"model `{_code(run.model)}`")
    lines.append(" · ".join(about))
    lines.append("")
    if run.error:
        lines.extend([f"**The run failed:** {_cell(run.error)}", ""])

    if run.uncounted:
        lines.extend([f"{run.uncounted} of {run.total} not counted: their verdict says nothing about the agent.", ""])
    not_passed = [a for a in run.attempts if not a.passed and a.counted]
    if not_passed:
        lines.extend(
            [
                f"**Not passed** ({len(not_passed)} of {run.total}{'' if graded == run.total else f', {graded} graded'})",
                "",
                "| Task | Verdict | Evidence | Trace |",
                "| --- | --- | --- | --- |",
            ]
        )
        for attempt in not_passed:
            trace = f"`{_code(attempt.trace_id, table=True)}`" if attempt.trace_id else "-"
            lines.append(
                f"| {_cell(_case_name(attempt))} | {_cell(attempt.verdict or 'pending')} | "
                f"{_cell(attempt.evidence, _MARKDOWN_EVIDENCE_CHARS)} | {trace} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def points(delta: Optional[float]) -> str:
    """A pass-rate change in percentage points: ``+12.5``, ``-16.7``, ``0.0``; ``-`` when not known."""
    if delta is None:
        return "-"
    value = round(delta * 100, 1)
    return f"{value:+.1f}" if value else "0.0"


def comparison_to_markdown(comparison: RunComparison, *, title: Optional[str] = None) -> str:
    baseline, candidate = comparison.baseline, comparison.candidate
    regressed = len(comparison.regressed)
    verdict = f"{regressed} task{'s' if regressed != 1 else ''} regressed" if regressed else "no regression"
    heading = title or f"LayerLens: {_suite_name(candidate)}"
    lines = [
        f"### {_cell(heading)} \u2014 {verdict}",
        "",
        "| | Run | Label | Passed | Pass rate | Status |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for role, run in (("Baseline", baseline), ("Candidate", candidate)):
        label = f"`{_code(run.label, table=True)}`" if run.label else "-"
        lines.append(
            f"| {role} | `{_code(run.run_id, table=True)}` | {label} | {run.passed}/{run.out_of} | "
            f"{_percent(run.pass_rate)} | {_cell(run.status)} |"
        )
    lines.extend(["", f"Pass rate {points(comparison.pass_rate_delta)} points.", ""])
    if comparison.environment_versions_differ:
        lines.extend([
            f"The runs were served different environment versions: {baseline.environment_version} "
            f"and {candidate.environment_version}.",
            "",
        ])  # fmt: skip
    if comparison.regressed:
        lines.extend([
            f"**Regressed** ({regressed}): passed on the baseline, fails on the candidate",
            "",
            "| Task | Verdict | Evidence | Trace |",
            "| --- | --- | --- | --- |",
        ])  # fmt: skip
        for change in comparison.regressed:
            now = change.candidate
            trace = f"`{_code(now.trace_id, table=True)}`" if now.trace_id else "-"
            lines.append(
                f"| {_cell(change.task_name or change.task_id)} | {_cell(now.verdict)} | "
                f"{_cell(now.evidence, _MARKDOWN_EVIDENCE_CHARS)} | {trace} |"
            )
        lines.append("")
    lines.append(_cell(tally(comparison)))
    return "\n".join(lines) + "\n"


_TALLY_NAMES = 5


def tally(comparison: RunComparison) -> str:
    """The tasks that recovered or are in one run only: how many, and the first few by name."""

    def named(names: List[str]) -> str:
        if not names:
            return "0"
        shown = ", ".join(names[:_TALLY_NAMES])
        more = f" and {len(names) - _TALLY_NAMES} more" if len(names) > _TALLY_NAMES else ""
        return f"{len(names)} ({shown}{more})"

    return (
        f"Recovered: {named([c.task_name or c.task_id for c in comparison.recovered])} · "
        f"only on the baseline: {named([a.task_name or a.task_id for a in comparison.only_in_baseline])} · "
        f"only on the candidate: {named([a.task_name or a.task_id for a in comparison.only_in_candidate])}"
    )
