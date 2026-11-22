"""``tests._optional_deps`` treats a module as absent only when it is NOT installed.

``pytest.importorskip`` skips on any ``ImportError``, so a framework that is
installed but broken skips its whole suite instead of failing (LAY-4242: CI
resolved semantic-kernel 1.8.0, whose ``from pydantic.networks import Url`` fails
on pydantic >= 2.12, and the semantic-kernel and ms-agent suites vanished). Each
case below imports a real throwaway package from ``tmp_path``.
"""

from __future__ import annotations

import sys
import uuid
import textwrap
from pathlib import Path

import pytest

from tests._optional_deps import importorskip, is_installed


@pytest.fixture
def make_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a package with the given ``__init__.py`` body; return its unique name."""
    monkeypatch.syspath_prepend(str(tmp_path))
    created = []

    def _make(init_body: str = "", submodules: "dict[str, str] | None" = None) -> str:
        name = f"ll_optional_{uuid.uuid4().hex[:12]}"
        package = tmp_path / name
        package.mkdir()
        (package / "__init__.py").write_text(textwrap.dedent(init_body))
        for sub, body in (submodules or {}).items():
            (package / f"{sub}.py").write_text(textwrap.dedent(body))
        created.append(name)
        return name

    yield _make
    for name in created:
        for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[mod]


def _absent_name() -> str:
    return f"ll_not_installed_{uuid.uuid4().hex[:12]}"


def _import_broken(modname: str) -> None:
    """Import an installed-but-broken module, failing (not skipping) if it skips.

    A skip raised in here would otherwise escape ``pytest.raises`` and mark the
    test skipped: the very failure mode these tests exist to catch.
    """
    try:
        importorskip(modname)
    except pytest.skip.Exception as skipped:
        pytest.fail(f"an installed-but-broken module was skipped: {skipped.msg}")


class TestNotInstalledSkips:
    def test_a_module_that_is_not_installed_skips(self) -> None:
        name = _absent_name()

        with pytest.raises(pytest.skip.Exception, match=name):
            importorskip(name)

    def test_the_reason_is_the_skip_message(self) -> None:
        with pytest.raises(pytest.skip.Exception, match="^dspy not installed$"):
            importorskip(_absent_name(), reason="dspy not installed")

    def test_a_missing_parent_package_skips(self) -> None:
        # importorskip("langgraph.graph") with no langgraph at all.
        parent = _absent_name()

        with pytest.raises(pytest.skip.Exception, match=parent):
            importorskip(f"{parent}.graph")

    def test_a_submodule_the_installed_version_does_not_ship_skips(self, make_package) -> None:
        name = make_package("VALUE = 1\n")

        with pytest.raises(pytest.skip.Exception, match=f"{name}.memory"):
            importorskip(f"{name}.memory")


class TestInstalledButBrokenFails:
    def test_an_import_error_inside_the_module_propagates(self, make_package) -> None:
        # The semantic-kernel 1.8.0 + pydantic >= 2.12 failure, in miniature.
        name = make_package("from json import Url\n")

        with pytest.raises(ImportError, match="cannot import name 'Url'") as raised:
            _import_broken(name)
        assert not isinstance(raised.value, ModuleNotFoundError)

    def test_a_missing_dependency_of_the_module_propagates(self, make_package) -> None:
        dependency = _absent_name()
        name = make_package(f"import {dependency}\n")

        with pytest.raises(ModuleNotFoundError) as raised:
            _import_broken(name)
        assert raised.value.name == dependency

    def test_a_missing_dependency_whose_name_prefixes_the_module_propagates(self, make_package) -> None:
        # "ll_optional" is not a parent package of "ll_optional_<hex>".
        name = make_package("import ll_optional\n")

        with pytest.raises(ModuleNotFoundError) as raised:
            _import_broken(name)
        assert raised.value.name == "ll_optional"

    def test_a_missing_dependency_of_a_submodule_propagates(self, make_package) -> None:
        dependency = _absent_name()
        name = make_package(submodules={"graph": f"import {dependency}\n"})

        with pytest.raises(ModuleNotFoundError) as raised:
            _import_broken(f"{name}.graph")
        assert raised.value.name == dependency

    def test_a_module_not_found_error_without_a_module_name_propagates(self, make_package) -> None:
        # Raised by hand, so nothing says which module is missing: not provably "not installed".
        name = make_package('raise ModuleNotFoundError("install the [extra] to use this")\n')

        with pytest.raises(ModuleNotFoundError, match="install the"):
            _import_broken(name)

    def test_any_other_error_at_import_propagates(self, make_package) -> None:
        # crewai on Python 3.9: `type | None` raises TypeError while importing.
        name = make_package("raise TypeError(\"unsupported operand type(s) for |: 'type' and 'NoneType'\")\n")

        with pytest.raises(TypeError, match="unsupported operand"):
            _import_broken(name)


class TestInstalledReturnsTheModule:
    def test_an_installed_module_is_returned(self, make_package) -> None:
        name = make_package("VALUE = 42\n")

        module = importorskip(name)

        assert module.VALUE == 42
        assert module is sys.modules[name]

    def test_an_installed_submodule_is_returned(self, make_package) -> None:
        name = make_package(submodules={"graph": "VALUE = 'graph'\n"})

        assert importorskip(f"{name}.graph").VALUE == "graph"

    def test_a_version_below_minversion_skips(self, make_package) -> None:
        name = make_package('__version__ = "1.9.0"\n')

        with pytest.raises(pytest.skip.Exception, match="1.9.0"):
            importorskip(name, minversion="2")

    def test_a_version_at_minversion_is_returned(self, make_package) -> None:
        name = make_package('__version__ = "2.0.0"\nVALUE = 2\n')

        assert importorskip(name, minversion="2").VALUE == 2


class TestIsInstalled:
    """The flag form, for ``pytest.mark.skipif`` and version-dependent branches."""

    def test_a_module_that_is_not_installed_is_not_installed(self) -> None:
        assert is_installed(_absent_name()) is False

    def test_a_submodule_the_installed_version_does_not_ship_is_not_installed(self, make_package) -> None:
        name = make_package("VALUE = 1\n")

        assert is_installed(f"{name}.agent.workflow") is False

    def test_an_installed_module_is_installed(self, make_package) -> None:
        name = make_package("VALUE = 1\n")

        assert is_installed(name) is True

    def test_an_installed_but_broken_module_raises(self, make_package) -> None:
        name = make_package("from json import Url\n")

        with pytest.raises(ImportError, match="cannot import name 'Url'"):
            is_installed(name)

    def test_an_installed_module_missing_a_dependency_raises(self, make_package) -> None:
        dependency = _absent_name()
        name = make_package(f"import {dependency}\n")

        with pytest.raises(ModuleNotFoundError) as raised:
            is_installed(name)
        assert raised.value.name == dependency
