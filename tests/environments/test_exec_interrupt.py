"""M13: ``env run exec`` and an interrupt a CI runner sends.

GitHub cancels a step with SIGINT, then SIGTERM 7.5 s later, then kills it. Two things went wrong:

- A step that runs ``layerlens ... &`` (to trap and forward the signal) starts it with SIGINT
  ignored, as a non-interactive shell starts every background job, and Python then never raises
  ``KeyboardInterrupt``: the forwarded ``kill -INT`` did nothing, and the run was left open. exec
  now takes SIGINT as it takes SIGTERM, whatever it was before.
- After the interrupt exec cancelled the run within its 5 s grace, then waited at exit for the
  thread pool's workers still inside a submit the API had not answered; the runner's SIGTERM at
  7.5 s ended it with 143, not 11 (2 of 3 live runs). The attempts now run on daemon threads, so
  exec exits once the run is cancelled.
"""

from __future__ import annotations

import sys
import time
import signal
import textwrap
import threading
import subprocess
from typing import Any, Dict, Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from layerlens.cli._app import cli
from layerlens.cli.commands.env._common import interrupts_held_off, signals_as_interrupt

from . import test_cli
from .conftest import ENV_ID, RUN_ID, FakeAPI, ok, submitted, make_client

A, KEY_ENV, FINAL = test_cli.A, test_cli.KEY_ENV, test_cli.TestExec.FINAL

ROOT = Path(__file__).resolve().parents[2]
SUBMIT = f"/{ENV_ID}/external-runs/{RUN_ID}/attempts/{A[0]}/submit"
CANCEL = f"/{ENV_ID}/runs/{RUN_ID}/cancel"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="signals sent to this process are POSIX")


@pytest.fixture(autouse=True)
def _client_on_the_fake_api(api: FakeAPI) -> Iterator[None]:
    with patch("layerlens.cli.commands.env._common.build_client", lambda ctx, key, use_bearer_auth: make_client(api)):
        yield


@pytest.fixture
def sigint_ignored() -> Iterator[None]:
    # As a non-interactive shell starts a background job: `layerlens ... &`.
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.parametrize("helper", [signals_as_interrupt, interrupts_held_off], ids=lambda helper: helper.__name__)
def test_a_handler_installed_outside_python_is_not_put_back(helper: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # signal.signal() returns None for a handler installed from C (by an app that embeds Python,
    # say), and signal.signal(number, None) raises TypeError: leaving the block raised it.
    installed: Dict[int, Any] = {}

    def from_c(number: int, handler: Any) -> Any:
        if handler is None:
            raise TypeError("signal handler must be signal.SIG_IGN, signal.SIG_DFL, or a callable object")
        previous, installed[number] = installed.get(number), handler
        return previous

    monkeypatch.setattr(signal, "signal", from_c)
    with helper():
        pass
    assert set(installed) == {signal.SIGINT, signal.SIGTERM}  # taken in the block, and left as they were


@posix_only
def test_a_sigint_cancels_the_run_when_exec_started_with_sigint_ignored(
    runner: CliRunner, api: FakeAPI, sigint_ignored: None
) -> None:
    def interrupted(request: httpx.Request) -> Any:  # noqa: ARG001
        # To the main thread itself, as a process-wide signal may land on this worker thread.
        signal.pthread_kill(threading.main_thread().ident or 0, signal.SIGINT)
        return ok(submitted(A[0], total=4))

    test_cli._exec_api(api, FINAL)
    api.routes[("POST", SUBMIT)] = [interrupted]
    result = runner.invoke(
        cli, ["-q", "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--",
              sys.executable, "-c", "print('x')"], env=KEY_ENV,
    )  # fmt: skip
    assert result.exit_code == 11, result.stderr
    assert f"interrupted, so run {RUN_ID} was cancelled" in result.stderr
    assert len(api.calls("POST", CANCEL)) == 1
    assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN  # put back as it was


_HELD_SUBMIT = """
import os, sys, signal, threading
from unittest.mock import patch

sys.path.insert(0, {root!r})
import layerlens.cli.commands.env.runs as runs
from layerlens.cli._app import cli
from tests.environments.conftest import ENV_ID, RUN_ID, FakeAPI, ok, submitted, make_client
from tests.environments.test_cli import A, TestExec, _exec_api

runs.SUBMIT_GRACE_SECONDS = 0.2
api = FakeAPI()
_exec_api(api, TestExec.FINAL)

def held_by_the_api(request):
    signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
    threading.Event().wait({held})  # the API holds this submit, grading it
    return ok(submitted(A[0], total=4))

api.routes[("POST", f"/{{ENV_ID}}/external-runs/{{RUN_ID}}/attempts/{{A[0]}}/submit")] = [held_by_the_api]
os.environ["LAYERLENS_API_KEY"] = "test-key"
with patch("layerlens.cli.commands.env._common.build_client", lambda ctx, key, use_bearer_auth: make_client(api)):
    cli.main(["-q", "env", "run", "exec", ENV_ID, "--set", "ci", "--agent-name", "a", "--",
              sys.executable, "-c", "print('x')"])
"""


@posix_only
def test_exec_exits_11_once_the_run_is_cancelled_while_a_submit_is_still_held(tmp_path: Path) -> None:
    held = 40.0
    script = tmp_path / "exec_interrupted.py"
    script.write_text(textwrap.dedent(_HELD_SUBMIT.format(root=str(ROOT), held=held)))
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=held + 30, cwd=str(tmp_path)
    )
    took = time.monotonic() - started
    assert done.returncode == 11, done.stderr
    assert f"interrupted, so run {RUN_ID} was cancelled" in done.stderr
    # It did not wait for the held submit: before, it exited only when the API answered it.
    assert took < held / 2, f"exec took {took:.1f}s to exit"
