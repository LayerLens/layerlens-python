"""Running the agent's command once per attempt, for ``layerlens env run exec``."""

from __future__ import annotations

import os
import time
import codecs
import signal
import threading
import subprocess
from typing import IO, Set, Dict, List, Callable, Optional, Sequence
from pathlib import Path
from dataclasses import dataclass

import click

from ...._env import API_KEY_ENV, LEGACY_API_KEY_ENVS
from ....resources.environments import ExternalAttempt

# The API limits a submitted answer, and the reason given with an error, to this many bytes.
MAX_ANSWER_BYTES = 65536

# Never handed to the agent: an Automation key can read the expected answers.
KEY_VARIABLES = (API_KEY_ENV, *LEGACY_API_KEY_ENVS)

# How much of the end of an agent's standard error is kept, to explain a run that never got an answer.
STDERR_TAIL_BYTES = 2048


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


def attempt_variables(attempt: ExternalAttempt, answer_file: Path) -> Dict[str, str]:
    return {
        "LAYERLENS_ENV_BASE_URL": attempt.base_url or "",
        "LAYERLENS_ATTEMPT_TOKEN": attempt.token,
        "LAYERLENS_TASK_PROMPT": attempt.prompt,
        "LAYERLENS_TASK_ID": attempt.task_id,
        "LAYERLENS_TASK_NAME": attempt.task_name,
        "LAYERLENS_ATTEMPT_ID": attempt.attempt_id,
        "LAYERLENS_ANSWER_FILE": str(answer_file),
    }


class LiveProcesses:
    """The agent processes running now, so an interrupt can stop every one of them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: Set[subprocess.Popen[bytes]] = set()
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
    workdir: Path,
    live: LiveProcesses,
) -> AttemptOutcome:
    """Run the command for one attempt and read its answer.

    The attempt ends when the command exits, or at ``timeout_seconds``; either way its
    process group is killed then. The answer is ``LAYERLENS_ANSWER_FILE`` when the command
    wrote it, else its standard output; its standard error is passed on to the CLI's. A
    non-zero exit, no answer, or an answer over the API's limit is an ``error``; running past
    ``timeout_seconds`` is a ``timeout``.
    """
    answer_file = workdir / f"answer-{attempt.attempt_id}.txt"
    answer_file.write_bytes(b"")
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**environment, **attempt_variables(attempt, answer_file)},
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

    def failed(outcome: str, reason: str) -> AttemptOutcome:
        return AttemptOutcome(outcome, None, reason, seconds, tail)

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
    return AttemptOutcome("answered", answer, None, seconds, tail)


def _text(data: bytes) -> str:
    """An answer's bytes as text: UTF-8, or UTF-16 when it starts with a byte order mark."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


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
    return f"[{index}/{total}] {attempt.task_name}: {what} ({outcome.seconds:.1f}s)"


__all__: List[str] = [
    "AttemptOutcome",
    "LaunchError",
    "LiveProcesses",
    "agent_environment",
    "outcome_line",
    "run_attempt",
]
