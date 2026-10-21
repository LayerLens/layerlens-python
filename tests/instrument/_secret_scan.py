"""Runtime secret guard for uploaded telemetry (credential-sprawl net).

Imports the production scrubber's pattern set and its structural-id exemption,
so the CI guard flags exactly what the prod redactor would still redact
(``test_guard_flags_exactly_what_the_scrubber_still_redacts`` holds them to
that), and asserts no secret-shaped value reaches any uploaded event. Wired into
the autouse ``_enforce_schema_lock`` fixture so it runs over every adapter unit
suite with zero per-test wiring. Pairs with
``layerlens.instrument._secret_scrub.safe_error`` (the production fix).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from layerlens.instrument._secret_scrub import _LUHN_VALIDATED, _STRUCTURAL_ID_KEYS, find_secrets


def _walk_strings(value: Any, path: str, out: List[tuple[str, str]], field: Optional[str] = None) -> None:
    # *field* is the key *value* sits under, threaded down as the scrubber's
    # ``_scrub_value`` threads it: a dict value takes its own key, and a list or
    # tuple element takes its container's.
    if isinstance(value, str):
        for name in find_secrets(value):
            # The collector ships a checksum-only (PAN) match in a structural id on
            # purpose: ~1 in 19,000 random 16-hex span ids is all-digit and
            # Luhn-valid (LAY-3622 F3). Flagging what it ships failed whichever test
            # minted such an id, at teardown (LAY-4267). Same keys and same pattern
            # scope as ``scrub_secrets(field=...)``: every other pattern still applies.
            if field in _STRUCTURAL_ID_KEYS and name in _LUHN_VALIDATED:
                continue
            out.append((path, name))
    elif isinstance(value, dict):
        for k, v in value.items():
            _walk_strings(v, f"{path}.{k}", out, k if isinstance(k, str) else None)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _walk_strings(v, f"{path}[{i}]", out, field)


def scan_for_secrets(events: List[Dict[str, Any]]) -> None:
    """Assert no uploaded event carries a secret-shaped value.

    Secrets leak independently of ``capture_content`` (a provider exception that
    echoes an API key is uploaded under the default config), so this guard runs
    on every recorded trace regardless of redaction settings.
    """
    hits: List[str] = []
    for event in events:
        found: List[tuple[str, str]] = []
        _walk_strings(event.get("payload") or {}, event.get("event_type", "?"), found)
        for path, name in found:
            hits.append(f"{path} matched secret pattern {name!r}")
    if hits:
        raise AssertionError(
            "secret-shaped value(s) reached an uploaded event (scrub at the emit site via "
            "layerlens.instrument._secret_scrub.safe_error):\n  " + "\n  ".join(hits)
        )
