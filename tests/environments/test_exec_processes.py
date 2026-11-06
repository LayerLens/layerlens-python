"""``run_attempt`` with real processes: what ends an attempt, and what it leaves running."""

from __future__ import annotations

import os
import sys
import time
import codecs
from pathlib import Path

import pytest

from layerlens.models import ExternalRunAttempt
from layerlens.cli.commands.env._exec import (
    STDERR_TAIL_BYTES,
    LaunchError,
    LiveProcesses,
    run_attempt,
    agent_environment,
)
from layerlens.resources.environments import ExternalAttempt

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")


def _attempt(prompt: str = "the task") -> ExternalAttempt:
    data = ExternalRunAttempt(
        attempt_id="6a91c0e5f1d2a3b4c5d6e801",
        task_id="6a8f2c1d9e0b4a7f3c5d6e11",
        task_name="t",
        prompt=prompt,
        base_url="https://api.test/api/v1/attempts/6a91c0e5f1d2a3b4c5d6e801",
        token="llat_v1_x",
        expires_at="2026-10-27T09:14:22Z",
    )
    return ExternalAttempt(None, data)  # type: ignore[arg-type]


def _run(tmp_path: Path, script: str, timeout: float = 20.0, prompt: str = "the task"):  # type: ignore[no-untyped-def]
    return run_attempt(
        _attempt(prompt),
        [sys.executable, "-c", script],
        timeout_seconds=timeout,
        environment=dict(os.environ),
        workdir=tmp_path,
        live=LiveProcesses(),
    )


def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


LEAVES_A_CHILD = r"""
import os, subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # inherits stdout
open(os.environ["PID_FILE"], "w").write(str(child.pid))
print("the answer", flush=True)
"""


@posix_only
def test_the_attempt_ends_when_the_command_exits_and_takes_its_leftovers_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PID_FILE", str(tmp_path / "child.pid"))
    started = time.monotonic()
    outcome = _run(tmp_path, LEAVES_A_CHILD)
    assert time.monotonic() - started < 10
    assert outcome.outcome == "answered" and outcome.answer == "the answer\n"
    assert _gone(int((tmp_path / "child.pid").read_text()))


@posix_only
def test_a_timeout_with_a_child_holding_the_output_does_not_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PID_FILE", str(tmp_path / "child.pid"))
    started = time.monotonic()
    outcome = _run(tmp_path, LEAVES_A_CHILD + "import time; time.sleep(60)\n", timeout=1.0)
    assert time.monotonic() - started < 10
    assert outcome.outcome == "timeout"
    assert _gone(int((tmp_path / "child.pid").read_text()))


def test_a_prompt_larger_than_a_pipe_buffer_reaches_an_agent_that_never_reads_it(tmp_path: Path) -> None:
    # The API's largest prompt: 20,000 characters, here 4 bytes each, past a 64 KiB pipe
    # buffer and within Linux's 128 KiB limit on one environment variable.
    outcome = _run(tmp_path, "print('done')", prompt="\U0001f600" * 20_000)
    assert outcome.outcome == "answered" and outcome.answer == "done\n"


def test_a_utf16_answer_file_with_a_byte_order_mark_is_read_as_text(tmp_path: Path) -> None:
    script = "import os; open(os.environ['LAYERLENS_ANSWER_FILE'], 'wb').write(%r)" % (
        codecs.BOM_UTF16_LE + "Zürich".encode("utf-16-le")
    )
    outcome = _run(tmp_path, script)
    assert outcome.answer == "Zürich"


def test_a_task_an_environment_variable_cannot_carry_is_a_launch_error(tmp_path: Path) -> None:
    with pytest.raises(LaunchError, match="for task 't'"):
        _run(tmp_path, "pass", prompt="a \x00 b")


def test_an_answer_at_the_limit_with_a_trailing_newline_is_an_answer(tmp_path: Path) -> None:
    outcome = _run(tmp_path, "print('x' * 65536)")
    assert outcome.outcome == "answered"


@posix_only
def test_a_python_agent_reads_and_writes_utf8_whatever_the_locale(tmp_path: Path) -> None:
    # An ASCII locale with UTF-8 mode off stands in for Windows' cp1252: Python would decode
    # the prompt, and encode the answer, in the locale's encoding.
    ascii_locale = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}
    script = "import sys; p = sys.stdin.read(); print('ok' if p == 'Z\\u00fcrich' else 'mangled', p.upper())"
    outcome = run_attempt(
        _attempt("Zürich"),
        [sys.executable, "-c", script],
        timeout_seconds=20,
        environment=agent_environment(ascii_locale),
        workdir=tmp_path,
        live=LiveProcesses(),
    )
    assert outcome.answer == "ok ZÜRICH\n", outcome


def test_the_agents_own_encoding_setting_is_kept() -> None:
    assert agent_environment({"PYTHONIOENCODING": "latin-1"})["PYTHONIOENCODING"] == "latin-1"


def test_the_key_is_kept_out_of_every_variable_that_holds_it() -> None:
    base = {
        "PATH": "/usr/bin",
        "LAYERLENS_API_KEY": "llk_SECRET",
        "LAYERLENS_STRATIX_API_KEY": "llk_OTHER",
        "CI_LAYERLENS_KEY": "llk_SECRET",
        "DOCKER_ARGS": "-e KEY=llk_SECRET --rm",
    }
    assert agent_environment(base, api_key="llk_SECRET") == {"PATH": "/usr/bin", "PYTHONIOENCODING": "utf-8"}


def test_the_agents_standard_error_is_passed_on_and_its_end_kept(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    script = "import sys; sys.stderr.write('x' * 5000 + '\\nTraceback: the last line\\n'); sys.exit(2)"
    outcome = _run(tmp_path, script)
    assert outcome.outcome == "error" and outcome.error == "the agent exited with status 2"
    assert outcome.stderr_tail.endswith("Traceback: the last line\n")
    assert len(outcome.stderr_tail.encode()) == STDERR_TAIL_BYTES
    assert "Traceback: the last line" in capfd.readouterr().err


# LAY-4430: the agent's own trace, linked to the attempt.
SERVER_ID = "6a91c0f2a7b8c9d0e1f2a3b4"
WRITES_TRACE_ID = "import os; open(os.environ['LAYERLENS_TRACE_ID_FILE'], 'w').write(%r)\n"


def test_the_trace_id_file_sits_beside_the_answer_file_and_starts_empty(tmp_path: Path) -> None:
    script = (
        "import os, sys; t = os.environ['LAYERLENS_TRACE_ID_FILE']; a = os.environ['LAYERLENS_ANSWER_FILE']\n"
        "print(os.path.dirname(t) == os.path.dirname(a), os.path.getsize(t))\n"
    )
    outcome = _run(tmp_path, script)
    assert outcome.answer == "True 0\n"
    assert outcome.trace_id is None and outcome.trace_problem is None


@pytest.mark.parametrize(
    "ending, outcome_name",
    [("print('the answer')", "answered"), ("raise SystemExit(3)", "error"), ("import time; time.sleep(30)", "timeout")],
)
def test_the_trace_id_is_read_however_the_attempt_ends(tmp_path: Path, ending: str, outcome_name: str) -> None:
    outcome = _run(tmp_path, WRITES_TRACE_ID % SERVER_ID + ending, timeout=2.0)
    assert outcome.outcome == outcome_name
    assert outcome.trace_id == SERVER_ID and outcome.trace_problem is None


def test_the_trace_id_is_trimmed(tmp_path: Path) -> None:
    outcome = _run(tmp_path, WRITES_TRACE_ID % f"  {SERVER_ID}\n" + "print('a')")
    assert outcome.trace_id == SERVER_ID


def test_a_utf16_trace_id_file_with_a_byte_order_mark_is_read_as_text(tmp_path: Path) -> None:
    script = "import os; open(os.environ['LAYERLENS_TRACE_ID_FILE'], 'wb').write(%r); print('a')" % (
        codecs.BOM_UTF16_LE + SERVER_ID.encode("utf-16-le")
    )
    assert _run(tmp_path, script).trace_id == SERVER_ID


def test_a_deleted_trace_id_file_is_no_trace(tmp_path: Path) -> None:
    outcome = _run(tmp_path, "import os; os.remove(os.environ['LAYERLENS_TRACE_ID_FILE']); print('a')")
    assert outcome.outcome == "answered"
    assert outcome.trace_id is None and outcome.trace_problem is None


@pytest.mark.parametrize(
    "written, problem",
    [
        (f"{SERVER_ID}\n6a91c0f2a7b8c9d0e1f2a3b5\n", "holds more than one trace id"),
        ("not one id", "holds more than one trace id"),
        ("é" * 128 + "x", "trace_id is 257 bytes; the limit is 256"),
    ],
)
def test_a_trace_id_the_api_would_refuse_is_not_linked_and_says_why(tmp_path: Path, written: str, problem: str) -> None:
    outcome = _run(tmp_path, WRITES_TRACE_ID % written + "print('the answer')")
    assert outcome.outcome == "answered" and outcome.answer == "the answer\n"
    assert outcome.trace_id is None
    assert outcome.trace_problem is not None and problem in outcome.trace_problem
    assert "LAYERLENS_TRACE_ID_FILE" in outcome.trace_problem


SDK_AGENT = r"""
from layerlens.models import CreateTracesResponse
from layerlens.instrument._collector import TraceCollector
from layerlens.instrument._capture_config import CaptureConfig

class Traces:
    def upload(self, path):
        return CreateTracesResponse(trace_ids=[%r])

class Client:
    traces = Traces()

collector = TraceCollector(Client(), CaptureConfig())
collector.emit("model.invoke", {"model": "test-model", "latency_ms": 1}, span_id="s00001")
collector.flush()  # queued for the background uploader, which drains as the process exits
print("the answer")
"""


def test_an_agent_traced_with_the_sdk_links_its_trace_with_no_code(tmp_path: Path) -> None:
    outcome = _run(tmp_path, SDK_AGENT % SERVER_ID)
    assert outcome.outcome == "answered" and outcome.answer == "the answer\n", outcome.stderr_tail
    assert outcome.trace_id == SERVER_ID


def test_an_attempt_removes_its_own_files(tmp_path: Path) -> None:
    # Each attempt owns its files: a worker still finishing an attempt after Ctrl-C must not
    # race the run's cleanup of a directory the two share.
    outcome = _run(tmp_path, WRITES_TRACE_ID % SERVER_ID + "print('the answer')")
    assert outcome.trace_id == SERVER_ID
    assert list(tmp_path.iterdir()) == []


def test_a_trace_id_file_that_is_not_text_is_not_linked(tmp_path: Path) -> None:
    script = "import os; open(os.environ['LAYERLENS_TRACE_ID_FILE'], 'wb').write(b'6a91c0f2\\xff\\xfe7b8c'); print('a')"
    outcome = _run(tmp_path, script)
    assert outcome.outcome == "answered" and outcome.trace_id is None
    assert outcome.trace_problem is not None and "not UTF-8" in outcome.trace_problem


def test_a_huge_trace_id_file_is_refused_without_reading_it_all(tmp_path: Path) -> None:
    script = "import os; open(os.environ['LAYERLENS_TRACE_ID_FILE'], 'w').write('x' * (8 << 20)); print('a')"
    outcome = _run(tmp_path, script)
    assert outcome.trace_id is None
    assert outcome.trace_problem is not None and "more than" in outcome.trace_problem
