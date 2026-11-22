"""The certification's "Capture config" check fails an adapter that drops the config it is given.

The check built the adapter with ``CaptureConfig(capture_content=False)`` and passed it if the
adapter's ``_config`` had ``capture_content`` False. That is the default, so an adapter that took
the argument and kept the default config passed, as if content gating reached it (docs E2E
2026-11-10, L7 AD-02). The check now gives the adapter a config the default is not, content on,
and then one with content off, and passes it only when each reaches ``_config``.
"""

from __future__ import annotations

from typing import Any, Optional

from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.protocols import ProtocolCertificationSuite
from layerlens.instrument.adapters.protocols._base_protocol import BaseProtocolAdapter


class _Base(BaseProtocolAdapter):
    PROTOCOL = "x"
    PROTOCOL_VERSION = "1.0"

    def connect(self, target: Any = None, **kwargs: Any) -> Any:
        return target


class _Threads(_Base):
    """Passes the config to the base, as an adapter should."""


class _DropsIt(_Base):
    """Takes ``capture_config`` and keeps the default instead."""

    def __init__(self, capture_config: Optional[CaptureConfig] = None) -> None:
        super().__init__()


class _AlwaysCaptures(_Base):
    """Takes ``capture_config`` and turns content capture on whatever it says."""

    def __init__(self, capture_config: Optional[CaptureConfig] = None) -> None:
        super().__init__(capture_config=CaptureConfig(capture_content=True))


def _check(cls: type) -> Any:
    result = ProtocolCertificationSuite().certify(cls)
    return next(c for c in result.checks if c.name == "capture_config_support")


def test_the_check_passes_only_an_adapter_whose_config_is_the_one_it_was_given() -> None:
    threads = _check(_Threads)
    assert (threads.passed, threads.message) == (True, "capture_config accepted and threaded")
    for cls in (_DropsIt, _AlwaysCaptures):
        check = _check(cls)
        assert (check.passed, check.severity) == (False, "error"), cls.__name__
        assert check.message == "capture_config not threaded to _config"
        assert ProtocolCertificationSuite().certify(cls).passed is False, cls.__name__
