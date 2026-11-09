"""Running the agent's command once per attempt, for ``layerlens env run exec``."""

from __future__ import annotations

import os
import time
import codecs
import signal
import tempfile
import threading
import subprocess
from typing import IO, Set, Dict, List, Tuple, Callable, Iterator, Optional, Sequence
from pathlib import Path
from contextlib import contextmanager
from dataclasses import dataclass

import click

from ...._env import API_KEY_ENV, LEGACY_API_KEY_ENVS
from ...._trace_tags import TRACE_TAGS_ENV, join_tags, split_tags
from ...._exit_timeout import EXIT_TIMEOUT_ENV
from ...._trace_id_file import TRACE_ID_FILE_ENV
from ....resources.environments import ExternalAttempt
from ....resources.environments._shared import MAX_ANSWER_BYTES, MAX_TRACE_ID_BYTES, trace_id_problem

# On an interrupt, how long the answers already being submitted get to finish before the run is
# cancelled. Short of a CI runner's wait between its SIGINT and its SIGTERM (7.5s on GitHub).
SUBMIT_GRACE_SECONDS = 5.0

# Never handed to the agent: an Automation key can read the expected answers.
KEY_VARIABLES = (API_KEY_ENV, *LEGACY_API_KEY_ENVS)

# How much of the end of an agent's standard error is kept, to explain a run that never got an answer.
STDERR_TAIL_BYTES = 2048

# The most of LAYERLENS_TRACE_ID_FILE that is read: far more than an id, far less than stray output.
TRACE_ID_FILE_READ_BYTES = 64 << 10

# How long an agent's exit waits for its traces to upload, when the step does not say: the SDK's
# own 5 s lost traces at ~1-2 s an upload, and with them the trace id that links the attempt.
AGENT_EXIT_TIMEOUT_SECONDS = 30.0


class LaunchError(Exception):
    """The command could not be started at all: the pipeline is broken, not the agent."""


@dataclass
class AttemptOutcome:
    # "answered", "error" or "timeout", as the submit takes it.
    outcome: str
    answer: Optional[str]
    error: Optional[str]
    seconds: float
    stderr_tail: str = ""
    # The agent's own trace, from LAYERLENS_TRACE_ID_FILE, linked when the attempt is submitted.
    trace_id: Optional[str] = None
    # Why what the file held is not linked, when it held something the API would refuse.
    trace_problem: Optional[str] = None


def agent_environment(base: Dict[str, str], *, api_key: Optional[str] = None) -> Dict[str, str]:
    """``base`` without the API key: neither its variables nor any variable whose value holds it.

    A Python agent's standard input and output are UTF-8, as the CLI writes and reads
    them, unless ``base`` sets ``PYTHONIOENCODING`` itself.
    """
    environment = {
        name: value for name, value in base.items() if name not in KEY_VARIABLES and not (api_key and api_key in value)
    }
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    return environment


def with_run_label(environment: Dict[str, str], label: Optional[str]) -> Dict[str, str]:
    """``environment`` with the run's label first in ``LAYERLENS_TRACE_TAGS``, before the tags it lists.

    LayerLens tags each attempt's own trace with the label; with it in this variable, an agent
    traced with the LayerLens SDK tags every trace it records with it too, so filtering traces
    on the label finds both. First, because a trace keeps its first 32 tags: the label is what
    finds the run's traces. The SDK ignores a value it cannot read, label included, so such a
    value is replaced by the label alone, with a warning.
    """
    label = (label or "").strip()
    if not label:
        return environment
    try:
        tags = [tag.strip() for tag in split_tags(environment.get(TRACE_TAGS_ENV, ""))]
    except ValueError as err:
        click.echo(f"Warning: {err}. The agent's traces are tagged with the run's label alone.", err=True)
        tags = []
    tags = [label, *(tag for tag in tags if tag and tag != label)]
    return {**environment, TRACE_TAGS_ENV: join_tags(tags)}


def with_exit_budget(environment: Dict[str, str], attempt_timeout: float) -> Dict[str, str]:
    """``environment`` with ``LAYERLENS_EXIT_TIMEOUT`` for the agent's SDK, unless it sets one already.

    An agent is a batch process: its traces matter more than a quick exit, and its trace is
    linked to the attempt only once its upload ends (the SDK then writes the id to
    LAYERLENS_TRACE_ID_FILE). So the agent gets ``AGENT_EXIT_TIMEOUT_SECONDS``, or half the
    attempt's time when that is shorter, leaving the attempt time to end on its own.
    """
    if environment.get(EXIT_TIMEOUT_ENV, "").strip():
        return environment
    budget = min(AGENT_EXIT_TIMEOUT_SECONDS, attempt_timeout / 2)
    return {**environment, EXIT_TIMEOUT_ENV: f"{budget:g}"}


def attempt_variables(attempt: ExternalAttempt, answer_file: Path, trace_id_file: Path) -> Dict[str, str]:
    return {
        "LAYERLENS_ENV_BASE_URL": attempt.base_url or "",
        "LAYERLENS_ATTEMPT_TOKEN": attempt.token,
        "LAYERLENS_TASK_PROMPT": attempt.prompt,
        "LAYERLENS_TASK_ID": attempt.task_id,
        "LAYERLENS_TASK_NAME": attempt.task_name,
        "LAYERLENS_ATTEMPT_ID": attempt.attempt_id,
        "LAYERLENS_ANSWER_FILE": str(answer_file),
        TRACE_ID_FILE_ENV: str(trace_id_file),
    }


class LiveProcesses:
    """The agent processes running now, and the answers being submitted, so an interrupt can
    stop every process and let no answer start on its way after it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._submits_ended = threading.Condition(self._lock)
        self._processes: Set[subprocess.Popen[bytes]] = set()
        self._submits = 0
        self.stopping = False

    def add(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.add(process)
            stopping = self.stopping
        if stopping:
            kill(process)

    def discard(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.discard(process)

    def stop_all(self) -> None:
        with self._lock:
            self.stopping = True
            processes = list(self._processes)
        for process in processes:
            kill(process)

    @contextmanager
    def submitting(self) -> Iterator[bool]:
        """Whether an attempt may submit now: ``False`` once :meth:`stop_all` has run.

        The check and the count of submits under way change under the lock :meth:`stop_all`
        takes, so a submit either starts before the stop, and :meth:`wait_for_submits` sees it,
        or not at all. Checked without it, an interrupt between the check and the submit sent
        the answer while the run was being cancelled (LAY-4454).
        """
        with self._lock:
            allowed = not self.stopping
            if allowed:
                self._submits += 1
        try:
            yield allowed
        finally:
            if allowed:
                with self._lock:
                    self._submits -= 1
                    if not self._submits:
                        self._submits_ended.notify_all()

    def submits_under_way(self) -> int:
        with self._lock:
            return self._submits

    def wait_for_submits(self, timeout: float) -> int:
        """Wait up to ``timeout`` seconds for the submits under way to end; returns how many have not."""
        with self._submits_ended:
            self._submits_ended.wait_for(lambda: not self._submits, timeout)
            return self._submits


def kill(process: subprocess.Popen[bytes]) -> None:
    """Kill the process and what it started that still runs.

    That is its whole process group, even once the process itself has exited, so a child it
    left in the background (holding its output open, or still calling tools) goes too.
    Windows has no such group, which is why ``env run exec`` refuses to run there.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            process.kill()


# How long to wait for the agent's output to close once its process group is killed.
_PIPE_GRACE_SECONDS = 5.0


class _Drain(threading.Thread):
    """Reads a pipe to its end, keeping what arrived if a leftover process holds it open.

    With ``echo``, each chunk is passed on as it arrives, and only the last ``keep`` bytes are kept.
    """

    def __init__(
        self, stream: IO[bytes], name: str, *, echo: Optional[Callable[[bytes], None]] = None, keep: int = 0
    ) -> None:
        super().__init__(name=f"layerlens-exec-{name}", daemon=True)
        self._stream = stream
        self._echo = echo
        self._keep = keep
        self._chunks: List[bytes] = []

    def run(self) -> None:
        read = getattr(self._stream, "read1", self._stream.read)
        try:
            while True:
                chunk = read(65536)
                if not chunk:
                    return
                if self._echo is None:
                    self._chunks.append(chunk)
                    continue
                self._echo(chunk)
                self._chunks = [(b"".join(self._chunks) + chunk)[-self._keep :]] if self._keep else []
        except (OSError, ValueError):
            return

    def output(self, until: float) -> bytes:
        """What arrived, once the pipe closes or ``until`` (a ``time.monotonic()``) passes."""
        self.join(max(0.0, until - time.monotonic()))
        return b"".join(list(self._chunks))


def _to_stderr(chunk: bytes) -> None:
    try:
        click.echo(chunk, err=True, nl=False)
    except (OSError, ValueError):
        pass


def _feed(stream: IO[bytes], data: bytes) -> None:
    try:
        stream.write(data)
        stream.close()
    except (OSError, ValueError):
        pass


def run_attempt(
    attempt: ExternalAttempt,
    command: Sequence[str],
    *,
    timeout_seconds: float,
    environment: Dict[str, str],
    live: LiveProcesses,
    workdir: Optional[Path] = None,
) -> AttemptOutcome:
    """Run the command for one attempt and read its answer.

    The attempt ends when the command exits, or at ``timeout_seconds``; either way its
    process group is killed then. The answer is ``LAYERLENS_ANSWER_FILE`` when the command
    wrote it, else its standard output; its standard error is passed on to the CLI's. A
    non-zero exit, no answer, or an answer over the API's limit is an ``error``; running past
    ``timeout_seconds`` is a ``timeout``. However it ends, the id the command left in
    ``LAYERLENS_TRACE_ID_FILE`` is the trace the attempt links.

    The two files sit in a directory of the attempt's own, made in ``workdir`` (the system's
    temporary directory by default) and removed as the attempt ends. A worker still finishing
    an attempt after an interrupt so never writes into a directory another thread is removing.
    """
    with tempfile.TemporaryDirectory(prefix=f"layerlens-exec-{attempt.attempt_id}-", dir=workdir) as own:
        return _run_in(Path(own), attempt, command, timeout_seconds=timeout_seconds, environment=environment, live=live)


def _run_in(
    directory: Path,
    attempt: ExternalAttempt,
    command: Sequence[str],
    *,
    timeout_seconds: float,
    environment: Dict[str, str],
    live: LiveProcesses,
) -> AttemptOutcome:
    answer_file = directory / "answer.txt"
    answer_file.write_bytes(b"")
    trace_id_file = directory / "trace-id.txt"
    trace_id_file.write_bytes(b"")
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**environment, **attempt_variables(attempt, answer_file, trace_id_file)},
            start_new_session=True,
        )
    except (OSError, ValueError) as err:
        raise LaunchError(f"could not start {command[0]!r} for task {attempt.task_name!r}: {err}") from err
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    live.add(process)
    drain = _Drain(process.stdout, "stdout")
    drain.start()
    errors = _Drain(process.stderr, "stderr", echo=_to_stderr, keep=STDERR_TAIL_BYTES)
    errors.start()
    threading.Thread(
        target=_feed, args=(process.stdin, attempt.prompt.encode("utf-8")), name="layerlens-exec-stdin", daemon=True
    ).start()
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        kill(process)
        try:
            process.wait(timeout=_PIPE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        live.discard(process)
    closed_by = time.monotonic() + _PIPE_GRACE_SECONDS
    stdout = drain.output(closed_by)
    tail = errors.output(closed_by).decode("utf-8", errors="replace")
    seconds = time.monotonic() - started
    trace_id, trace_problem = read_trace_id(trace_id_file)

    def failed(outcome: str, reason: str) -> AttemptOutcome:
        return AttemptOutcome(outcome, None, reason, seconds, tail, trace_id, trace_problem)

    if process.returncode is None:
        return failed("error", f"the agent (pid {process.pid}) could not be stopped")
    if live.stopping:
        return failed("error", "the run was interrupted")
    if timed_out:
        return failed("timeout", f"the agent ran longer than {timeout_seconds:g}s")
    if process.returncode != 0:
        return failed("error", _exit_reason(process.returncode))

    written = answer_file.read_bytes()
    answer = _text(written if written.strip() else stdout)
    if not answer.strip():
        return failed("error", "the agent exited 0 with no answer: LAYERLENS_ANSWER_FILE and its output were empty")
    size = len(answer.strip().encode("utf-8"))
    if size > MAX_ANSWER_BYTES:
        return failed("error", f"the agent's answer is {size} bytes; the limit is {MAX_ANSWER_BYTES}")
    return AttemptOutcome("answered", answer, None, seconds, tail, trace_id, trace_problem)


def read_trace_id(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """The trace id the agent left in ``path``, or why what it left there is not linked.

    Empty, or deleted, is no trace. The file holds one id, as text; a value the API would
    refuse is reported rather than sent, since a refused submit would cancel the whole run.
    """
    try:
        with path.open("rb") as f:
            data = f.read(TRACE_ID_FILE_READ_BYTES + 1)
    except FileNotFoundError:
        return None, None
    except OSError as err:
        return None, f"{TRACE_ID_FILE_ENV} could not be read: {err}"
    if len(data) > TRACE_ID_FILE_READ_BYTES:
        return None, (
            f"{TRACE_ID_FILE_ENV} holds more than {TRACE_ID_FILE_READ_BYTES >> 10} KiB; "
            f"a trace id is at most {MAX_TRACE_ID_BYTES} bytes"
        )
    try:
        trace_id = _text(data, errors="strict").strip()
    except UnicodeDecodeError:
        return None, f"{TRACE_ID_FILE_ENV} is not UTF-8 or UTF-16 text"
    if not trace_id:
        return None, None
    if not trace_id.isprintable() or " " in trace_id:
        return None, f"{TRACE_ID_FILE_ENV} holds more than one trace id, or text that is not one"
    problem = trace_id_problem(trace_id)
    if problem is not None:
        return None, f"{TRACE_ID_FILE_ENV}: {problem}"
    return trace_id, None


def _text(data: bytes, *, errors: str = "replace") -> str:
    """A file's bytes as text: UTF-8, or UTF-16 when they start with a byte order mark."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors=errors)
    return data.decode("utf-8-sig", errors=errors)


def _exit_reason(code: int) -> str:
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = str(-code)
        return f"the agent was killed by signal {name}"
    return f"the agent exited with status {code}"


def outcome_line(index: int, total: int, attempt: ExternalAttempt, outcome: AttemptOutcome) -> str:
    what = {
        "answered": "answered",
        "timeout": "timed out",
        "error": f"error: {outcome.error}",
    }.get(outcome.outcome, outcome.outcome)
    line = f"[{index}/{total}] {attempt.task_name}: {what} ({outcome.seconds:.1f}s)"
    unlinked = attempt.result.trace_id_unlinked if attempt.result is not None else None
    if unlinked:  # the API graded the answer but found no trace by the agent's id (N3)
        return f"{line}; no trace linked: {unlinked}"
    if outcome.trace_id is not None:
        return f"{line}, trace {outcome.trace_id}"
    if outcome.trace_problem is not None:
        return f"{line}; no trace linked: {outcome.trace_problem}"
    return line


__all__: List[str] = [
    "AttemptOutcome",
    "LaunchError",
    "LiveProcesses",
    "agent_environment",
    "outcome_line",
    "read_trace_id",
    "run_attempt",
]
