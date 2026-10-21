"""Secret scrubbing + runtime guard (credential-sprawl defense).

Secrets leak independently of capture_content: a provider exception that echoes
an API key is uploaded under the DEFAULT config. These tests prove (a) the
production scrubber strips the real shapes, (b) the real provider error path is
scrubbed end-to-end, (c) benign text is not over-scrubbed, and (d) the runtime
guard actually bites.
"""

from __future__ import annotations

import sys
import json
import inspect

import pytest

from layerlens.instrument._context import _current_collector
from layerlens.instrument._collector import TraceCollector
from layerlens.instrument._secret_scrub import (
    REDACTION,
    DEPTH_REDACTION,
    MAX_SCRUB_DEPTH,
    safe_error,
    find_secrets,
    scrub_payload,
    scrub_secrets,
)
from layerlens.instrument._capture_config import CaptureConfig

from ._secret_scan import scan_for_secrets

# Run as a required CI gate via `-m invariant` (see .github/workflows/invariants.yaml).
pytestmark = pytest.mark.invariant

SECRETS = [
    "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZ1234",
    "AKIAIOSFODNN7EXAMPLE",
    "Bearer abcdef0123456789ABCDEF",
    "postgres://admin:hunter2@db.internal:5432/app",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.abcdef123456",
    # added after adversarial review (vertex/azure/stripe/github in scope)
    "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "AccountKey=abcd1234EFGH5678ijkl9012MNOP3456==",
    "sk_live_ABCDEFGHIJKLMNOP1234567890",
    "password=SuperSecret123",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_scrub_removes_known_secret_shapes(secret: str) -> None:
    text = f"boom: {secret} happened"
    scrubbed = scrub_secrets(text)
    assert secret not in scrubbed, f"secret survived scrub: {secret}"
    assert REDACTION in scrubbed
    assert find_secrets(text), "find_secrets failed to flag a known secret"


def test_scrub_does_not_touch_benign_text() -> None:
    benign = "ValueError: list index out of range (model gpt-4o, 3 tokens)"
    assert scrub_secrets(benign) == benign, "benign error text was over-scrubbed"
    assert not find_secrets(benign)


def test_safe_error_scrubs_exception_string() -> None:
    exc = ValueError("Incorrect API key provided: sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    out = safe_error(exc)
    assert "sk-proj-" not in out and REDACTION in out


def test_provider_emit_llm_error_scrubs_api_key_real_path() -> None:
    """Real provider error path: the uploaded agent.error must not carry the key,
    but error_type must survive (observability preserved)."""
    from layerlens.instrument.adapters.providers._emit_helpers import emit_llm_error

    collector = TraceCollector(object(), CaptureConfig.standard())  # default: capture_content=True
    token = _current_collector.set(collector)
    try:
        emit_llm_error(
            "openai.chat.completions",
            ValueError("Incorrect API key provided: sk-proj-LEAKLEAKLEAKLEAKLEAK0123456789"),
            12.0,
        )
    finally:
        _current_collector.reset(token)
    events = collector.events
    blob = json.dumps([e["payload"] for e in events], default=str)
    assert "sk-proj-" not in blob, "provider error leaked the API key into the uploaded payload"
    assert events[0]["payload"].get("error_type") == "ValueError", "error_type over-scrubbed"


def test_guard_bites_on_seeded_secret() -> None:
    """The runtime guard must FAIL on a seeded secret — proves the CI net is live,
    not vacuous (hardcoded literal so it is independent of the shared regex)."""
    events = [{"event_type": "agent.error", "payload": {"error": "key sk-ant-api03-AAAAAAAAAAAAAAAAAAAA1234"}}]
    with pytest.raises(AssertionError, match="secret"):
        scan_for_secrets(events)


def test_guard_passes_clean_events() -> None:
    events = [{"event_type": "agent.error", "payload": {"error_type": "ValueError", "status": "error"}}]
    scan_for_secrets(events)  # must not raise


class TestSecretNetEnforcementWiring:
    """Prove the credential-sprawl net is actually WIRED into the autouse
    ``_enforce_schema_lock`` teardown (LAY-3572 / B25), not merely callable
    directly. A unit suite that uploads an event carrying a secret must FAIL —
    if someone dropped the ``scan_for_secrets(events)`` line from the fixture,
    the inner suite would report 0 errors and this test would fail (which a
    direct ``scan_for_secrets(...)`` call could never detect)."""

    def test_autouse_net_fails_a_suite_that_uploads_a_secret(self, pytester) -> None:
        pytester.makeconftest(
            "from tests.instrument.conftest import _enforce_schema_lock, record_for_schema_lock  # noqa: F401"
        )
        pytester.makepyfile(
            test_inner="""
            from tests.instrument.conftest import record_for_schema_lock

            def test_uploads_a_secret():
                # schema-valid agent.error (has a category) so the ERROR is from
                # the secret net specifically, not the schema validator.
                record_for_schema_lock([
                    {"event_type": "agent.error",
                     "payload": {"error": "auth failed: sk-ant-api03-AAAAAAAAAAAAAAAAAAAA1234",
                                 "error_type": "AuthError"}}
                ])
            """
        )
        result = pytester.runpytest()
        result.assert_outcomes(passed=1, errors=1)


def test_collector_chokepoint_scrubs_error_under_default_config() -> None:
    """The chokepoint runs even under the DEFAULT capture_content=True (where
    redact_payload is a no-op) — so any adapter's str(exc) error is scrubbed,
    not just the 2 provider sites that call safe_error."""
    collector = TraceCollector(object(), CaptureConfig.standard())  # capture_content=True
    collector.emit(
        "agent.error",
        {"error": "auth failed: sk-proj-LEAKLEAKLEAKLEAKLEAK0123456789", "error_type": "AuthError"},
        span_id="s1",
    )
    payload = collector.events[0]["payload"]
    assert "sk-proj-" not in json.dumps(payload), "chokepoint did not scrub error under default config"
    assert payload.get("error_type") == "AuthError", "category over-scrubbed"


# ---------------------------------------------------------------------------
# Nested secrets in error structures (LAY-3572 / R2 / B24) — provider SDKs and
# structured-error bodies put str(exc)/details inside a dict or list under the
# error key, which the top-level-only chokepoint missed.
# ---------------------------------------------------------------------------


def test_chokepoint_scrubs_secret_nested_in_error_dict() -> None:
    collector = TraceCollector(object(), CaptureConfig.standard())  # default capture_content=True
    collector.emit(
        "agent.error",
        {
            "error": {"detail": "auth failed: sk-proj-LEAKLEAKLEAKLEAKLEAK0123456789", "code": "E_AUTH"},
            "error_type": "AuthError",
        },
        span_id="s1",
    )
    payload = collector.events[0]["payload"]
    assert "sk-proj-" not in json.dumps(payload), "secret nested inside an error dict not scrubbed by the chokepoint"
    assert payload.get("error_type") == "AuthError", "category over-scrubbed"


def test_chokepoint_scrubs_secret_in_error_list() -> None:
    collector = TraceCollector(object(), CaptureConfig.standard())
    collector.emit(
        "agent.error",
        {"error_message": ["validation failed", "token AKIAIOSFODNN7EXAMPLE rejected"], "error_code": "E1"},
        span_id="s1",
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(collector.events[0]["payload"]), (
        "secret in an error list not scrubbed"
    )


# PEM private keys + Azure SAS signatures (LAY-3572 / B26) — high-value shapes
# the pattern set did not cover.
_NEW_SECRET_SHAPES = [
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "https://acct.blob.core.windows.net/c/b?sv=2021-08-06&sig=aB3dEfGh%2FIjKlMnOpQrStUvWx0123456789%3D",
]


@pytest.mark.parametrize("secret", _NEW_SECRET_SHAPES)
def test_scrub_removes_pem_and_azure_sas(secret: str) -> None:
    assert find_secrets(secret), f"new secret shape not detected: {secret!r}"
    scrubbed = scrub_secrets(f"config blob: {secret} trailing")
    assert REDACTION in scrubbed
    assert "BEGIN" not in scrubbed.replace("config blob", ""), "PEM header survived scrub"
    if "sig=" in secret:
        assert "aB3dEfGh" not in scrubbed, "Azure SAS signature survived scrub"


# ---------------------------------------------------------------------------
# BROAD secret scan over ALL string fields (LAY-3625 / A10 — user-approved
# 2026-06-25). Secrets ride in tool args, model output, agui patch values, and
# elicitation prompts — NOT just the 4 ERROR_KEYS. Under the shipping default
# (capture_content=True) redact_payload is a no-op, so a secret in tool.call
# arguments / model.invoke output uploaded CLEARTEXT to atlas-app. The collector
# chokepoint now scrubs every string value, orthogonal to capture_content.
# ---------------------------------------------------------------------------

_NON_ERROR_LEAK_CASES = [
    pytest.param(
        "tool.call",
        {"tool": "charge_card", "arguments": {"api_key": "sk-proj-LEAKLEAKLEAKLEAKLEAK0123456789"}},
        "sk-proj-",
        id="tool_call_arguments",
    ),
    pytest.param(
        "model.invoke",
        {"output_message": {"role": "assistant", "content": "your key is sk-ant-api03-AAAAAAAAAAAAAAAAAAAA1234"}},
        "sk-ant-",
        id="model_invoke_output",
    ),
    pytest.param(
        "agui.state",
        {"operations": [{"op": "add", "path": "/creds", "value": "AKIAIOSFODNN7EXAMPLE"}]},
        "AKIAIOSFODNN7EXAMPLE",
        id="agui_state_delta_value",
    ),
    pytest.param(
        "mcp.elicitation",
        {"title": "enter token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "phase": "request"},
        "ghp_",
        id="mcp_elicitation_title",
    ),
]


@pytest.mark.parametrize("event_type,payload,secret", _NON_ERROR_LEAK_CASES)
def test_chokepoint_scrubs_secret_in_non_error_field(event_type: str, payload: dict, secret: str) -> None:
    """A10: a secret in tool args / model output / agui patch value / elicitation
    title must be scrubbed at the collector chokepoint even when content capture
    is ON (capture_content=True, the full()/opt-in path where redact_payload is a
    no-op — secrets are orthogonal to capture_content). The pre-A10 scrub only
    touched ERROR_KEYS, so these all leaked. Bite: narrow scrub_payload back to
    ERROR_KEYS and this goes RED."""
    collector = TraceCollector(object(), CaptureConfig(capture_content=True))
    collector.emit(event_type, dict(payload), span_id="s1")
    blob = json.dumps(collector.events[0]["payload"], default=str)
    assert secret not in blob, f"{event_type}: secret in a non-error field leaked past the broad chokepoint"
    assert REDACTION in blob, f"{event_type}: expected the secret to be replaced with the redaction marker"


def test_broad_scrub_preserves_benign_nested_content() -> None:
    """The broad scan must not over-scrub benign nested content (the regexes are
    specific). A normal tool result with no secret-shaped values is untouched."""
    collector = TraceCollector(object(), CaptureConfig(capture_content=True))
    payload = {"tool": "search", "arguments": {"query": "weather in Paris"}, "result": {"temp": 20, "ok": True}}
    collector.emit("tool.call", dict(payload), span_id="s1")
    out = collector.events[0]["payload"]
    assert out["arguments"]["query"] == "weather in Paris", "benign nested content over-scrubbed"
    assert REDACTION not in json.dumps(out, default=str), "benign payload incorrectly redacted"


# ---------------------------------------------------------------------------
# Credit-card PAN + CVC (A15 / UCP-Q2 — PCI). ACP rfc.delegate_payment §277:
# "logs MUST NOT contain full PAN or CVC"; UCP: "Never log raw credentials". A
# card number reaching ANY uploaded string must be scrubbed at the collector
# chokepoint regardless of capture_content — the commerce content-key redaction
# only fires under capture_content=False and only on known keys, so a card that
# rides an unmapped field / the default content-on config needs the scrub net.
# The PAN pattern is Luhn-gated + card-grouped so a benign 16-digit order id is
# NOT over-scrubbed.
# ---------------------------------------------------------------------------

# Luhn-VALID test PANs (the official scheme test numbers) in every grouping form.
_PANS = [
    "4111111111111111",  # Visa, contiguous
    "4111 1111 1111 1111",  # Visa, space-grouped
    "4111-1111-1111-1111",  # Visa, dash-grouped
    "5555555555554444",  # Mastercard
    "378282246310005",  # Amex (15 digits)
    "4012888888881881",  # Visa
    "6011111111111117",  # Discover
]


@pytest.mark.parametrize("pan", _PANS)
def test_scrub_removes_card_pan(pan: str) -> None:
    text = f"charge failed for card {pan} at merchant"
    scrubbed = scrub_secrets(text)
    assert pan not in scrubbed, f"PAN survived scrub: {pan!r}"
    assert REDACTION in scrubbed
    assert "card_pan" in find_secrets(text), f"find_secrets failed to flag a real PAN: {pan!r}"


# Benign 16-ish-digit values that are NOT Luhn-valid cards: order ids, tracking
# numbers, timestamps, plain large numbers. These must NOT be over-scrubbed
# (keeping the PAN pattern specific — Luhn + card grouping).
_NOT_PANS = [
    "1234567890123456",  # 16 digits, Luhn-INVALID
    "ORDER-1234567890123456",  # order id with a letter+dash prefix
    "9999999999999999",  # Luhn-invalid run
    "2026-06-26T12:00:00",  # timestamp
    "tracking 1Z999AA10123456784",  # alphanumeric tracking number
    "amount 1234.56 usd",  # money, not a card
    # A Luhn-VALID card-shaped run FUSED inside a larger alphanumeric token (a
    # hex hash, a base64 id) is NOT a standalone PAN: a real card number in a log
    # is delimited by non-word chars, never welded to hex letters. The keyed-HMAC
    # action_context_hash (a2ui/ap2/a2a) is a random sha256 hexdigest that
    # intermittently contains such a run — it was corrupted by the PAN scrubber.
    "sha256:aa4111111111111111ff",  # Luhn-valid Visa run between hex letters
    "deadbeef4012888888881881cafe",  # Luhn-valid Visa run inside a hex digest
]


@pytest.mark.parametrize("value", _NOT_PANS)
def test_benign_card_like_value_not_over_scrubbed(value: str) -> None:
    assert "card_pan" not in find_secrets(value), f"benign value mis-flagged as a PAN: {value!r}"
    assert scrub_secrets(value) == value, f"benign card-like value over-scrubbed: {value!r}"


# CVC / CVV only in a LABELED context (a bare 3-4 digit number is everywhere).
_CVCS = ["cvc: 737", '"cvv":"4321"', "security_code=123", "card_verification_value: 1234", "cvc2=321"]


@pytest.mark.parametrize("text", _CVCS)
def test_scrub_removes_labeled_cvc(text: str) -> None:
    assert "card_cvc" in find_secrets(text), f"labeled CVC not flagged: {text!r}"
    assert REDACTION in scrub_secrets(text)


def test_bare_three_digit_number_not_scrubbed_as_cvc() -> None:
    benign = "there were 737 items in the cart"
    assert "card_cvc" not in find_secrets(benign), "bare 3-digit number mis-flagged as a CVC"
    assert scrub_secrets(benign) == benign, "benign 3-digit number over-scrubbed"


def test_chokepoint_scrubs_pan_in_commerce_field_under_default_config() -> None:
    """A real PAN riding a commerce.* event field is scrubbed at the collector
    chokepoint even under the content-on config (capture_content=True, where
    redact_payload is a no-op for content keys). Bite: remove the ``card_pan``
    pattern from SECRET_PATTERNS -> the PAN survives -> RED."""
    collector = TraceCollector(object(), CaptureConfig(capture_content=True))
    collector.emit(
        "commerce.checkout_completed",
        {"session_id": "s1", "card": {"number": "4111 1111 1111 1111", "cvc": "737"}, "amount": 49.99},
        span_id="s1",
    )
    blob = json.dumps(collector.events[0]["payload"], default=str)
    assert "4111 1111 1111 1111" not in blob, "card PAN leaked past the collector chokepoint under content-on"
    assert REDACTION in blob, "expected the PAN to be replaced with the redaction marker"


def test_hash_digest_with_embedded_pan_shaped_run_not_over_scrubbed() -> None:
    """Regression + the exact CI flake this reproduces: a keyed-HMAC digest
    (a2ui/ap2/a2a ``action_context_hash`` = ``"sha256:"`` + a random hexdigest)
    can, by chance, contain a 13-19 digit run that passes Luhn. The PAN scrubber
    must NOT corrupt the digest — a real card number is a standalone token, never
    fused to a digest's hex letters. ``scrub_payload`` runs on EVERY uploaded
    event, so an over-match here silently mangles a linkage-critical hash.

    Bite: revert the ``card_pan`` boundary to ``(?<![\\d.-])...(?![\\d.-])``
    (letters not excluded) and the embedded run is redacted -> RED. This is the
    intermittent ``test_a2ui_adapter::test_no_content_config_keeps_the_hash``
    failure, made deterministic here."""
    # 4111111111111111 is a Luhn-valid Visa test PAN, here welded between hex
    # letters exactly as it appears inside a random sha256 hexdigest.
    digest = "sha256:aabbcc4111111111111111ddeeff"
    out = scrub_payload({"action_context_hash": digest})
    assert out["action_context_hash"] == digest, "hash digest corrupted by PAN over-scrub"
    assert "card_pan" not in find_secrets(digest), "PAN-shaped run fused in a hash mis-flagged as a card"


def test_real_mcp_error_path_scrubbed_without_safe_error_call() -> None:
    """A framework/protocol error site that does NOT call safe_error (mcp tool
    wrapper) is still scrubbed by the collector chokepoint, under default config."""
    from layerlens.instrument.adapters.protocols.mcp.adapter import MCPProtocolAdapter
    from layerlens.instrument.adapters.protocols.mcp.tool_wrapper import wrap_mcp_tool_call

    adapter = MCPProtocolAdapter(capture_config=CaptureConfig.standard())
    collector = TraceCollector(object(), CaptureConfig.standard())
    token = _current_collector.set(collector)
    try:

        def broken(**_kw):
            raise RuntimeError("upstream rejected key AKIAIOSFODNN7EXAMPLE")

        with pytest.raises(RuntimeError):
            wrap_mcp_tool_call(broken, adapter)(name="charge", arguments={"q": "x"})
    finally:
        _current_collector.reset(token)
    blob = json.dumps([e["payload"] for e in collector.events], default=str)
    assert "AKIAIOSFODNN7EXAMPLE" not in blob, "mcp error path leaked an AWS key (chokepoint missed it)"


# ---------------------------------------------------------------------------
# Structural identifiers must survive the PAN scrubber (LAY-3622 F3).
# ---------------------------------------------------------------------------


STRUCTURAL_ID_FIELDS = ("span_id", "trace_id", "parent_span_id", "run_id", "originating_span_id")

#: Real card test numbers, and an all-zero id. Every one is a Luhn-valid,
#: all-decimal, card-length string — i.e. exactly the shape a machine-generated
#: hex identifier lands on by chance.
LUHN_VALID_IDS = ("4111111111111111", "5500000000000004", "0000000000000000")


@pytest.mark.parametrize("field", STRUCTURAL_ID_FIELDS)
@pytest.mark.parametrize("value", LUHN_VALID_IDS)
def test_a_luhn_valid_structural_id_is_not_destroyed(field: str, value: str) -> None:
    """A span/trace id is MACHINE-GENERATED hex, never free text — and a 16-hex id
    is all-decimal with probability (10/16)^16 and then Luhn-valid ~1 in 10, so
    roughly **1 in 19,000** ids look exactly like a card number. Measured over
    400,000 random ids: 21 destroyed.

    Replacing one with the redaction marker silently breaks parent/child linkage
    for that trace, and therefore the agent graph — the DAG loses an edge, or a
    node's identity, with no error anywhere. The scrubber cannot be right about a
    field whose whole purpose is to be an opaque correlation key.

    Bite proof: remove the structural-id exemption and every case here fails with
    ``[REDACTED-SECRET]``.
    """
    assert scrub_payload({field: value})[field] == value


def test_the_exemption_is_scoped_to_the_PAN_pattern_only() -> None:
    """The exemption must not turn a structural field into a secret-laundering
    channel: EVERY other pattern still applies there. If a credential is ever
    templated into a span id, it is still caught."""
    for secret in (
        "sk-live-abcdefghijklmnopqrstuvwxyz0123",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    ):
        for field in STRUCTURAL_ID_FIELDS:
            scrubbed = scrub_payload({field: secret})[field]
            assert scrubbed == REDACTION, f"{field} leaked a real secret: {secret[:12]}..."


@pytest.mark.parametrize("value", LUHN_VALID_IDS)
def test_a_real_PAN_is_still_scrubbed_everywhere_else(value: str) -> None:
    """VACUITY CONTROL / no-regression: the exemption is keyed on the FIELD, so the
    same string in any content or producer-supplied field is still redacted. If
    this ever passes trivially, the PAN scrubber has been disabled outright."""
    for field in ("input", "output", "content", "prompt", "error", "metadata"):
        assert scrub_payload({field: value})[field] == REDACTION, f"{field} leaked a PAN"
    # Producer-supplied NAMES stay protected too — a customer picks these, so a
    # card number appearing there is not structurally impossible the way it is in
    # a machine-generated span id.
    for field in ("agent_id", "span_name", "tool_name"):
        assert scrub_payload({field: value})[field] == REDACTION, f"{field} lost PAN protection"


def test_a_nested_structural_id_is_also_exempt() -> None:
    # Adapters nest span references (e.g. under a metadata/extra wrapper), and the
    # recursive scrub sees the same key name there.
    payload = {"metadata": {"span_id": "4111111111111111"}, "spans": [{"trace_id": "5500000000000004"}]}
    cleaned = scrub_payload(payload)
    assert cleaned["metadata"]["span_id"] == "4111111111111111"
    assert cleaned["spans"][0]["trace_id"] == "5500000000000004"


def test_free_text_scrubbing_still_ignores_the_key() -> None:
    # scrub_secrets() has no field context by design (it is used on bare strings
    # via safe_error), so it must keep redacting a PAN unconditionally.
    assert scrub_secrets("card 4111111111111111 charged") != "card 4111111111111111 charged"


def test_an_mcp_async_task_keeps_a_luhn_valid_originating_span_id() -> None:
    """``originating_span_id`` links an MCP async task to the span that started it,
    and the adapter fills it with a 16-hex span id (the current span's, or a fresh
    ``uuid4().hex[:16]``), so ~1 in 19,000 tasks carries one that is all-digit and
    Luhn-valid. The collector redacted those as card numbers, cutting the task off
    from its span with no error (LAY-4267). No test could notice: the guard scans
    what the collector ships, and the id was already gone by then.

    Bite proof: drop ``originating_span_id`` from ``_STRUCTURAL_ID_KEYS`` and both
    events carry ``[REDACTED-SECRET]`` instead of the span id.
    """
    from types import SimpleNamespace

    from layerlens.instrument._context import _pop_span, _push_span
    from layerlens.instrument.adapters.protocols.mcp.adapter import MCPProtocolAdapter

    adapter = MCPProtocolAdapter(capture_config=CaptureConfig.standard())
    target = SimpleNamespace(call_tool=lambda name, arguments=None, **kw: {"content": "ok"})
    adapter.connect(target=target)
    collector = TraceCollector(object(), CaptureConfig.standard())
    token = _current_collector.set(collector)
    span = _push_span("4111111111111111", "agent")
    try:
        target.call_tool(name="lookup", arguments={"q": "x"})
    finally:
        _pop_span(span)
        _current_collector.reset(token)

    tasks = [e for e in collector.events if e["event_type"] == "mcp.async_task"]
    assert [t["payload"]["phase"] for t in tasks] == ["start", "end"]
    for task in tasks:
        assert task["parent_span_id"] == "4111111111111111"
        assert task["payload"]["originating_span_id"] == "4111111111111111"


# ---------------------------------------------------------------------------
# The guard keeps the scrubber's structural-id exemption (LAY-4267).
#
# The collector ships a Luhn-valid all-digit span id unchanged, on purpose (see
# above). The guard used to scan every payload string with no knowledge of its key,
# so ~1 in 19,000 random span ids failed whichever test happened to mint it, at
# teardown. It hit test_openinference_floor.py on PR #79's `test (3.10)` job.
# ---------------------------------------------------------------------------


def _guard_flags(payload: dict) -> bool:
    try:
        scan_for_secrets([{"event_type": "agent.output", "payload": payload}])
    except AssertionError:
        return True
    return False


def test_guard_accepts_the_pan_shaped_span_ids_a_real_trace_ships(capture_trace, mock_client) -> None:
    """The CI flake, made deterministic: an OpenInference span whose ids happen to be
    all-digit and Luhn-valid goes through the real adapter and collector, which keep
    both ids, and the guard must accept what the collector ships. The autouse
    ``_enforce_schema_lock`` scans these same events at teardown, so before the fix
    this test also ERRORed there, exactly as it did in CI."""
    from layerlens.instrument.adapters.frameworks.openinference import OpenInferenceAdapter

    adapter = OpenInferenceAdapter(mock_client, capture_config=CaptureConfig.full())
    adapter.connect()
    adapter.ingest_span(
        {
            "name": "openai.chat",
            "trace_id": "0af7651916cd43dd8448eb211c80319c",
            "span_id": "4111111111111111",
            "parent_span_id": "5500000000000004",
            "attributes": {"openinference.span.kind": "LLM", "llm.model_name": "gpt-4o", "output.value": "ok"},
        }
    )
    adapter.flush()
    events = capture_trace["events"]

    model = next(e for e in events if e["event_type"] == "model.invoke")["payload"]
    assert model["span_id"] == "4111111111111111"
    assert model["parent_span_id"] == "5500000000000004"
    scan_for_secrets(events)


@pytest.mark.parametrize("field", STRUCTURAL_ID_FIELDS)
@pytest.mark.parametrize("value", LUHN_VALID_IDS)
def test_guard_accepts_a_luhn_valid_structural_id(field: str, value: str) -> None:
    # At the top level, nested in a dict, inside a list of dicts, and as a list
    # element, which takes its container's key: the scrubber exempts all four.
    for payload in ({field: value}, {"metadata": {field: value}}, {"spans": [{field: value}]}, {field: [value]}):
        assert not _guard_flags(payload), f"guard flagged an id the collector ships: {payload!r}"


@pytest.mark.parametrize(
    "payload",
    [
        {"output": "card 4111111111111111 on file"},
        {"input": "4111 1111 1111 1111"},
        {"error": "charge declined for 5500000000000004"},
        {"messages": [{"role": "user", "content": "my card is 4111-1111-1111-1111"}]},
        # A key the scrubber does not exempt, even though it names an id: a producer
        # picks these values.
        {"agent_id": "4111111111111111"},
        {"session_id": "4111111111111111"},
        {"tool_call_id": "4111111111111111"},
        # A structural id beside the content does not launder it.
        {"span_id": "0af7651916cd43dd", "output": "card 4111111111111111 on file"},
    ],
)
def test_guard_still_fails_on_a_pan_anywhere_else(payload: dict) -> None:
    with pytest.raises(AssertionError, match="card_pan"):
        scan_for_secrets([{"event_type": "agent.output", "payload": payload}])


@pytest.mark.parametrize("field", STRUCTURAL_ID_FIELDS)
def test_guard_exemption_covers_only_the_pan_pattern(field: str) -> None:
    # A credential templated into a span id is still caught: only the checksum-only
    # PAN pattern is skipped there, as in the scrubber.
    with pytest.raises(AssertionError, match="aws_access_key"):
        scan_for_secrets([{"event_type": "agent.output", "payload": {field: "AKIAIOSFODNN7EXAMPLE"}}])


@pytest.mark.parametrize("field", STRUCTURAL_ID_FIELDS + ("output", "error", "agent_id", "session_id"))
@pytest.mark.parametrize("value", LUHN_VALID_IDS + ("AKIAIOSFODNN7EXAMPLE", "1234567890123456", "plain text"))
def test_guard_flags_exactly_what_the_scrubber_still_redacts(field: str, value: str) -> None:
    """The guard and the collector must agree on every value: a value the scrubber
    leaves alone must pass the guard (else a random test fails), and a value it
    would still redact must fail the guard (else a leak past the chokepoint goes
    unnoticed). They share the pattern set, so this checks how each one applies it."""
    for payload in ({field: value}, {"metadata": {field: value}}, {"spans": [{field: value}]}, {field: [value]}):
        redacted = scrub_payload(payload) != payload
        assert _guard_flags(payload) is redacted, f"guard and scrubber disagree on {payload!r}"


# ---------------------------------------------------------------------------
# Nesting depth: scrubbing must never raise into the caller (LAY-4267).
#
# _scrub_value recursed once per nesting level, and on CPython <= 3.11 a list level
# costs two frames (the call and its list comprehension). Decodable tool arguments
# ~500 levels deep exhausted the default recursion limit inside collector.emit, and
# the RecursionError reached the customer's own create() call.
# The end-to-end reproduction through a real openai client lives with the other
# recorded OpenAI replays, in adapters/providers/test_openai_recorded.py.
# ---------------------------------------------------------------------------


def _nested(leaf: object, levels: int, wrap) -> object:
    for _ in range(levels):
        leaf = wrap(leaf)
    return leaf


_WRAPPERS = {"list": lambda v: [v], "tuple": lambda v: (v,), "dict": lambda v: {"nested": v}}
_SECRET = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


@pytest.mark.parametrize("wrap", _WRAPPERS.values(), ids=_WRAPPERS.keys())
def test_one_level_inside_the_bound_is_scrubbed_as_before(wrap) -> None:
    # The payload is level 0 and "output" level 1, so the innermost container here is
    # at level MAX_SCRUB_DEPTH - 1: still walked, and its secret still redacted.
    payload = {"output": _nested(_SECRET, MAX_SCRUB_DEPTH - 1, wrap)}
    assert scrub_payload(payload) == {"output": _nested(REDACTION, MAX_SCRUB_DEPTH - 1, wrap)}
    clean = {"output": _nested("plain text", MAX_SCRUB_DEPTH - 1, wrap)}
    assert scrub_payload(clean) is clean, "a clean payload inside the bound must come back unchanged, uncopied"


@pytest.mark.parametrize("wrap", _WRAPPERS.values(), ids=_WRAPPERS.keys())
@pytest.mark.parametrize("levels", [MAX_SCRUB_DEPTH, MAX_SCRUB_DEPTH + 1, 900])
def test_a_container_at_the_bound_is_replaced_by_the_marker(wrap, levels: int) -> None:
    # Fail closed: whatever sits at level MAX_SCRUB_DEPTH and below is never uploaded,
    # scrubbed or not, so a secret down there cannot leak and nothing recurses into it.
    payload = {"output": _nested(_SECRET, levels, wrap)}
    assert scrub_payload(payload) == {"output": _nested(DEPTH_REDACTION, MAX_SCRUB_DEPTH - 1, wrap)}


def test_a_secret_below_the_bound_never_reaches_the_uploaded_trace(mock_client, capture_trace) -> None:
    collector = TraceCollector(mock_client, CaptureConfig.full())
    deep = _nested({"api_key": _SECRET}, 900, lambda v: [v])
    collector.emit("tool.call", {"provider": "openai", "tool_name": "lookup", "arguments": deep}, span_id="s1")
    collector.flush()

    blob = json.dumps(capture_trace["events"])
    assert _SECRET not in blob
    assert DEPTH_REDACTION in blob
    scan_for_secrets(capture_trace["events"])


def test_scrubbing_fits_in_the_last_300_frames_of_the_recursion_limit() -> None:
    """The bound is what keeps the scrub small: it must work under a caller whose own
    stack (a framework, an async runtime, a web server) already holds most of the
    recursion limit. Here all but 300 frames are used, pytest's own included."""
    payload = {"output": _nested("x", 900, lambda v: [v])}

    def descend(frames: int) -> object:
        return scrub_payload(payload) if frames == 0 else descend(frames - 1)

    result = descend(sys.getrecursionlimit() - 300 - len(inspect.stack(0)))
    assert result == {"output": _nested(DEPTH_REDACTION, MAX_SCRUB_DEPTH - 1, lambda v: [v])}
