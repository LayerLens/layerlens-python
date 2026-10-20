"""Per-protocol registry. Reuses ``FrameworkCase``. Protocols are LLM-free,
but they DO carry content (message text, tool args, payment details), so each
content-emitting protocol also runs the ``redaction`` variant — the scenarios
embed ``SENTINEL`` in their content fields and the harness asserts it never
reaches the trace payload under ``capture_content=False`` (LAY-3578 / N7).
"""

from __future__ import annotations

from typing import Tuple

from . import _protocol_scenarios as ps
from ._framework_registry import FrameworkCase

# Built-in protocols (agui/a2ui/ucp) need no external package -> import_name
# "layerlens" always resolves. ap2's runner builds its mandate chain from the
# ``ap2`` SDK's own models, so the case gates on ``ap2`` and skips by name where
# that SDK is missing. mcp/a2a may require their optional package; the test
# importorskips it (and they are exercised in a Python 3.10+ venv).
PROTOCOLS: Tuple[FrameworkCase, ...] = (
    FrameworkCase(
        id="agui", import_name="layerlens", runner=ps.run_agui, supports_redaction=True, install_hint="built-in"
    ),
    FrameworkCase(
        # a2ui emits only ids/counts and a KEYED HMAC of the action context
        # (per-instance random key, never emitted) — no cleartext content to
        # redact, and the digest is not reversible (P3 fix, LAY-3578).
        id="a2ui",
        import_name="layerlens",
        runner=ps.run_a2ui,
        supports_redaction=False,
        install_hint="built-in",
    ),
    FrameworkCase(
        id="ap2",
        import_name="ap2",
        runner=ps.run_ap2,
        supports_redaction=True,
        # One event type per step run_ap2 drives: intent -> cart (accepted) ->
        # payment -> receipt, then the off-whitelist cart the guardrail blocks.
        # All five are emitted under both variants: the payment.* types ride
        # l6c_protocol_lifecycle, which both capture configs keep on, and
        # policy.violation is always enabled.
        expected_types=(
            "payment.intent_mandate",
            "payment.cart_mandate",
            "payment.mandate_signed",
            "payment.receipt_issued",
            "policy.violation",
        ),
        # One event per step, plus the blocked cart's own payment.cart_mandate
        # (status "blocked"), emitted alongside its policy.violation.
        min_events=6,
        install_hint=(
            "ap2 @ git+https://github.com/google-agentic-commerce/AP2"
            "@b4587ac1d055888a73b4b21750973cffba961793 (py>=3.11)"
        ),
    ),
    FrameworkCase(
        id="ucp", import_name="layerlens", runner=ps.run_ucp, supports_redaction=True, install_hint="built-in"
    ),
    FrameworkCase(
        id="mcp",
        import_name="mcp",
        runner=ps.run_mcp,
        supports_redaction=True,
        install_hint="layerlens[mcp] (py>=3.10)",
    ),
    FrameworkCase(
        id="a2a",
        import_name="a2a",
        runner=ps.run_a2a,
        supports_redaction=True,
        install_hint="layerlens[a2a] (py>=3.10)",
    ),
)
