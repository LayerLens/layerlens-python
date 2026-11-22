"""``error_code``: the provider's own reason for an error, whatever its text says (LAY-4454).

openai 3.38 stopped printing the response body in an error's text, so traces lost
``rate_limit_exceeded`` and the like while ``exc.code`` still held it. Each provider below
raises its real exception the real way, from an HTTP reply its own client parsed, and
:func:`error_category` must find the reason where that SDK keeps it.
"""

from __future__ import annotations

import ast
import enum
import importlib
from typing import Any, Dict, List, Tuple, Callable
from pathlib import Path

import httpx
import pytest

from layerlens.instrument import trace
from tests._optional_deps import importorskip
from layerlens.instrument._error_code import MAX_CHAIN, error_code_of, error_category
from layerlens.instrument._capture_config import CaptureConfig

from .conftest import find_event


def _replying(status: int, body: Dict[str, Any]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)))


def _raised(call: Callable[[], Any]) -> BaseException:
    with pytest.raises(Exception) as raised:
        call()
    return raised.value


def _openai_error(status: int, code: str, message: str = "x", kind: str = "invalid_request_error") -> BaseException:
    openai = importorskip("openai")
    body = {"error": {"message": message, "type": kind, "param": None, "code": code}}
    client = openai.OpenAI(
        api_key="sk-test", base_url="https://api.test/v1", http_client=_replying(status, body), max_retries=0
    )
    return _raised(
        lambda: client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
    )


def _gone(self: object) -> Any:  # noqa: ARG001
    raise RuntimeError("the proxied object is gone")


# A proxy whose ``__class__`` raises when read. isinstance() reads ``__class__`` when the
# object's type does not match, so checking the proxy raises too.
_Proxy = type("Proxy", (), {"__class__": property(_gone)})


class TestWhereEachProviderKeepsItsReason:
    @pytest.mark.parametrize(
        "status, code, exception",
        [
            (429, "rate_limit_exceeded", "RateLimitError"),
            (401, "invalid_api_key", "AuthenticationError"),
            (404, "model_not_found", "NotFoundError"),
            (400, "context_length_exceeded", "BadRequestError"),
        ],
    )
    def test_openai(self, status: int, code: str, exception: str) -> None:
        assert error_category(_openai_error(status, code)) == {"error_type": exception, "error_code": code}

    def test_openai_without_a_code_gives_its_type(self) -> None:
        openai = importorskip("openai")
        body = {"error": {"message": "The server had an error", "type": "server_error", "param": None, "code": None}}
        client = openai.OpenAI(
            api_key="sk-test", base_url="https://api.test/v1", http_client=_replying(500, body), max_retries=0
        )
        exc = _raised(lambda: client.models.list())
        assert error_code_of(exc) == "server_error"

    def test_anthropic(self) -> None:
        anthropic = importorskip("anthropic")
        body = {"type": "error", "error": {"type": "rate_limit_error", "message": "Number of request tokens..."}}
        # anthropic 1.0 moved from httpx to httpx2 (same API) and only accepts an httpx2 client.
        http = importlib.import_module("httpx2") if int(anthropic.__version__.split(".")[0]) >= 1 else httpx
        replying = http.Client(transport=http.MockTransport(lambda request: http.Response(429, json=body)))
        client = anthropic.Anthropic(api_key="sk-ant-test", http_client=replying, max_retries=0)
        exc = _raised(
            lambda: client.messages.create(
                model="claude-sonnet-4-5", max_tokens=1, messages=[{"role": "user", "content": "hi"}]
            )
        )
        assert error_category(exc) == {"error_type": "RateLimitError", "error_code": "rate_limit_error"}

    def test_groq_keeps_it_in_the_body(self) -> None:
        groq = importorskip("groq")
        body = {"error": {"message": "Rate limit reached", "type": "tokens", "code": "rate_limit_exceeded"}}
        client = groq.Groq(api_key="gsk_test", http_client=_replying(429, body), max_retries=0)
        exc = _raised(
            lambda: client.chat.completions.create(model="llama-3.3-70b", messages=[{"role": "user", "content": "hi"}])
        )
        assert error_code_of(exc) == "rate_limit_exceeded"

    def test_bedrock(self) -> None:
        boto3 = importorskip("boto3")
        from botocore.stub import Stubber  # botocore comes with boto3
        from botocore.exceptions import ClientError

        client = boto3.client(
            "bedrock-runtime", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
        )
        with Stubber(client) as stubber:
            stubber.add_client_error("converse", service_error_code="ValidationException", http_status_code=400)
            exc = _raised(lambda: client.converse(modelId="m", messages=[]))
        # botocore raises the service's modeled subclass of ClientError, named for the code.
        assert isinstance(exc, ClientError)
        assert error_category(exc) == {"error_type": "ValidationException", "error_code": "ValidationException"}

    def test_google_gen_ai_gives_the_status_not_the_http_code(self) -> None:
        importorskip("google.genai")
        from google.genai import errors

        reply = {"error": {"code": 429, "message": "Resource has been exhausted", "status": "RESOURCE_EXHAUSTED"}}
        exc = errors.ClientError(429, reply, None)
        assert exc.code == 429
        assert error_code_of(exc) == "RESOURCE_EXHAUSTED"

    def test_a_reply_without_a_reason_gives_none(self) -> None:
        importorskip("cohere")
        from cohere.core.api_error import ApiError

        exc = ApiError(status_code=429, body={"message": "You are using a Trial key"})
        assert error_category(exc) == {"error_type": "ApiError"}


class TestTheChainOfCauses:
    def test_a_frameworks_own_error_carries_its_providers_reason(self) -> None:
        provider = _openai_error(429, "rate_limit_exceeded")
        try:
            raise RuntimeError("the agent's model call failed") from provider
        except RuntimeError as exc:
            assert error_category(exc) == {"error_type": "RuntimeError", "error_code": "rate_limit_exceeded"}

    def test_an_error_raised_while_handling_one_carries_its_reason(self) -> None:
        provider = _openai_error(401, "invalid_api_key")
        try:
            try:
                raise provider
            except Exception:
                raise KeyError("retry budget")  # noqa: B904 -- the implicit context is what is tested
        except KeyError as exc:
            assert exc.__cause__ is None and exc.__context__ is provider
            assert error_code_of(exc) == "invalid_api_key"

    def test_from_none_hides_the_reason_as_it_hides_the_traceback(self) -> None:
        provider = _openai_error(401, "invalid_api_key")
        try:
            try:
                raise provider
            except Exception:
                raise ValueError("bad config") from None
        except ValueError as exc:
            assert error_code_of(exc) is None

    def test_the_nearest_reason_wins(self) -> None:
        inner = _openai_error(429, "rate_limit_exceeded")
        outer = _openai_error(404, "model_not_found")
        outer.__cause__ = inner
        assert error_code_of(outer) == "model_not_found"

    def test_a_cycle_ends(self) -> None:
        a, b = ValueError("a"), ValueError("b")
        a.__cause__, b.__cause__ = b, a
        assert error_code_of(a) is None

    def test_only_so_many_links_are_searched(self) -> None:
        provider = _openai_error(429, "rate_limit_exceeded")
        link: BaseException = provider
        for depth in range(MAX_CHAIN):
            wrapper = RuntimeError(f"wrap {depth}")
            wrapper.__cause__ = link
            link = wrapper
        assert error_code_of(link) is None  # the provider's error sits one link too deep
        assert error_code_of(link.__cause__) == "rate_limit_exceeded"  # type: ignore[arg-type]

    @pytest.mark.parametrize("attribute", ["__cause__", "__context__", "__suppress_context__"])
    def test_a_link_that_raises_when_read_counts_as_absent(self, attribute: str) -> None:
        # error_code_of never raises: it runs while a traced call is failing, and an error of
        # its own would replace the caller's.
        def unreadable(self: BaseException) -> Any:
            raise RuntimeError(f"{attribute} unavailable")

        odd = type("OddError", (Exception,), {attribute: property(unreadable)})
        assert error_code_of(odd("x")) is None

    def test_a_cause_that_raises_when_read_still_leaves_the_context_searched(self) -> None:
        odd = type("OddError", (Exception,), {"__cause__": property(lambda self: 1 / 0)})
        provider = _openai_error(429, "rate_limit_exceeded")
        try:
            try:
                raise provider
            except Exception:
                raise odd("wrapped")  # noqa: B904 -- the implicit context is what is tested
        except Exception as exc:
            assert error_code_of(exc) == "rate_limit_exceeded"

    def test_a_link_that_raises_when_checked_counts_as_absent(self) -> None:
        odd = type("OddError", (Exception,), {"__cause__": property(lambda self: _Proxy())})
        assert error_code_of(odd("x")) is None


class TestOnlyACodeIsKept:
    def test_text_in_a_code_attribute_is_not_a_code(self) -> None:
        # SystemExit, and some SDKs, put a message in ``code``; under capture_content=False
        # it would survive the redaction that strips the error's text.
        assert error_code_of(SystemExit("could not reach https://api.example.com, giving up")) is None
        assert error_code_of(SystemExit("x" * 65)) is None

    def test_a_number_is_not_a_code(self) -> None:
        class HTTPError(Exception):
            code = 503
            status = 503

        assert error_code_of(HTTPError()) is None

    def test_a_string_valued_enum_becomes_its_plain_value(self) -> None:
        class Reason(str, enum.Enum):
            THROTTLED = "throttled"

        class E(Exception):
            code = Reason.THROTTLED

        code = error_code_of(E())
        assert code == "throttled" and type(code) is str

    def test_an_attribute_that_raises_is_passed_over(self) -> None:
        class Odd(Exception):
            @property
            def code(self) -> str:
                raise RuntimeError("not loaded")

            type = "odd_failure"

        assert error_code_of(Odd()) == "odd_failure"

    @pytest.mark.parametrize("attribute", ["code", "body", "response"])
    def test_a_value_that_raises_when_checked_is_passed_over(self, attribute: str) -> None:
        odd = type("OddError", (Exception,), {attribute: _Proxy(), "status": "RESOURCE_EXHAUSTED"})
        assert error_code_of(odd()) == "RESOURCE_EXHAUSTED"

    def test_what_a_framework_hands_over_need_not_be_an_exception(self) -> None:
        assert error_category("boom") == {"error_type": "str"}
        assert error_category(None) == {"error_type": "NoneType"}


def test_a_traced_function_failing_on_a_provider_records_its_reason(mock_client: Any, capture_trace: Any) -> None:
    provider = _openai_error(429, "rate_limit_exceeded", message="Rate limit reached for gpt-4o-mini")

    @trace(mock_client, capture_config=CaptureConfig.standard())
    def answer() -> str:
        raise provider

    with pytest.raises(type(provider)):
        answer()

    payload = find_event(capture_trace["events"], "agent.error")["payload"]
    assert payload["error_type"] == "RateLimitError"
    assert payload["error_code"] == "rate_limit_exceeded"


def test_recording_an_odd_error_never_replaces_it(mock_client: Any, capture_trace: Any) -> None:
    # error_code_of runs while the traced call is failing: an error of its own would reach
    # the caller in place of theirs.
    odd = type("OddError", (Exception,), {"__cause__": property(lambda self: _Proxy())})

    @trace(mock_client, capture_config=CaptureConfig.standard())
    def answer() -> str:
        raise odd("x")

    # Caught by hand, not with pytest.raises: on a failure, pytest's report of an error
    # whose chain holds the proxy would itself fail on the proxy and stop the session.
    raised: Any = None
    try:
        answer()
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    assert type(raised) is odd

    payload = find_event(capture_trace["events"], "agent.error")["payload"]
    assert payload["error_type"] == "OddError"
    assert "error_code" not in payload


def test_the_reason_outlives_the_redaction_of_the_errors_text(mock_client: Any, capture_trace: Any) -> None:
    # The point of a category: capture_content=False strips the error's text, and the
    # reason is what is left to say why the call failed.
    provider = _openai_error(401, "invalid_api_key", message="Incorrect API key provided: sk-test")

    @trace(mock_client, capture_config=CaptureConfig.minimal())
    def answer() -> str:
        raise provider

    with pytest.raises(type(provider)):
        answer()

    payload = find_event(capture_trace["events"], "agent.error")["payload"]
    assert "error" not in payload
    assert payload["error_type"] == "AuthenticationError"
    assert payload["error_code"] == "invalid_api_key"


# ---------------------------------------------------------------------------
# Guard: every adapter takes an exception's category from error_category
# ---------------------------------------------------------------------------

INSTRUMENT = Path(__file__).resolve().parents[2] / "src" / "layerlens" / "instrument"


def _bare_class_names(tree: ast.AST) -> List[int]:
    """Lines that set ``error_type`` to ``type(x).__name__`` by hand."""

    def is_class_name(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "__name__"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "type"
        )

    def unwrap(node: ast.AST) -> ast.AST:
        return node.body if isinstance(node, ast.IfExp) else node

    lines: List[int] = []
    for node in ast.walk(tree):
        pairs: List[Tuple[Any, Any]] = []
        if isinstance(node, ast.Dict):
            pairs = list(zip(node.keys, node.values))
        elif isinstance(node, ast.Call):
            pairs = [(ast.Constant(kw.arg), kw.value) for kw in node.keywords if kw.arg]
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript):
            pairs = [(node.targets[0].slice, node.value)]
        for key, value in pairs:
            if isinstance(key, ast.Constant) and key.value == "error_type" and is_class_name(unwrap(value)):
                lines.append(getattr(node, "lineno", 0))
    return sorted(lines)


def test_every_adapter_takes_an_errors_category_from_error_category() -> None:
    # A site that sets error_type by hand records no error_code, and its traces lose the
    # provider's reason the next time an SDK rewords its errors.
    found = {
        str(path.relative_to(INSTRUMENT)): _bare_class_names(ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(INSTRUMENT.rglob("*.py"))
        if path.name != "_error_code.py"
    }
    assert {path: lines for path, lines in found.items() if lines} == {}


def test_the_guard_finds_a_hand_set_error_type() -> None:
    sample = (
        'p = {"error_type": type(e).__name__}\nf(error_type=type(e).__name__)\np["error_type"] = type(e).__name__\n'
    )
    assert _bare_class_names(ast.parse(sample)) == [1, 2, 3]
