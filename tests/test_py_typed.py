"""The SDK is typed for its users' type checkers: the package carries a PEP 561 ``py.typed`` marker.

Without it mypy refuses to read an installed package's annotations: ``error: Skipping analyzing
"layerlens": module is installed, but missing library stubs or py.typed marker [import-untyped]``,
and every SDK call is ``Any`` (docs E2E 2026-11-10, L1-153). The wheel's contents are checked by the
``test-build`` CI job; this runs mypy on a user's file against the package as installed here.
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import layerlens

USER_CODE = """\
from layerlens import LayerLens

client = LayerLens(api_key="k")
reveal_type(client.evaluations.get_by_id("x"))
"""


def test_the_package_has_a_py_typed_marker() -> None:
    assert (Path(layerlens.__file__).parent / "py.typed").is_file()


def test_mypy_reads_the_sdks_types_in_a_users_code(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(USER_CODE)
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", "--cache-dir", str(tmp_path / ".mypy_cache"), "app.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=600,
    )
    out = result.stdout + result.stderr
    assert "import-untyped" not in out and "py.typed" not in out, out
    assert 'Revealed type is "Union[layerlens.models.evaluation.Evaluation, None]"' in out or (
        'Revealed type is "layerlens.models.evaluation.Evaluation | None"' in out
    ), out
