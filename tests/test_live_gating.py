"""A plain test run never calls the LayerLens API, even with a key exported.

Developers export ``LAYERLENS_API_KEY`` for everyday use (the CLI and the VS
Code extension read it), so its presence must not turn ``pytest`` into traffic
against their organisation: live tests need ``-m live`` as well, and the
"without an API key" sample tests must strip every LayerLens variable, new and
legacy, before running a sample (LAY-4220).

This runs those tests in a child pytest with a key exported and the base URL
pointed at a local server that records every request it receives.
"""

from __future__ import annotations

import os
import sys
import threading
import subprocess
from typing import List
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

REPO_ROOT = Path(__file__).resolve().parents[1]


def _recording_server() -> "tuple[HTTPServer, List[str]]":
    seen: List[str] = []

    class Handler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            seen.append(f"{self.command} {self.path}")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "recorded by test_live_gating"}')

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _record

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- base signature
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_plain_pytest_run_sends_nothing_with_a_key_exported() -> None:
    server, seen = _recording_server()
    try:
        env = {
            **os.environ,
            "LAYERLENS_API_KEY": "exported-for-everyday-use",
            "LAYERLENS_BASE_URL": f"http://127.0.0.1:{server.server_port}/api/v1",
        }
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:randomly",
                "tests/test_models_custom_live.py",
                "tests/test_samples_e2e.py::TestAllSamplesLiveAPI::test_live[core/quickstart.py]",
                "tests/test_samples_e2e.py::TestWithoutAPIKey::test_no_api_key[core/quickstart.py]",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert seen == [], f"a plain pytest run called the API with the exported key: {seen}"
