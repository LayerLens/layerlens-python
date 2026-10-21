"""Guard against silently-dead test modules.

A module-level ``pytest.importorskip("X")`` where ``X`` never exists in *any*
environment doesn't skip a test — it permanently deletes the whole module
from every run while CI stays green. That exact failure mode hid a broken
adapter for months (the pydantic_ai suite skipped itself on the fictional
``pydantic_ai.capabilities.hooks``, LAY-3567 B2) and killed the suite's only
concurrency guard the same way.

Every constant ``importorskip`` target must therefore be a known, real
package listed below. Adding a new optional test dependency is fine — add it
here consciously, so a typo'd or imaginary module can never silently bury a
test module again. (Dynamic targets, e.g. the live suite's per-case
``importorskip(case.import_name)``, are registry-driven and not checked here.)
"""

from __future__ import annotations

import os
import ast
from typing import Set, Dict, List, Tuple

import pytest

TESTS_DIR = os.path.dirname(__file__)

#: Real, installable modules the test tree may gate on.
KNOWN_SKIP_TARGETS = {
    # core / always-present (gates only on version or odd environments)
    "httpx",
    "pydantic",
    "boto3",
    "fastapi",
    "litellm",
    "ollama",
    "openai",
    "vertexai.generative_models",
    # framework adapters (installed per-venv; absent on the base env by design)
    "pydantic_ai",
    "langchain",
    "langchain.memory",
    "langchain_core",
    "langchain_openai",
    "langgraph.graph",
    "crewai",
    "crewai.events",
    # openai-agents installs as the top-level ``agents`` package.
    "agents",
    "autogen_core",
    # the autogen-ext[openai] model client the autogen floor/recorded suites drive.
    "autogen_ext.models.openai",
    "crewai.tasks.task_output",
    "semantic_kernel",
    "semantic_kernel.agents",
    "semantic_kernel.contents",
    "llama_index",
    "llama_index.core",
    "llama_index.llms.openai",
    "haystack",
    "chromadb",
    "smolagents",
    "strands",
    "agno",
    "google.adk",
    "google.genai",
    "dspy",
    "instructor",
    # instructor's retry surface — the validation-retry lane drives a REAL
    # tenacity RetryError rather than a hand-built stand-in.
    "tenacity",
    "marvin",
    # Mirascope v2's call surface. v1's ``mirascope.core`` does NOT exist on the
    # v2 line the adapter targets, so gate on what is actually imported.
    "mirascope.llm",
    "browser_use",
    # The openinference adapter ingests OTel spans; the suite builds real
    # ReadableSpans from the OTel SDK.
    "opentelemetry.sdk",
    # opentelemetry-proto: the OTLP protobuf decoder's optional dependency. The SDK
    # deliberately does NOT declare it (the openinference extra stays empty), so the
    # protobuf half of the OTLP lane skips when it is absent; the JSON half — the
    # required path — never skips.
    "opentelemetry.proto",
    # protocol / integration extras
    "copilotkit",
    "ag_ui_langgraph",
    # protocol SDKs the rewritten adapters test against (installed per-venv:
    # mcp + a2a in the sk venv; absent on base by design)
    "mcp",
    "a2a",
    # Not in uv.lock, which cannot hold it (it requires Python >= 3.11 and pins
    # pydantic and pytest), so its suites skip in the lock environment. The ap2
    # row of tests/matrix/frameworks.toml installs it and runs them.
    "ap2",
}


def _iter_test_files() -> List[str]:
    files = []
    for root, dirs, names in os.walk(TESTS_DIR):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", ".pytest_cache"}]
        for name in names:
            if name.endswith(".py"):
                files.append(os.path.join(root, name))
    return sorted(files)


def _importorskip_targets(path: str) -> List[str]:
    with open(path, encoding="utf-8") as fh:
        try:
            tree = ast.parse(fh.read(), filename=path)
        except SyntaxError:
            return []
    targets = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "importorskip" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            targets.append(arg.value)
    return targets


def test_importorskip_targets_are_known():
    offenders: Dict[str, List[str]] = {}
    for path in _iter_test_files():
        if os.path.abspath(path) == os.path.abspath(__file__):
            continue
        for target in _importorskip_targets(path):
            if target not in KNOWN_SKIP_TARGETS:
                rel = os.path.relpath(path, os.path.dirname(TESTS_DIR))
                offenders.setdefault(target, []).append(rel)

    assert not offenders, (
        "importorskip targets not in the known-module allowlist "
        "(a fictional/typo'd target permanently disables the whole test module "
        "while CI stays green): "
        + "; ".join(f"{t!r} in {', '.join(files)}" for t, files in sorted(offenders.items()))
        + ". If the module is real, add it to KNOWN_SKIP_TARGETS in tests/test_skip_hygiene.py."
    )


# ---------------------------------------------------------------------------
# An installed-but-broken dependency must fail, never skip (LAY-4242).
#
# ``pytest.importorskip`` and hand-written ``try: import x / except ImportError:
# skip`` blocks skip on ANY import failure, so a framework that is installed but
# fails to import (an API that moved, a missing dependency of its own) shows up
# as fewer tests instead of a failure. Only ``tests._optional_deps`` may decide
# that a module is absent; it skips on that alone and lets every other import
# failure through.
# ---------------------------------------------------------------------------

#: The one module allowed to call ``pytest.importorskip`` (it wraps it).
_OPTIONAL_DEPS = os.path.join(TESTS_DIR, "_optional_deps.py")

#: Handlers naming these catch an import failure wherever it comes from: an import
#: statement, or a library call that imports lazily (crewai's ``LLM()``).
_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError"}

#: Broader handlers, which swallow a broken install when they wrap an import.
_BROAD_CATCHERS = {"Exception", "BaseException"}


def _pytest_names(tree: ast.AST) -> Tuple[Set[str], Dict[str, str]]:
    """Names bound to the pytest module, and names imported from it (local -> original)."""
    modules: Set[str] = set()
    members: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.asname or alias.name for alias in node.names if alias.name == "pytest")
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            members.update({alias.asname or alias.name: alias.name for alias in node.names})
    return modules, members


def _calls_pytest(node: ast.AST, attr: str, names: Tuple[Set[str], Dict[str, str]]) -> bool:
    """True if ``node`` (or anything in it) calls ``pytest.<attr>``, however it was imported."""
    modules, members = names
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == attr:
            if isinstance(func.value, ast.Name) and func.value.id in modules:
                return True
        elif isinstance(func, ast.Name) and members.get(func.id) == attr:
            return True
    return False


def _caught_names(handler: ast.ExceptHandler) -> Set[str]:
    if handler.type is None:  # bare except
        return {"BaseException"}
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {t.id for t in types if isinstance(t, ast.Name)}


#: Calls that import a module by name.
_DYNAMIC_IMPORTS = {"import_module", "__import__"}


def _imports(node: ast.AST) -> bool:
    """True if ``node`` imports something: an import statement, or importlib.import_module / __import__."""
    for n in ast.walk(node):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            return True
        if isinstance(n, ast.Call):
            func = n.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in _DYNAMIC_IMPORTS:
                return True
    return False


def _marks_skip(handler: ast.ExceptHandler, names: Tuple[Set[str], Dict[str, str]]) -> bool:
    """True if the handler builds a skip marker (``pytestmark = pytest.mark.skip(...)``)."""
    modules, members = names
    for call in ast.walk(handler):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr not in ("skip", "skipif"):
            continue
        owner = call.func.value  # pytest.mark, or `mark` imported from pytest
        if isinstance(owner, ast.Attribute) and owner.attr == "mark" and isinstance(owner.value, ast.Name):
            if owner.value.id in modules:
                return True
        elif isinstance(owner, ast.Name) and members.get(owner.id) == "mark":
            return True
    return False


def _import_guards(tree: ast.AST) -> List[Tuple[ast.Try, ast.ExceptHandler]]:
    """Every handler that catches an import failure, paired with its ``try``.

    A handler naming ImportError/ModuleNotFoundError counts whatever its ``try``
    does; a broad one (Exception, BaseException, bare) only around an import.
    """
    guards = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        imports = any(_imports(stmt) for stmt in node.body)
        for handler in node.handlers:
            caught = _caught_names(handler)
            if caught & _IMPORT_ERRORS or (imports and caught & _BROAD_CATCHERS):
                guards.append((node, handler))
    return guards


def _sets_a_flag_to_false(handler: ast.ExceptHandler) -> bool:
    # Anywhere in the handler, including under an ``if``.
    return any(
        isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Constant)
        and node.value.value is False
        for stmt in handler.body
        for node in ast.walk(stmt)
    )


def _optional_import_offences(source: str) -> List[str]:
    """Each way ``source`` lets an installed-but-broken dependency skip tests, as "line N: why"."""
    tree = ast.parse(source)
    names = _pytest_names(tree)
    offences = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _calls_pytest(node, "importorskip", names):
            offences.append(
                f"line {node.lineno}: pytest.importorskip skips on ANY ImportError; "
                "use tests._optional_deps.importorskip"
            )
    for try_node, handler in _import_guards(tree):
        if _calls_pytest(handler, "skip", names) or _marks_skip(handler, names):
            offences.append(
                f"line {try_node.lineno}: skips when an import fails; use tests._optional_deps.importorskip "
                "(or check the installed version explicitly)"
            )
        elif _sets_a_flag_to_false(handler):
            offences.append(
                f"line {try_node.lineno}: an availability flag set by catching an import failure; "
                "use tests._optional_deps.is_installed"
            )
    return sorted(set(offences), key=lambda o: int(o.split()[1].rstrip(":")))


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('import pytest\nx = pytest.importorskip("x")\n', id="pytest.importorskip"),
        pytest.param('import pytest as pt\nx = pt.importorskip("x")\n', id="aliased-pytest"),
        pytest.param('from pytest import importorskip\nx = importorskip("x")\n', id="from-pytest-import"),
        pytest.param(
            "import pytest\ntry:\n    import x\nexcept ImportError:\n"
            '    pytest.skip("x missing", allow_module_level=True)\n',
            id="try-import-skip",
        ),
        pytest.param(
            'import pytest\ntry:\n    from x import y\nexcept (ImportError, Exception):\n    pytest.skip("x broken")\n',
            id="try-import-broad-skip",
        ),
        pytest.param(
            'from pytest import skip\ntry:\n    import x\nexcept Exception:\n    skip("x")\n',
            id="from-pytest-import-skip",
        ),
        pytest.param("try:\n    import x\n\n    HAS_X = True\nexcept ImportError:\n    HAS_X = False\n", id="flag"),
        pytest.param(
            "import sys\ntry:\n    import x\nexcept ImportError:\n    if sys.version_info >= (3, 10):\n        HAS_X = False\n",
            id="nested-flag",
        ),
        pytest.param(
            # A library call that imports lazily (crewai's LLM() builds its provider):
            # the ImportError still means an installed package is broken.
            'import pytest\ntry:\n    llm = LLM(model="openai/gpt-4o-mini")\nexcept ImportError as exc:\n'
            '    pytest.skip(f"seam unavailable: {exc}")\n',
            id="import-error-from-a-call",
        ),
        pytest.param(
            'import importlib\nimport pytest\ntry:\n    x = importlib.import_module("x")\nexcept Exception:\n'
            '    pytest.skip("x broken")\n',
            id="dynamic-import-broad-skip",
        ),
        pytest.param(
            'import pytest\ntry:\n    import x\nexcept ImportError:\n    pytestmark = pytest.mark.skip(reason="x unavailable")\n',
            id="skip-marker",
        ),
    ],
)
def test_the_optional_import_guard_recognises(source: str) -> None:
    assert _optional_import_offences(source), "the guard below would miss this pattern"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('from tests._optional_deps import importorskip\nx = importorskip("x")\n', id="the-helper"),
        pytest.param("try:\n    import tomllib\nexcept ImportError:\n    import tomli as tomllib\n", id="fallback"),
        pytest.param(
            'import pytest\ntry:\n    x = int("a")\nexcept ValueError:\n    pytest.skip("no")\n',
            id="unrelated-try",
        ),
    ],
)
def test_the_optional_import_guard_allows(source: str) -> None:
    assert _optional_import_offences(source) == []


def test_installed_but_broken_dependencies_fail_instead_of_skipping() -> None:
    offenders: List[str] = []
    for path in _iter_test_files():
        if os.path.abspath(path) in {os.path.abspath(__file__), os.path.abspath(_OPTIONAL_DEPS)}:
            continue
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        rel = os.path.relpath(path, os.path.dirname(TESTS_DIR))
        offenders.extend(f"{rel} {offence}" for offence in _optional_import_offences(source))

    assert not offenders, (
        "these skip tests when an installed dependency fails to import, so a broken install "
        "shows up as fewer tests instead of a failure:\n" + "\n".join(offenders)
    )
