"""AP2 expiry gate on every Python the SDK supports, driven with AP2's JSON mandates.

The adapter refuses the receipt for an expired cart, and for a cart bought under an
expired intent. ``test_ap2_payment.py`` proves the gate with the pinned ``ap2`` SDK's
own models, but that SDK requires Python >= 3.11, and the expiry parse is exactly
where Python versions differ: before 3.11 ``datetime.fromisoformat`` rejects a ``Z``
suffix and any fraction of a second that is not 3 or 6 digits long. The adapter read
an expiry it could not parse as "never expires", so on Python 3.8-3.10 an expired
cart such as ``"2000-01-01T00:00:00Z"`` got a receipt.

An integrator on those versions handles AP2 mandates as the JSON objects AP2 sends
(the adapter reads dicts and models alike). This module drives the adapter's public
``record_*`` / ``issue_receipt`` API with those objects, built to match
``model_dump(mode="json")`` of the pinned SDK's models field for field. It needs no
optional dependency, so it runs on every Python in the Run Tests job and in the
Invariant Gates job.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Callable, Iterator, Optional
from datetime import tzinfo, datetime, timezone, timedelta

import pytest

from layerlens.instrument import _spend_ledger
from layerlens.instrument._context import _current_collector
from layerlens.instrument._collector import TraceCollector
from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.protocols.ap2 import AP2ProtocolAdapter, _parse_iso

# A receipt for an expired mandate is a payment-safety defect: these run in the
# required Invariant Gates job too.
pytestmark = pytest.mark.invariant

_UTC = timezone.utc
_FAR_FUTURE = "2999-01-01T00:00:00Z"
_EXPIRED = "2000-01-01T00:00:00Z"
#: Builder value: leave the expiry key out of the mandate.
_ABSENT: Any = object()


# ── AP2 mandates as JSON: model_dump(mode="json") of the pinned SDK's models ──


def _item(label: str, value: float) -> Dict[str, Any]:
    return {"label": label, "amount": {"currency": "USD", "value": value}, "pending": None, "refund_period": 30}


def _intent(expiry: Any = _FAR_FUTURE) -> Dict[str, Any]:
    intent: Dict[str, Any] = {
        "user_cart_confirmation_required": True,
        "natural_language_description": "red basketball shoes",
        "merchants": ["ACME"],
        "skus": None,
        "requires_refundability": False,
        "intent_expiry": expiry,
    }
    if expiry is _ABSENT:
        del intent["intent_expiry"]
    return intent


def _cart(expiry: Any = _FAR_FUTURE) -> Dict[str, Any]:
    item = _item("Shoes", 49.99)
    contents: Dict[str, Any] = {
        "id": "cart1",
        "user_cart_confirmation_required": True,
        "payment_request": {
            "method_data": [{"supported_methods": "card", "data": {}}],
            "details": {
                "id": "pd1",
                "display_items": [item],
                "shipping_options": None,
                "modifiers": None,
                "total": item,
            },
            "options": None,
            "shipping_address": None,
        },
        "cart_expiry": expiry,
        "merchant_name": "ACME",
    }
    if expiry is _ABSENT:
        del contents["cart_expiry"]
    return {"contents": contents, "merchant_authorization": "eyJhbGciOiJSUzI1NiJ9.merchant-cart-sig.zzz"}


def _payment() -> Dict[str, Any]:
    return {
        "payment_mandate_contents": {
            "payment_mandate_id": "pay1",
            "payment_details_id": "pd1",
            "payment_details_total": _item("Total", 49.99),
            "payment_response": {
                "request_id": "pd1",
                "method_name": "card",
                "details": None,
                "shipping_address": None,
                "shipping_option": None,
                "payer_name": None,
                "payer_email": None,
                "payer_phone": None,
            },
            "merchant_agent": "ACME",
            "timestamp": "2026-10-16T09:00:00.000000+00:00",
        },
        "user_authorization": "eyJhbGciOiJFUzI1NksifQ.user-payment-vp.zzz",
    }


# ── harness ──


@pytest.fixture(autouse=True)
def _fresh_ledger() -> Iterator[None]:
    """Each test is a fresh run with its own run-scoped spend ledger."""
    token = _spend_ledger.set_ledger(None)
    try:
        yield
    finally:
        _spend_ledger.reset_ledger(token)


def _collect(fn: Callable[[], Any], config: Optional[CaptureConfig] = None) -> List[Dict[str, Any]]:
    collector = TraceCollector(object(), config or CaptureConfig())
    token = _current_collector.set(collector)
    try:
        fn()
    finally:
        _current_collector.reset(token)
    return collector.events


def _signed_cart(
    *,
    cart_expiry: Any = _FAR_FUTURE,
    intent_expiry: Any = _FAR_FUTURE,
    capture_config: Optional[CaptureConfig] = None,
) -> AP2ProtocolAdapter:
    """An adapter that has seen intent ``m1``, cart ``cart1`` bought under it, and
    the payment for that cart: the cart is signed, so only an expiry can refuse
    its receipt."""
    adapter = AP2ProtocolAdapter(capture_config=capture_config)
    adapter.record_intent_mandate(_intent(intent_expiry), mandate_id="m1")
    adapter.record_cart_mandate(_cart(cart_expiry), intent_mandate_id="m1")
    adapter.record_payment_mandate(_payment(), cart_id="cart1")
    return adapter


def _refusal(
    adapter: AP2ProtocolAdapter,
    *,
    now: Optional[datetime] = None,
    config: Optional[CaptureConfig] = None,
) -> Dict[str, Any]:
    """Ask for cart1's receipt, assert it is refused and no receipt is emitted,
    and return the refusal's ``policy.violation`` payload."""

    def go() -> None:
        with pytest.raises(PermissionError):
            adapter.issue_receipt(cart_id="cart1", now=now)

    events = _collect(go, config)
    assert not [e for e in events if e["event_type"] == "payment.receipt_issued"], "receipt issued: expiry not enforced"
    violations = [e["payload"] for e in events if e["event_type"] == "policy.violation"]
    assert len(violations) == 1, violations
    assert violations[0]["stage"] == "receipt" and violations[0]["cart_id"] == "cart1", violations[0]
    return violations[0]


def _assert_receipted(adapter: AP2ProtocolAdapter, *, now: Optional[datetime] = None) -> None:
    events = _collect(lambda: adapter.issue_receipt(cart_id="cart1", now=now))
    issued = [e["payload"] for e in events if e["event_type"] == "payment.receipt_issued"]
    assert [p["status"] for p in issued] == ["issued"], events
    assert not [e for e in events if e["event_type"] == "policy.violation"], events


# ===========================================================================
# The expiry grammar, pinned to literal instants. Every case reads the same on
# every Python. The Z / lower-case z / fraction cases are the ones
# ``fromisoformat`` rejects before 3.11 (lower-case z on EVERY version), and the
# refused cases include forms that only SOME versions accept, so a gate that
# leaned on ``fromisoformat`` alone would decide differently per Python.
# BITE (in ``_parse_iso``): drop the Z rewrite -> RED everywhere for the
# lower-case z and on 3.9 / 3.10 for every Z; drop the fraction padding -> RED on
# 3.9 / 3.10; drop the pattern check or match a prefix only -> RED everywhere.
# ===========================================================================


class TestParseIso:
    @pytest.mark.parametrize(
        "text, instant",
        [
            # RFC 3339 section 5.6, as AP2's Go reference merchant writes it (time.RFC3339).
            ("2026-10-16T10:00:00Z", datetime(2026, 10, 16, 10, tzinfo=_UTC)),
            # RFC 3339 section 5.6 NOTE: "T" and "Z" may be lower case.
            ("2026-10-16t10:00:00z", datetime(2026, 10, 16, 10, tzinfo=_UTC)),
            # The same NOTE allows a space between date and time; str(datetime) writes one.
            ("2026-10-16 10:00:00+00:00", datetime(2026, 10, 16, 10, tzinfo=_UTC)),
            # datetime.isoformat(), as the ap2 SDK writes its own timestamps.
            ("2026-10-16T10:00:00.250000+00:00", datetime(2026, 10, 16, 10, 0, 0, 250000, tzinfo=_UTC)),
            # An offset moves the instant: 12:00 at +02:00 and 05:00 at -05:00 are both 10:00 UTC.
            ("2026-10-16T12:00:00+02:00", datetime(2026, 10, 16, 10, tzinfo=_UTC)),
            ("2026-10-16T05:00:00-05:00", datetime(2026, 10, 16, 10, tzinfo=_UTC)),
            # RFC 3339 section 4.3: -00:00 is UTC with an unknown local offset.
            ("2026-10-16T10:00:00-00:00", datetime(2026, 10, 16, 10, tzinfo=_UTC)),
            # Fractions of any length (JavaScript's toISOString() writes 3 digits, Go's
            # RFC3339Nano up to 9). Digits past microseconds are dropped, which can
            # only move the expiry earlier.
            ("2026-10-16T10:00:00.5Z", datetime(2026, 10, 16, 10, 0, 0, 500000, tzinfo=_UTC)),
            ("2026-10-16T10:00:00.123Z", datetime(2026, 10, 16, 10, 0, 0, 123000, tzinfo=_UTC)),
            ("2026-10-16T10:00:00.1234+00:00", datetime(2026, 10, 16, 10, 0, 0, 123400, tzinfo=_UTC)),
            ("2026-10-16T10:00:00.123456789Z", datetime(2026, 10, 16, 10, 0, 0, 123456, tzinfo=_UTC)),
            # No offset: UTC.
            ("2026-10-16T10:00:00", datetime(2026, 10, 16, 10, tzinfo=_UTC)),
            # ISO 8601 reduced precision: a date is its midnight, a time without
            # seconds or minutes starts that minute or hour.
            ("2026-10-16", datetime(2026, 10, 16, tzinfo=_UTC)),
            ("2026-10-16T10", datetime(2026, 10, 16, 10, tzinfo=_UTC)),
            ("2026-10-16T10:30Z", datetime(2026, 10, 16, 10, 30, tzinfo=_UTC)),
        ],
    )
    def test_reads_the_instant(self, text: str, instant: datetime) -> None:
        parsed = _parse_iso(text)
        # Offset-aware, so comparing it with the receipt clock can never raise.
        assert parsed is not None and parsed.utcoffset() is not None, parsed
        assert parsed == instant

    def test_reads_a_datetime_as_its_instant(self) -> None:
        """An integrator's own mandate model may hold the expiry as a datetime."""
        noon_at_plus_two = datetime(2026, 10, 16, 12, tzinfo=timezone(timedelta(hours=2)))
        assert _parse_iso(noon_at_plus_two) == datetime(2026, 10, 16, 10, tzinfo=_UTC)
        # Naive: UTC, as for a string without an offset.
        naive = _parse_iso(datetime(2026, 10, 16, 10))
        assert naive is not None and naive.utcoffset() == timedelta(0), naive
        assert naive == datetime(2026, 10, 16, 10, tzinfo=_UTC)

    def test_reads_a_datetime_whose_tzinfo_has_no_offset_as_utc(self) -> None:
        """Python counts a datetime as naive when its tzinfo gives no offset. Kept
        as is, comparing it with the receipt clock would raise TypeError."""

        class _NoOffset(tzinfo):
            def utcoffset(self, dt: Optional[datetime]) -> Optional[timedelta]:
                return None

            def dst(self, dt: Optional[datetime]) -> Optional[timedelta]:
                return None

            def tzname(self, dt: Optional[datetime]) -> Optional[str]:
                return None

        parsed = _parse_iso(datetime(2026, 10, 16, 10, tzinfo=_NoOffset()))
        assert parsed is not None and parsed.utcoffset() == timedelta(0), parsed
        assert parsed == datetime(2026, 10, 16, 10, tzinfo=_UTC)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "tomorrow",
            "2026-13-01T00:00:00Z",  # no month 13
            "2026-02-30T00:00:00Z",  # no February 30
            "2026-10-16T24:00:00Z",  # Python 3.14's fromisoformat reads this as the next midnight
            "2026-10-16T23:59:60Z",  # a leap second: RFC 3339 allows it, datetime cannot hold it
            "2026-10-16T10:00:00+05:75",  # every fromisoformat reads this as +06:15
            "2026-10-16T10:00:00+24:00",  # an offset of a whole day
            "2026-10-16T10:00:00+0530",  # basic-format offset: fromisoformat 3.11+ only
            "2026-10-16T10:00:00+05",  # hour-only offset: fromisoformat 3.11+ only
            "20261016T100000Z",  # ISO 8601 basic format: fromisoformat 3.11+ only
            "2026-W42-5",  # ISO week date: fromisoformat 3.11+ only
            "2026-10-16X10:00:00Z",  # fromisoformat takes any character as the separator
            "2026-10-16Z",  # an offset without a time
            " 2026-10-16T10:00:00Z",
            "2026-10-16T10:00:00Z\n",
            1792144800,  # an epoch number is not an ISO 8601 date-time
        ],
    )
    def test_refuses_what_is_not_an_iso_8601_date_time(self, value: Any) -> None:
        assert _parse_iso(value) is None


# ===========================================================================
# CART EXPIRY gates the receipt. The defect: before Python 3.11 a Z-suffixed
# expiry parsed to nothing and the cart counted as unexpired. An expiry that is
# present but unreadable is refused (fail closed); an absent one still means
# the cart does not expire.
# BITE: in ``issue_receipt``, drop the MANDATE_EXPIRY_INVALID branch (or read an
# unreadable expiry as None again) -> RED; break the Z rewrite -> RED on 3.9/3.10.
# ===========================================================================


class TestCartExpiry:
    def test_expired_cart_with_z_suffix_gets_no_receipt(self) -> None:
        assert _refusal(_signed_cart(cart_expiry=_EXPIRED))["reason_code"] == "MANDATE_EXPIRED"

    def test_unexpired_cart_with_z_suffix_gets_its_receipt(self) -> None:
        _assert_receipted(_signed_cart(cart_expiry=_FAR_FUTURE))

    @pytest.mark.parametrize(
        "expiry",
        [
            "2026-10-16T10:00:00.250Z",
            "2026-10-16T12:00:00.250+02:00",
            "2026-10-16T10:00:00.2500+00:00",
        ],
    )
    def test_gate_closes_at_the_expiry_instant(self, expiry: str) -> None:
        """Each form names 10:00:00.25 UTC: a microsecond before it the cart is
        valid, a microsecond after it the receipt is refused."""
        instant = datetime(2026, 10, 16, 10, 0, 0, 250000, tzinfo=_UTC)
        _assert_receipted(_signed_cart(cart_expiry=expiry), now=instant - timedelta(microseconds=1))
        late = _refusal(_signed_cart(cart_expiry=expiry), now=instant + timedelta(microseconds=1))
        assert late["reason_code"] == "MANDATE_EXPIRED"

    @pytest.mark.parametrize("expiry", ["", "tomorrow", "2026-02-30T00:00:00Z", 1792144800])
    def test_unreadable_expiry_gets_no_receipt(self, expiry: Any) -> None:
        """The gate cannot tell whether this cart expired, so it refuses the
        receipt rather than reading the expiry as "never"."""
        assert _refusal(_signed_cart(cart_expiry=expiry))["reason_code"] == "MANDATE_EXPIRY_INVALID"

    @pytest.mark.parametrize("expiry", [_ABSENT, None], ids=["key-absent", "null"])
    def test_cart_without_an_expiry_gets_its_receipt(self, expiry: Any) -> None:
        _assert_receipted(_signed_cart(cart_expiry=expiry))

    def test_datetime_expiry_gates_like_its_string_form(self) -> None:
        """A naive datetime is UTC, as a string without an offset is."""
        assert _refusal(_signed_cart(cart_expiry=datetime(2000, 1, 1)))["reason_code"] == "MANDATE_EXPIRED"
        _assert_receipted(_signed_cart(cart_expiry=datetime(2999, 1, 1, tzinfo=_UTC)))


# ===========================================================================
# INTENT EXPIRY: ``intent_expiry`` bounds the authority the user gave. Once it
# has passed, a cart bought under that intent gets no receipt.
# BITE: drop the intent from the receipt's expiry check -> RED.
# ===========================================================================


class TestIntentExpiry:
    def test_expired_intent_refuses_the_receipt(self) -> None:
        assert _refusal(_signed_cart(intent_expiry=_EXPIRED))["reason_code"] == "MANDATE_EXPIRED"

    def test_unreadable_intent_expiry_refuses_the_receipt(self) -> None:
        assert _refusal(_signed_cart(intent_expiry="end of quarter"))["reason_code"] == "MANDATE_EXPIRY_INVALID"

    @pytest.mark.parametrize("expiry", [_ABSENT, None], ids=["key-absent", "null"])
    def test_intent_without_an_expiry_does_not_refuse(self, expiry: Any) -> None:
        _assert_receipted(_signed_cart(intent_expiry=expiry))

    def test_only_the_intent_the_cart_was_bought_under_gates_it(self) -> None:
        adapter = AP2ProtocolAdapter()
        adapter.record_intent_mandate(_intent(_EXPIRED), mandate_id="stale")
        adapter.record_intent_mandate(_intent(_FAR_FUTURE), mandate_id="m1")
        adapter.record_cart_mandate(_cart(), intent_mandate_id="m1")
        adapter.record_payment_mandate(_payment(), cart_id="cart1")
        _assert_receipted(adapter)


# ===========================================================================
# CONTENT. A readable expiry is an instant, not content: the mandate events
# report it as the mandate sent it, and it survives capture_content=False, so a
# refusal stays auditable (when did the cart expire?). An unreadable expiry is
# merchant-supplied text: no event carries it. The mandate event marks it with
# ``<key>_invalid: True`` instead, and the refusal keeps its reason code.
# BITE: emit the unreadable value, drop or always emit the marker, rename an
# expiry key, or put the refusal detail in a field that survives redaction -> RED.
# ===========================================================================


@pytest.mark.parametrize(
    "sent, reported",
    [
        # Each case maps a key suffix ("" is the expiry itself) to its emitted value.
        ("2999-06-01T00:00:00.5+02:00", {"": "2999-06-01T00:00:00.5+02:00"}),
        (_ABSENT, {"": None}),
        ("end of quarter", {"": None, "_invalid": True}),
    ],
    ids=["readable", "absent", "unreadable"],
)
@pytest.mark.parametrize("side", ["adapter", "collector"])
def test_mandate_events_report_each_expiry_without_content(side: str, sent: Any, reported: Dict[str, Any]) -> None:
    no_content = CaptureConfig(capture_content=False)
    adapter = AP2ProtocolAdapter(capture_config=no_content if side == "adapter" else None)

    def go() -> None:
        adapter.record_intent_mandate(_intent(sent), mandate_id="m1")
        adapter.record_cart_mandate(_cart(sent), intent_mandate_id="m1")

    events = _collect(go, no_content if side == "collector" else None)
    for event_type, key in (("payment.intent_mandate", "intent_expiry"), ("payment.cart_mandate", "cart_expiry")):
        payloads = [e["payload"] for e in events if e["event_type"] == event_type]
        assert len(payloads) == 1, events
        fields = {name: value for name, value in payloads[0].items() if name.startswith(key)}
        assert fields == {key + suffix: value for suffix, value in reported.items()}, payloads[0]


@pytest.mark.parametrize("side", ["adapter", "collector"])
def test_unreadable_expiry_never_reaches_the_uploaded_trace_without_content(
    side: str, mock_client: Any, capture_trace: Dict[str, Any]
) -> None:
    """Through the real upload: intent and cart both carry unreadable expiries,
    the payment is signed, and the receipt is refused."""
    raw = "SENTINEL-not-a-date"
    no_content = CaptureConfig(capture_content=False)
    adapter = AP2ProtocolAdapter(capture_config=no_content if side == "adapter" else None)
    collector = TraceCollector(mock_client, no_content if side == "collector" else CaptureConfig())
    token = _current_collector.set(collector)
    try:
        adapter.record_intent_mandate(_intent(raw), mandate_id="m1")
        adapter.record_cart_mandate(_cart(raw), intent_mandate_id="m1")
        adapter.record_payment_mandate(_payment(), cart_id="cart1")
        with pytest.raises(PermissionError):
            adapter.issue_receipt(cart_id="cart1")
    finally:
        _current_collector.reset(token)
    collector.flush()

    events = capture_trace["events"]
    assert raw not in json.dumps(capture_trace, default=str), "an unreadable expiry reached the uploaded trace"

    def payloads(event_type: str) -> List[Dict[str, Any]]:
        return [e["payload"] for e in events if e["event_type"] == event_type]

    assert [p.get("intent_expiry_invalid") for p in payloads("payment.intent_mandate")] == [True], events
    assert [p.get("cart_expiry_invalid") for p in payloads("payment.cart_mandate")] == [True], events
    assert [p.get("reason_code") for p in payloads("policy.violation")] == ["MANDATE_EXPIRY_INVALID"], events
