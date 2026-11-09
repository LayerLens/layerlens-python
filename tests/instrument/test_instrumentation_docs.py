"""Every instrumentation example in the docs and samples uses the API that exists (LAY-4454).

The reference documented ``TraceRecorder`` and ``SpanData``, and the guides showed
``span(name, kind=...)`` yielding an object to set ``.output`` on, and
``provider.connect_client(...)``: an API removed from ``src/`` long ago. A reader who
copied one got a ``TypeError`` or an ``AttributeError``, and the sample that did the same
was never run, since it needs an OpenAI key. These tests tie the docs to the code, as
``test_public_errors.py`` does for the error classes:

* every name a code block imports from ``layerlens.instrument`` exists;
* every call of one binds to its real signature, and every method called on what one
  returns exists;
* ``span()`` is used for the id it yields: nothing is set on it;
* every function and class the reference documents under a heading exists, with the
  parameters the heading gives it.
"""

from __future__ import annotations

import re
import ast
import shutil
import typing
import inspect
import importlib
import subprocess
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = "docs/api-reference/instrumentation.md"
_FENCE = re.compile(r"^```python\n(.*?)^```", re.M | re.S)
_HEADING = re.compile(r"^#{2,4} `([A-Za-z_][\w.]*)(\(.*\))?`\s*$", re.M)


GIT_CHECKOUT = shutil.which("git") is not None and (ROOT / ".git").exists()


def _tracked(*globs: str) -> List[Path]:
    """The tracked files matching ``globs``; none outside a git checkout, where the tests skip."""
    if not GIT_CHECKOUT:
        return []
    out = subprocess.run(["git", "ls-files", "--", *globs], cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / line for line in out.stdout.splitlines()]


def _page_imports(blocks: List[str]) -> str:
    """The ``layerlens.instrument`` imports of a page's blocks: a block often leaves out the
    imports an earlier block made, and is read with them."""
    lines: List[str] = []
    for code in blocks:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        lines += [
            ast.unparse(node)
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("layerlens.instrument")
        ]
    return "\n".join(dict.fromkeys(lines))


def _examples() -> List[Tuple[str, str]]:
    """(where, code) for every code block or sample that uses ``layerlens.instrument``."""
    found: List[Tuple[str, str]] = []
    for path in _tracked("docs/*.md", "docs/**/*.md", "README.md"):
        text = path.read_text(encoding="utf-8")
        matches = list(_FENCE.finditer(text))
        imports = _page_imports([m.group(1) for m in matches])
        if not imports:
            continue
        for match in matches:
            line = text[: match.start()].count("\n") + 2
            # The page's imports go last, so a problem's line number is still the block's own.
            found.append((f"{path.relative_to(ROOT)}:{line}", f"{match.group(1)}\n{imports}\n"))
    for path in _tracked("samples/*.py", "samples/**/*.py"):
        code = path.read_text(encoding="utf-8")
        if "layerlens.instrument" in code:
            found.append((str(path.relative_to(ROOT)), code))
    return found


EXAMPLES = _examples()


def _resolve(module: str, name: str) -> Tuple[bool, Any]:
    """(known, object): ``known`` is False when the module cannot be imported here."""
    try:
        mod = importlib.import_module(module)
    except ImportError:
        return False, None
    return True, getattr(mod, name, _MISSING)


_MISSING = object()


def _returned_class(obj: Any) -> Optional[type]:
    if inspect.isclass(obj):
        return obj
    try:
        hint = typing.get_type_hints(obj).get("return")
    except Exception:
        return None
    return hint if inspect.isclass(hint) else None


def _problems(where: str, code: str) -> List[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as err:
        return [f"{where}: not valid Python ({err.msg}, line {err.lineno})"]
    problems: List[str] = []
    imported: Dict[str, Any] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("layerlens.instrument"):
            for alias in node.names:
                known, obj = _resolve(node.module, alias.name)
                if not known:
                    continue
                if obj is _MISSING:
                    problems.append(f"{where}: {node.module} has no {alias.name}")
                else:
                    imported[alias.asname or alias.name] = obj

    instances: Dict[str, type] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in imported
        ):
            cls = _returned_class(imported[node.value.func.id])
            for target in node.targets:
                if cls is not None and isinstance(target, ast.Name):
                    instances[target.id] = cls

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in imported and callable(imported[func.id]):
            if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                continue
            try:
                inspect.signature(imported[func.id]).bind(
                    *[None] * len(node.args), **{k.arg: None for k in node.keywords if k.arg}
                )
            except TypeError as err:
                problems.append(f"{where}:{node.lineno}: {ast.unparse(node)} does not fit {func.id}: {err}")
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in instances:
            cls = instances[func.value.id]
            if not hasattr(cls, func.attr):
                problems.append(f"{where}:{node.lineno}: {cls.__name__} has no method {func.attr}()")

    span_fn = imported.get("span")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if not (
                span_fn is not None
                and isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and imported.get(call.func.id) is span_fn
                and isinstance(item.optional_vars, ast.Name)
            ):
                continue
            name = item.optional_vars.id
            for inner in ast.walk(node):
                target = inner.value if isinstance(inner, ast.Subscript) else inner
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == name
                    and isinstance(getattr(inner, "ctx", None), ast.Store)
                ):
                    problems.append(
                        f"{where}:{getattr(inner, 'lineno', 0)}: {ast.unparse(inner)} — span() yields the span's "
                        "id, a str; record what a step did with emit()"
                    )
    return problems


def test_the_scan_finds_the_examples_these_tests_rely_on() -> None:
    # Guards the scan: an empty list would make the test below pass vacuously.
    if not GIT_CHECKOUT:
        pytest.skip("needs a git checkout to list tracked files")
    wheres = {where.split(":")[0] for where, _ in EXAMPLES}
    assert {REFERENCE, "docs/instrumentation/quickstart.md", "samples/integrations/openai_instrumented.py"} <= wheres


@pytest.mark.parametrize("where, code", EXAMPLES, ids=[where for where, _ in EXAMPLES])
def test_an_example_uses_the_instrumentation_api_that_exists(where: str, code: str) -> None:
    assert _problems(where, code) == []


def _documented() -> List[Tuple[str, str, str]]:
    """(name, its parameters as written, the module it is imported from) for each heading."""
    text = (ROOT / REFERENCE).read_text(encoding="utf-8")
    imports = {
        alias.name: node.module
        for code in _FENCE.findall(text)
        for node in ast.walk(ast.parse(code))
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("layerlens.instrument")
        for alias in node.names
    }
    return [
        (m.group(1), m.group(2) or "", imports.get(m.group(1), "layerlens.instrument")) for m in _HEADING.finditer(text)
    ]


DOCUMENTED = _documented()


def test_the_reference_documents_the_core_api() -> None:
    names = {name for name, _, _ in DOCUMENTED}
    assert {"trace", "span", "emit", "trace_context", "get_trace_context", "TraceCollector"} <= names
    assert not {"TraceRecorder", "SpanData"} & names


@pytest.mark.parametrize("name, params, module", DOCUMENTED, ids=[name for name, _, _ in DOCUMENTED])
def test_a_documented_heading_names_what_exists_with_its_parameters(name: str, params: str, module: str) -> None:
    known, obj = _resolve(module, name)
    if not known:
        pytest.skip(f"{module} needs an optional dependency not installed here")
    assert obj is not _MISSING, f"{REFERENCE} documents {name}, which {module} does not have"
    if not params:
        return
    real = inspect.signature(obj)
    written = ast.parse(f"def _{params}: pass").body[0].args  # type: ignore[attr-defined]
    kinds = {
        **{a.arg: inspect.Parameter.POSITIONAL_OR_KEYWORD for a in written.args},
        **{a.arg: inspect.Parameter.KEYWORD_ONLY for a in written.kwonlyargs},
    }
    defaults = dict(zip([a.arg for a in written.args][::-1], written.defaults[::-1]))
    defaults.update({a.arg: d for a, d in zip(written.kwonlyargs, written.kw_defaults) if d is not None})
    for param, kind in kinds.items():
        assert param in real.parameters, f"{name}{params}: {name} takes no {param}"
        actual = real.parameters[param]
        assert actual.kind == kind, f"{name}{params}: {param} is {actual.kind.description}"
        if param in defaults:
            assert actual.default == ast.literal_eval(defaults[param]), f"{name}{params}: {param}'s default"
    required = [
        p
        for p in real.parameters.values()
        if p.default is p.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]
    assert [p.name for p in required if p.name not in kinds] == [], f"{name}{params} leaves out a required parameter"
