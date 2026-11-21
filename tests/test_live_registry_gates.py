"""The live suite's skip gates must match what each lane's runner needs (LAY-4260).

A live lane skips only through ``pytest.importorskip`` on its case's
``import_name`` (and ``extra_imports``). A runner that imports an optional
package its case does not gate on therefore never skips on a machine without
that package: the lane fails on the import or, if the runner swallows the
``ImportError``, runs without exercising its adapter at all. The ap2 lane was
registered that way: gated on ``layerlens`` while its runner needs the ``ap2``
SDK and returned early when it was missing.

CI never runs the live suite (``tests/e2e/live/conftest.py`` skips everything
under that directory unless opted in), so these checks live outside it and read
the registries statically.
"""

from __future__ import annotations

import os
import ast
import sys
import inspect
import textwrap
import sysconfig
import importlib.util
from typing import Any, Set, List, Tuple, Callable

from packaging.requirements import Requirement

from tests.e2e.live._registry import PROVIDERS
from tests.e2e.live._protocol_registry import PROTOCOLS
from tests.e2e.live._framework_registry import FRAMEWORKS

if sys.version_info >= (3, 11):
    import tomllib
else:  # py3.9/3.10: tomli ships with pytest's dependencies
    import tomli as tomllib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ALL_CASES: List[Tuple[str, Any]] = (
    [("providers", c) for c in PROVIDERS]
    + [("frameworks", c) for c in FRAMEWORKS]
    + [("protocols", c) for c in PROTOCOLS]
)


def _is_stdlib(package: str) -> bool:
    names = getattr(sys, "stdlib_module_names", None)  # Python >= 3.10
    if names is not None:
        return package in names
    # Python 3.9 has no such list: a stdlib module is built in, frozen, or lives
    # under the interpreter's stdlib directory but outside any site-packages.
    if package in sys.builtin_module_names:
        return True
    spec = importlib.util.find_spec(package)
    if spec is None or spec.origin is None:
        return False  # not installed (the stdlib always is), or a namespace package
    if spec.origin == "frozen":
        return True
    origin = os.path.realpath(spec.origin)
    stdlib_dirs = {os.path.realpath(sysconfig.get_paths()[key]) for key in ("stdlib", "platstdlib")}
    return "site-packages" not in origin.split(os.sep) and any(origin.startswith(d + os.sep) for d in stdlib_dirs)


def _sdk_dependencies() -> Set[str]:
    """Import names of the SDK's own dependencies, installed wherever the SDK is.

    Distribution and import names coincide for every current core dependency
    (httpx, pydantic). One where they differ would be reported as an ungated
    import below: a loud false positive, never a silent pass.
    """
    with open(os.path.join(REPO_ROOT, "pyproject.toml"), "rb") as f:
        dependencies = tomllib.load(f)["project"]["dependencies"]
    return {Requirement(d).name.lower().replace("-", "_") for d in dependencies}


def _imported_packages(runner: Callable[..., Any]) -> Set[str]:
    """Top-level packages imported anywhere in the runner's body, nested blocks
    included. Module-level imports of the scenario modules are not scanned: one
    the environment lacks stops the registries importing, which errors this
    module and the live suite's collection instead of skipping."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(runner)))
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _gated_packages(case: Any) -> Set[str]:
    # Compared per top-level package, because Python 3.9 offers no module ->
    # distribution map. Known blind spot: a namespace package counts as one, so
    # a gate on ``google.adk`` also covers an import of ``google.genai``.
    gates = (case.import_name, *getattr(case, "extra_imports", ()))
    return {gate.split(".")[0] for gate in gates}


def test_every_optional_package_a_runner_imports_is_a_skip_gate() -> None:
    always_installed = {"layerlens", "pytest"} | _sdk_dependencies()
    offenders = []
    for registry, case in _ALL_CASES:
        gated = _gated_packages(case)
        ungated = sorted(
            package
            for package in _imported_packages(case.runner)
            if package not in always_installed and package not in gated and not _is_stdlib(package)
        )
        if ungated:
            offenders.append(f"{registry}:{case.id} imports {ungated} but its skip gates are {sorted(gated)}")

    assert not offenders, (
        "live runners import optional packages their case does not gate on, so a machine without "
        "them runs the lane instead of skipping it: "
        + "; ".join(offenders)
        + ". Gate on the package via import_name or extra_imports."
    )


# The steps run_ap2 drives, and the event type each one emits
# (src/layerlens/instrument/adapters/protocols/ap2.py):
_AP2_STEP_EVENT_TYPES = {
    "payment.intent_mandate",  # record_intent_mandate
    "payment.cart_mandate",  # record_cart_mandate, accepted
    "payment.mandate_signed",  # record_payment_mandate
    "payment.receipt_issued",  # issue_receipt
    "policy.violation",  # the off-whitelist cart that AP2Guardrails blocks
}


def test_ap2_lane_requires_an_event_from_every_step_it_drives() -> None:
    (ap2,) = [case for case in PROTOCOLS if case.id == "ap2"]
    # Five steps emit six events: the blocked cart adds its own payment.cart_mandate
    # beside its policy.violation. With the default min_events of 1 the lane would
    # pass on five events, the types present but the blocked cart's own event gone.
    assert ap2.min_events >= 6, "the ap2 live lane must require every event of its six-event chain"
    missing = sorted(_AP2_STEP_EVENT_TYPES - set(ap2.expected_types))
    assert not missing, (
        f"the ap2 live lane does not require {missing}, so it can pass while those AP2 steps "
        "emit nothing. List every step's event type in the case's expected_types."
    )
