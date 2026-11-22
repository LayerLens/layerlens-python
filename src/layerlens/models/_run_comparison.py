"""How a candidate run of an environment differs from a baseline run, task by task."""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from .environment import RunResult, RunAttempt


def _fails(attempt: RunAttempt) -> bool:
    """Graded with a verdict that counts, and not a pass: the rows the pass rate holds against the agent."""
    return attempt.verdict is not None and attempt.counted and not attempt.passed


def unfinished_reason(role: str, run: RunResult) -> Optional[str]:
    """Why ``run`` cannot be compared yet, or None: one still running has rows nobody graded."""
    if run.finished:
        return None
    return f"the {role} run {run.run_id} is still {run.status}; compare it once it has finished"


def task_changes(baseline: RunResult, candidate: RunResult) -> Dict[str, List[RunAttempt]]:
    """The candidate's rows that regressed and recovered, and the rows found in one run only.

    Rows are matched by task id: a task sent inline from a tasks file has an id made from
    its name, prompt and grader, so the same task in two runs has the same id, and a task
    whose grader changed is a different task.
    """
    base_rows = {a.task_id: a for a in baseline.attempts}
    cand_ids = {a.task_id for a in candidate.attempts}
    out: Dict[str, List[RunAttempt]] = {"regressed": [], "recovered": [], "only_in_candidate": []}
    for attempt in candidate.attempts:
        before = base_rows.get(attempt.task_id)
        if before is None:
            out["only_in_candidate"].append(attempt)
        elif before.passed and _fails(attempt):
            out["regressed"].append(attempt)
        elif _fails(before) and attempt.passed:
            out["recovered"].append(attempt)
    out["only_in_baseline"] = [a for a in baseline.attempts if a.task_id not in cand_ids]
    return out
