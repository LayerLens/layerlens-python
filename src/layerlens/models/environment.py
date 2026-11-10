"""Response and request shapes of the environments Automation API (``client.environments``).

Enumerated values (``status``, ``verdict``, ``kind``, ...) are plain strings, so a value
the API adds later parses instead of failing; the constants below name the ones known
today.
"""

from __future__ import annotations

from typing import Any, Dict, List, Union, Optional
from datetime import datetime

from pydantic import Field, BaseModel, ConfigDict

from ._run_reports import run_to_junit, run_to_markdown, comparison_to_markdown
from ._run_comparison import task_changes, unfinished_reason

__all__ = [
    "BUILD_READY",
    "BUILD_FAILED",
    "BUILD_BUILDING",
    "JOB_TERMINAL_STATUSES",
    "RUN_TERMINAL_STATUSES",
    "VERDICT_PASS",
    "UNCOUNTED_VERDICTS",
    "EnvironmentRef",
    "Environment",
    "JobRef",
    "CreatedEnvironment",
    "BuildStep",
    "EnvironmentBuild",
    "WorldConfigField",
    "WorldType",
    "Tool",
    "ToolList",
    "StateReset",
    "TaskSetSummary",
    "Grader",
    "Task",
    "TaskSet",
    "TaskSpec",
    "JobProgress",
    "Job",
    "RunAttempt",
    "RunSummary",
    "RunResult",
    "RunPage",
    "TaskChange",
    "RunComparison",
    "ExternalRunAttempt",
    "ExternalRunStart",
    "SubmitResult",
]

BUILD_BUILDING = "building"
BUILD_READY = "ready"
BUILD_FAILED = "failed"

JOB_TERMINAL_STATUSES = frozenset({"success", "failure", "cancelled"})
RUN_TERMINAL_STATUSES = JOB_TERMINAL_STATUSES

VERDICT_PASS = "pass"

# Verdicts the app never counts, neither as a pass nor against one: a play the customer's
# persona broke (off_script), and a persona preview's play (not_graded).
UNCOUNTED_VERDICTS = frozenset({"off_script", "not_graded"})


class _Model(BaseModel):
    model_config = ConfigDict(protected_namespaces=(), populate_by_name=True)


class EnvironmentRef(_Model):
    """The short form create, revert and the job routes answer with."""

    id: str
    name: str
    # "composite", the world's type, or "" for an OpenAPI spec import.
    type: str
    current_version: int


class Environment(_Model):
    """An environment as the list and detail routes show it to an API key."""

    id: str
    name: str
    # "composite", the world's type, or "spec" for an OpenAPI spec import.
    type: str
    current_version: int
    # The version actually served; 0 before the first build lands.
    environment_version: int
    tool_count: int
    created_at: datetime
    updated_at: datetime
    world_hash: str = ""
    generator_version: str = ""


class JobRef(_Model):
    id: str
    status: str


class CreatedEnvironment(_Model):
    """What ``create`` returns: the environment, the build it started, and whether it was reused."""

    environment: EnvironmentRef
    # The build: the one this request started, or with ``reused`` the one on record.
    job: Optional[JobRef] = None
    reused: bool = False
    # Set when config or seed was sent for a world an agent builds from the description,
    # which does not use them; says so in a sentence.
    unused: Optional[str] = None

    @property
    def id(self) -> str:
        return self.environment.id

    @property
    def name(self) -> str:
        return self.environment.name


class BuildStep(_Model):
    name: str
    # "running", "done" or "failed". Step names are informational and can change.
    state: str
    detail: str = ""


class EnvironmentBuild(_Model):
    """``GET /{id}/build``: the latest build of an environment."""

    # "building", "ready" or "failed"; scripts should read this, not the steps.
    status: str
    # Set only when ready.
    version: Optional[int] = None
    steps: List[BuildStep] = []
    tools: Optional[int] = None
    records: Optional[int] = None
    answer_key: Optional[int] = None
    refused_because: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self.status == BUILD_READY

    @property
    def failed(self) -> bool:
        return self.status == BUILD_FAILED


class WorldConfigField(_Model):
    """One config knob of a world type, as a JSON-schema fragment."""

    title: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    min_length: Optional[int] = Field(default=None, alias="minLength")
    max_length: Optional[int] = Field(default=None, alias="maxLength")
    default: Any = None
    enum: Optional[List[Any]] = None
    items: Optional[WorldConfigField] = None
    properties: Optional[Dict[str, WorldConfigField]] = None
    required: Optional[List[str]] = None


class WorldType(_Model):
    type: str
    title: str = ""
    description: str = ""
    config_schema: Dict[str, WorldConfigField] = {}
    tool_count: int = 0
    example_name: str = ""
    example_prompt: str = ""
    # Whether an agent builds this type's worlds from an organization slice.
    agent_slice: bool = False
    # How the type reads an organization onto its knobs; "app" names it in that organization.
    org_knobs: Optional[Dict[str, Any]] = None

    @property
    def built_by_agent(self) -> bool:
        """An agent builds these worlds from the description, as a one-world composite: config and seed are unused."""
        app = (self.org_knobs or {}).get("app")
        return self.agent_slice and isinstance(app, str) and bool(app.strip())


class Tool(_Model):
    name: str
    summary: str = ""
    input_schema: Dict[str, Any] = {}
    method: str = ""
    path: str = ""


class ToolList(_Model):
    tools: List[Tool] = []
    count: int = 0
    environment_version: int = 0
    platform_tools: List[Tool] = []


class StateReset(_Model):
    reset: bool
    deleted: bool
    blobs_deleted: int = 0
    blobs_failed: int = 0


class TaskSetSummary(_Model):
    name: str
    # Tasks that are not retired.
    size: int


class Grader(_Model):
    # "answer", "actions", "judge", "state", "ordered", "plot" or "criteria".
    kind: str
    config: Dict[str, Any] = {}


class Task(_Model):
    """A stored task, at its current version."""

    id: str
    name: str
    version: int
    prompt: str
    grader: Grader
    retired: bool = False
    created_at: datetime
    updated_at: datetime


class TaskSet(_Model):
    name: str
    # Every task in the set, retired ones included.
    total: int
    tasks: List[Task]


class TaskSpec(_Model):
    """A task sent inline with a run, as one line of a ``--tasks-file`` holds it."""

    name: str
    prompt: str
    # Any kind but "plot" and "criteria": an inline task has no plot and no persona.
    grader: Grader


class JobProgress(_Model):
    completed: int
    total: int


class Job(_Model):
    """``GET /{id}/jobs/{job_id}``: a build, a task generation or another background job."""

    id: str
    kind: str
    # "pending", "in_progress", "success", "failure" or "cancelled".
    status: str
    stop_reason: Optional[str] = None
    created_at: datetime
    finished_at: Optional[datetime] = None
    progress: Optional[JobProgress] = None
    # None when the job's cost is not known.
    cost_usd: Optional[float] = None
    # Why the job failed; set only when ``status`` is "failure".
    error: Optional[str] = None
    # A build: version and counts; a build_setup (an agent-built world) has no version, which
    # build_status() reads instead. A generation: set_name, target, written, dropped, unprobed.
    result: Optional[Dict[str, Any]] = None

    @property
    def finished(self) -> bool:
        return self.status in JOB_TERMINAL_STATUSES


class RunAttempt(_Model):
    """One task of a run. ``verdict`` is None while the task is still to be graded."""

    attempt_id: Optional[str] = None
    task_id: str
    task_name: Optional[str] = None
    task_version: Optional[int] = None
    # "pass", "fail", "error" or "timeout"; or one of UNCOUNTED_VERDICTS.
    verdict: Optional[str] = None
    grader: Optional[str] = None
    evidence: Optional[str] = None
    # "agent" or "platform", for "error" and "timeout" verdicts.
    error_source: Optional[str] = None
    # Clipped to 4 KB; see ``agent_answer_truncated``.
    agent_answer: Optional[str] = None
    agent_answer_truncated: bool = False
    cost_usd: Optional[float] = None
    # LayerLens's trace of the attempt.
    trace_id: Optional[str] = None
    # The trace the agent itself sent, if it passed one on submit.
    agent_trace_id: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.verdict == VERDICT_PASS

    @property
    def counted(self) -> bool:
        """False for a verdict that says nothing about the agent (one of UNCOUNTED_VERDICTS)."""
        return self.verdict not in UNCOUNTED_VERDICTS


class RunSummary(_Model):
    """A run as ``runs.list`` lists it: a :class:`RunResult` without its per-task rows."""

    run_id: str
    # "external" (your agent) or "rollout" (a model or endpoint).
    kind: str
    # "pending", "in_progress", "success", "failure" or "cancelled".
    status: str
    stop_reason: Optional[str] = None
    # Why the run failed; set only when ``status`` is "failure".
    error: Optional[str] = None
    environment_version: int = 0
    agent_name: Optional[str] = None
    label: Optional[str] = None
    model: str = ""
    set_name: Optional[str] = None
    total: int
    passed: int
    failed: int
    errored: int
    timed_out: int
    pending: int
    # The rows pass_rate is over: graded with a verdict that counts, leaving out plays a
    # cancel cut short and tasks a cancel or the budget stopped before they ran. None from
    # a server that predates it.
    counted: Optional[int] = None
    # passed / counted; None until a counted row is graded.
    pass_rate: Optional[float] = None
    # None when the run's cost is not known (an endpoint, own: or custom: model).
    cost_usd: Optional[float] = None
    # When the run was started, began and ended; None from a server that predates them.
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @property
    def finished(self) -> bool:
        return self.status in RUN_TERMINAL_STATUSES

    @property
    def out_of(self) -> int:
        """What ``passed`` is out of: ``counted``, or ``total`` from a server that does not send it."""
        return self.total if self.counted is None else self.counted


class RunResult(RunSummary):
    """``GET /{id}/runs/{run_id}``: a run of either kind, with one row per task."""

    attempts: List[RunAttempt] = []

    @property
    def uncounted(self) -> int:
        """Attempts with a verdict that says nothing about the agent, such as ``off_script``."""
        return sum(1 for a in self.attempts if not a.counted)

    def failures(self) -> List[RunAttempt]:
        """The graded attempts that did not pass, leaving out those not counted."""
        return [a for a in self.attempts if a.verdict is not None and not a.passed and a.counted]

    def to_junit(self, path: Union[str, None] = None, *, suite_name: Optional[str] = None) -> str:
        """The run as JUnit XML, one test case per task; also written to ``path`` when given."""
        xml = run_to_junit(self, suite_name=suite_name)
        if path is not None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml)
        return xml

    def to_markdown(self, *, title: Optional[str] = None) -> str:
        """A Markdown summary for a CI job page: the counts, then every task that did not pass."""
        return run_to_markdown(self, title=title)


class RunPage(_Model):
    """``GET /{id}/runs``: one page of an environment's runs, newest first."""

    runs: List[RunSummary] = []
    # Pass it back as ``cursor`` for the next page; None on the last page.
    next_cursor: Optional[str] = None


class TaskChange(_Model):
    """One task whose verdict changed between two runs, with its row in each."""

    task_id: str
    task_name: Optional[str] = None
    baseline: RunAttempt
    candidate: RunAttempt


class RunComparison(_Model):
    """How a candidate run differs from a baseline run: :meth:`between` builds it.

    A regression is a task that passed on the baseline and fails on the candidate: graded
    ``fail``, ``error`` or ``timeout``, the verdicts the pass rate counts against the agent.
    Tasks in only one run are listed, and never a regression: a task added to the file, or
    one whose prompt or grader changed (which gives it a new id), says nothing about whether
    the agent got worse at the tasks both runs share.
    """

    baseline: RunSummary
    candidate: RunSummary
    # candidate.pass_rate - baseline.pass_rate; None when either run has no pass rate.
    pass_rate_delta: Optional[float] = None
    regressed: List[TaskChange] = []
    recovered: List[TaskChange] = []
    only_in_baseline: List[RunAttempt] = []
    only_in_candidate: List[RunAttempt] = []
    # True when any task regressed.
    regression: bool = False

    @classmethod
    def between(cls, baseline: RunResult, candidate: RunResult) -> RunComparison:
        """Compare two finished runs. Raises :class:`ValueError` for a run still in progress."""
        for role, run in (("baseline", baseline), ("candidate", candidate)):
            why = unfinished_reason(role, run)
            if why is not None:
                raise ValueError(why)
        changes = task_changes(baseline, candidate)
        before = {a.task_id: a for a in baseline.attempts}

        def changed(rows: List[RunAttempt]) -> List[TaskChange]:
            return [
                TaskChange(
                    task_id=a.task_id,
                    task_name=a.task_name or before[a.task_id].task_name,
                    baseline=before[a.task_id],
                    candidate=a,
                )  # fmt: skip
                for a in rows
            ]

        delta = None
        if baseline.pass_rate is not None and candidate.pass_rate is not None:
            delta = candidate.pass_rate - baseline.pass_rate
        regressed = changed(changes["regressed"])
        return cls(
            baseline=_summary_of(baseline),
            candidate=_summary_of(candidate),
            pass_rate_delta=delta,
            regressed=regressed,
            recovered=changed(changes["recovered"]),
            only_in_baseline=changes["only_in_baseline"],
            only_in_candidate=changes["only_in_candidate"],
            regression=bool(regressed),
        )

    @property
    def environment_versions_differ(self) -> bool:
        """The runs were served different versions of the environment."""
        return self.baseline.environment_version != self.candidate.environment_version

    def to_markdown(self, *, title: Optional[str] = None) -> str:
        """A Markdown summary for a CI job page: the pass rates, then every task that regressed."""
        return comparison_to_markdown(self, title=title)


def _summary_of(run: RunResult) -> RunSummary:
    return RunSummary.model_validate(run.model_dump(exclude={"attempts"}))


class ExternalRunAttempt(_Model):
    """One attempt of a run of your own agent, as its start hands it out."""

    attempt_id: str
    task_id: str
    task_name: str
    prompt: str
    # The attempt's own address: GET {base_url}/tools and POST {base_url}/call.
    base_url: Optional[str] = None
    # The attempt's credential for base_url; it reaches this attempt's tools and nothing else.
    token: str = Field(repr=False)
    # The Agent API's /call for this run and task, for an agent that holds a Standard key.
    call_url: Optional[str] = None
    expires_at: datetime


class ExternalRunStart(_Model):
    run_id: str
    attempts: List[ExternalRunAttempt]


class SubmitResult(_Model):
    run_id: str
    task_id: str
    attempt_id: str
    # The run's graded attempts so far, this one included.
    answered: int
    total: int
    # The run finished with this submit (its last attempt was graded).
    finished: bool
    # Set when the trace id sent with it names no trace the API links (a trace's id in the
    # organization, or the SDK or OpenTelemetry trace id of one in the project): the answer was graded,
    # and the attempt is not linked to that trace. The API's sentence saying so.
    trace_id_unlinked: Optional[str] = None
