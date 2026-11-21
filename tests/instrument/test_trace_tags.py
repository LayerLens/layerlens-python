"""Tags on the traces the SDK records: ``trace(tags=)``, ``trace_context(tags=)``, ``LAYERLENS_TRACE_TAGS`` (LAY-4449).

What a trace is tagged with is checked on the payload the SDK uploads, as the API reads it:
the top-level ``tags`` list of each uploaded trace.
"""

from __future__ import annotations

import re
import asyncio
import logging
import threading
import contextvars
from typing import Any, Dict, List

import pytest

from layerlens.instrument import (
    CaptureConfig,
    TraceCollector,
    emit,
    _tags,
    trace,
    trace_context,
    get_trace_context,
)
from layerlens._trace_tags import TRACE_TAGS_ENV
from layerlens.attestation._hash import compute_hash, event_hash_input
from layerlens.instrument.adapters.frameworks._base_framework import FrameworkAdapter


@pytest.fixture(autouse=True)
def _fresh_warnings() -> Any:
    # Each test sees its own warnings: a warning is given once per process.
    _tags._warned.clear()
    yield
    _tags._warned.clear()


class _Adapter(FrameworkAdapter):
    """A framework adapter as the real ones are built: one run, one collector of its own."""

    name = "stub"

    def run(self) -> None:
        self._begin_run()
        try:
            self._emit("agent.input", {"name": "stub-agent", "input": "q"})
        finally:
            self._end_run()


def _record(payload_events: Any = None) -> None:
    emit("tool.call", payload_events or {"tool_name": "lookup"})


def _tags_of(upload: Dict[str, Any]) -> List[str]:
    return list(upload.get("tags", []))


# ---------------------------------------------------------------------------
# Where tags come from
# ---------------------------------------------------------------------------


def test_trace_puts_its_tags_on_the_trace(mock_client: Any, capture_trace_list: List[Dict[str, Any]]) -> None:
    @trace(mock_client, name="support-agent", tags=["checkout", "v2"])
    def handle(question: str) -> str:
        return f"answer to {question}"

    handle("refund?")

    assert len(capture_trace_list) == 1
    assert capture_trace_list[0]["tags"] == ["checkout", "v2"]


def test_an_async_trace_puts_its_tags_on_the_trace(mock_client: Any, capture_trace_list: List[Dict[str, Any]]) -> None:
    @trace(mock_client, tags=["async"])
    async def handle() -> str:
        await asyncio.sleep(0)
        return "ok"

    assert asyncio.run(handle()) == "ok"
    assert capture_trace_list[0]["tags"] == ["async"]


def test_trace_context_puts_its_tags_on_the_trace(mock_client: Any, capture_trace_list: List[Dict[str, Any]]) -> None:
    with trace_context(mock_client, tags=["batch-7"]) as collector:
        _record()
        assert collector.tags == ["batch-7"]

    assert capture_trace_list[0]["tags"] == ["batch-7"]


def test_a_trace_with_no_tags_uploads_no_tags_key(mock_client: Any, capture_trace_list: List[Dict[str, Any]]) -> None:
    with trace_context(mock_client):
        _record()

    assert "tags" not in capture_trace_list[0]


def test_the_variable_tags_every_trace_the_process_records(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TRACE_TAGS_ENV, "ci,run-4449")

    trace(mock_client)(lambda: None)()
    with trace_context(mock_client):
        _record()
    _Adapter(mock_client).run()  # an adapter that opens its own trace
    collector = TraceCollector(mock_client, CaptureConfig())  # as openai_agents/llamaindex build theirs
    collector.emit("model.invoke", {"model": "m"}, span_id="s1")
    collector.flush()

    assert [_tags_of(u) for u in capture_trace_list] == [["ci", "run-4449"]] * 4


def test_the_variable_can_list_a_tag_with_a_comma_as_json(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TRACE_TAGS_ENV, '["ci", "release 1.2, hotfix"]')

    with trace_context(mock_client):
        _record()

    assert capture_trace_list[0]["tags"] == ["ci", "release 1.2, hotfix"]


def test_tags_merge_variable_first_then_enclosing_blocks_then_the_traces_own(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TRACE_TAGS_ENV, "ci, shared")

    @trace(mock_client, tags=["inner", "shared", "outer"])
    def step() -> None:
        _record()

    with trace_context(mock_client, tags=["outer"]):
        _record()
        step()

    inner, outer = capture_trace_list  # the inner trace flushes first
    # A repeat keeps its first place: "shared" stays where the variable put it.
    assert inner["tags"] == ["ci", "shared", "outer", "inner"]
    assert outer["tags"] == ["ci", "shared", "outer"]


def test_a_trace_an_adapter_opens_inside_a_block_carries_the_blocks_tags(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    # openai_agents, llamaindex and openinference open a trace of their own even inside
    # trace_context: the block's tags still reach it.
    with trace_context(mock_client, tags=["experiment-12"]):
        own = TraceCollector(mock_client, CaptureConfig())
        own.emit("model.invoke", {"model": "m"}, span_id="s1")
        own.flush()

    assert capture_trace_list[0]["tags"] == ["experiment-12"]


def test_an_adapter_run_inside_trace_context_joins_its_trace_and_tags(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    with trace_context(mock_client, tags=["joined"]):
        _Adapter(mock_client).run()

    assert len(capture_trace_list) == 1
    assert capture_trace_list[0]["tags"] == ["joined"]


def test_a_blocks_tags_end_with_the_block(mock_client: Any, capture_trace_list: List[Dict[str, Any]]) -> None:
    with trace_context(mock_client, tags=["inside"]):
        _record()
    after = TraceCollector(mock_client, CaptureConfig())

    assert after.tags == []


def test_concurrent_tasks_keep_their_own_tags(mock_client: Any, capture_trace_list: List[Dict[str, Any]]) -> None:
    def traced(tag: str) -> Any:
        @trace(mock_client, name=tag, tags=[tag])
        async def work() -> None:
            for _ in range(3):
                await asyncio.sleep(0)
                # A trace another task opens mid-flight sees only its own task's block.
                inner = TraceCollector(mock_client, CaptureConfig())
                assert inner.tags == [tag]

        return work()

    async def main() -> None:
        await asyncio.gather(*(traced(f"task-{i}") for i in range(5)))

    asyncio.run(main())
    assert sorted(u["tags"][0] for u in capture_trace_list) == [f"task-{i}" for i in range(5)]
    assert all(len(u["tags"]) == 1 for u in capture_trace_list)


def test_a_thread_started_with_the_blocks_context_carries_its_tags(mock_client: Any) -> None:
    seen: List[List[str]] = []
    with trace_context(mock_client, tags=["worker"]):
        context = contextvars.copy_context()
        thread = threading.Thread(
            target=context.run, args=(lambda: seen.append(TraceCollector(mock_client, CaptureConfig()).tags),)
        )
        thread.start()
        thread.join()

    assert seen == [["worker"]]


def test_tags_are_not_handed_across_a_process_boundary(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    # get_trace_context() carries the trace's identity, not its labels: a caller cannot tag
    # the receiver's traces, which grader rules may act on.
    with trace_context(mock_client, tags=["caller"]):
        _record()
        handed_over = get_trace_context()
    assert handed_over is not None and "tags" not in handed_over

    with trace_context(mock_client, from_context=handed_over, tags=["receiver"]):
        _record()

    assert capture_trace_list[1]["trace_id"] == capture_trace_list[0]["trace_id"]
    assert capture_trace_list[1]["tags"] == ["receiver"]


def test_a_traces_tags_are_fixed_when_it_starts(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TRACE_TAGS_ENV, "before")
    with trace_context(mock_client):
        monkeypatch.setenv(TRACE_TAGS_ENV, "after")
        _record()

    assert capture_trace_list[0]["tags"] == ["before"]


def test_collector_tags_is_a_copy(mock_client: Any, capture_trace_list: List[Dict[str, Any]]) -> None:
    with trace_context(mock_client, tags=["a"]) as collector:
        collector.tags.append("b")
        _record()

    assert capture_trace_list[0]["tags"] == ["a"]


def test_the_callers_list_can_change_without_changing_the_trace(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    tags = ["a"]

    @trace(mock_client, tags=tags)
    def handle() -> None:
        _record()

    tags.append("b")
    handle()

    assert capture_trace_list[0]["tags"] == ["a"]


# ---------------------------------------------------------------------------
# What a tag may be
# ---------------------------------------------------------------------------


def test_tags_are_trimmed_and_blanks_and_repeats_dropped(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The API's tag filter trims what it is asked for: an untrimmed tag could never be found.
    monkeypatch.setenv(TRACE_TAGS_ENV, " ci ,, ci ,")
    with trace_context(mock_client, tags=["  nightly", "", "   ", "nightly ", "ci"]):
        _record()

    assert capture_trace_list[0]["tags"] == ["ci", "nightly"]


def test_a_secret_in_a_tag_is_redacted_and_never_logged(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    secret = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    with caplog.at_level(logging.WARNING, logger="layerlens.instrument._tags"):
        with trace_context(mock_client, tags=[f"deploy-{secret}", "ok"]):
            _record()

    assert capture_trace_list[0]["tags"] == ["deploy-[REDACTED-SECRET]", "ok"]
    assert "secret-shaped" in caplog.text
    assert secret not in caplog.text


@pytest.mark.parametrize(
    "bad",
    [
        "line\nbreak",
        "tab\there",
        "\u202eevil.exe",  # right-to-left override: spoofs how the tag reads
        "zero\u200bwidth",
        "line\u2028separator",
        "nul\x00byte",
        "bad-utf8-\udcff",  # a byte the environment's UTF-8 decoding could not read
    ],
)
def test_a_tag_with_a_control_or_format_character_is_dropped_with_a_warning(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]], caplog: pytest.LogCaptureFixture, bad: str
) -> None:
    with caplog.at_level(logging.WARNING, logger="layerlens.instrument._tags"):
        with trace_context(mock_client, tags=[bad, "kept"]):
            _record()

    assert capture_trace_list[0]["tags"] == ["kept"]
    assert "control or format character" in caplog.text


def test_unicode_letters_symbols_and_spaces_are_kept(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    tags = ["release 1.2", "café", "日本語", "\U0001f680 launch", "agent:support", "a, b"]
    with trace_context(mock_client, tags=tags):
        _record()

    assert capture_trace_list[0]["tags"] == tags


def test_a_tag_over_the_length_limit_is_dropped_not_cut(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    at_limit = "x" * _tags.MAX_TAG_BYTES
    over = "y" * (_tags.MAX_TAG_BYTES + 1)
    with caplog.at_level(logging.WARNING, logger="layerlens.instrument._tags"):
        with trace_context(mock_client, tags=[at_limit, over]):
            _record()

    # A cut tag would be a value nobody chose, and no filter for the real one would find it.
    assert capture_trace_list[0]["tags"] == [at_limit]
    assert f"{_tags.MAX_TAG_BYTES + 1} bytes" in caplog.text


def test_the_limit_counts_utf8_bytes_as_the_platform_does(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]]
) -> None:
    fits = "日" * (_tags.MAX_TAG_BYTES // 3)  # 3 bytes each
    over = "本" * (_tags.MAX_TAG_BYTES // 3 + 1)
    with trace_context(mock_client, tags=[fits, over]):
        _record()

    assert capture_trace_list[0]["tags"] == [fits]


def test_the_longest_run_label_is_a_tag(mock_client: Any, capture_trace_list: List[Dict[str, Any]]) -> None:
    label = "l" * 128  # the API's limit for a run's --label, which tags the run's traces
    with trace_context(mock_client, tags=[label]):
        _record()

    assert capture_trace_list[0]["tags"] == [label]


def test_tags_past_the_count_limit_are_dropped_and_the_variables_kept(
    mock_client: Any,
    capture_trace_list: List[Dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TRACE_TAGS_ENV, "ci-run")
    own = [f"t{i}" for i in range(_tags.MAX_TAGS + 10)]
    with caplog.at_level(logging.WARNING, logger="layerlens.instrument._tags"):
        with trace_context(mock_client, tags=own):
            _record()

    sent = capture_trace_list[0]["tags"]
    assert len(sent) == _tags.MAX_TAGS
    # The variable's tags come first, so CI can always find the traces of its run.
    assert sent[0] == "ci-run" and sent[1:] == own[: _tags.MAX_TAGS - 1]
    assert f"only the first {_tags.MAX_TAGS}" in caplog.text


@pytest.mark.parametrize(
    "tags, message",
    [
        ("ci", "tags is a list of tags, not one string: pass tags=[...]"),
        (b"ci", "tags is a list of tags, not one string: pass tags=[...]"),
        (["ci", 7], "each tag is a string, got int"),
        ([None], "each tag is a string, got NoneType"),
        (5, "tags is a list of strings, got int"),
    ],
)
def test_tags_that_are_not_a_list_of_strings_raise_where_they_are_given(
    mock_client: Any, tags: Any, message: str
) -> None:
    with pytest.raises(TypeError, match=re.escape(message)):
        trace(mock_client, tags=tags)  # at decoration, before anything runs
    with pytest.raises(TypeError, match=re.escape(message)):
        with trace_context(mock_client, tags=tags):
            pass  # pragma: no cover - never entered
    with pytest.raises(TypeError, match=re.escape(message)):
        TraceCollector(mock_client, CaptureConfig(), tags=tags)


def test_a_tuple_or_generator_of_tags_is_fine(mock_client: Any, capture_trace_list: List[Dict[str, Any]]) -> None:
    with trace_context(mock_client, tags=("a", "b")):
        _record()
    with trace_context(mock_client, tags=(t for t in ["c"])):
        _record()

    assert [u["tags"] for u in capture_trace_list] == [["a", "b"], ["c"]]


def test_a_variable_that_opens_with_a_bracket_but_is_not_json_tags_nothing_and_says_so(
    mock_client: Any,
    capture_trace_list: List[Dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TRACE_TAGS_ENV, "[ci, nightly]")
    with caplog.at_level(logging.WARNING, logger="layerlens.instrument._tags"):
        with trace_context(mock_client, tags=["own"]):
            _record()

    assert capture_trace_list[0]["tags"] == ["own"]
    assert "LAYERLENS_TRACE_TAGS starts with '[' but is not a JSON array of strings" in caplog.text


def test_a_bad_variable_is_reported_once_not_per_trace(
    mock_client: Any,
    capture_trace_list: List[Dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TRACE_TAGS_ENV, '["ci"')
    with caplog.at_level(logging.WARNING, logger="layerlens.instrument._tags"):
        for _ in range(20):
            with trace_context(mock_client):
                _record()

    assert len(capture_trace_list) == 20
    assert caplog.text.count("is not a JSON array of strings") == 1


def test_a_tag_dropped_from_every_trace_is_reported_once(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    # A request handler that tags each trace the same bad way must not log once per request.
    with caplog.at_level(logging.WARNING, logger="layerlens.instrument._tags"):
        for _ in range(20):
            with trace_context(mock_client, tags=["tab\there", "ok"]):
                _record()

    assert [u["tags"] for u in capture_trace_list] == [["ok"]] * 20
    assert caplog.text.count("control or format character") == 1


# The tags the platform keeps, as atlas-app's apps/shared/tracetags tests them with the same
# table (TestAppendAgreesWithTheSDK): what the SDK sends is what the API stores.
PLATFORM_VECTORS = [
    (["  ci  "], ["ci"]),
    (["\u00a0nbsp\u3000"], ["nbsp"]),  # Unicode spaces trim, as Go's strings.TrimSpace does
    (["a\x1c"], []),  # U+001C: Python's strip() would trim it, Go's does not; a control either way
    (["dup", "dup", "", "   "], ["dup"]),
    (["x" * 128, "y" * 129], ["x" * 128]),
    (["\u65e5" * 42, "\u672c" * 43], ["\u65e5" * 42]),  # 126 bytes kept, 129 dropped
    (["line\nbreak", "\u202eevil", "zero\u200bwidth", "line\u2028sep", "ok"], ["ok"]),
    (["release 1.2, hotfix", "caf\u00e9"], ["release 1.2, hotfix", "caf\u00e9"]),
    ([f"t{i}" for i in range(40)], [f"t{i}" for i in range(32)]),
]


@pytest.mark.parametrize("given, kept", PLATFORM_VECTORS)
def test_the_sdk_keeps_the_tags_the_platform_keeps(given: List[str], kept: List[str]) -> None:
    assert list(_tags.tags_argument(given)) == kept


def test_a_block_exited_in_another_context_still_closes() -> None:
    # Some frameworks run a callback in a copy of the context; closing a block there must not
    # raise and skip the rest of its cleanup (the span, the collector, the flush).
    token = contextvars.copy_context().run(_tags._push_scope, ("elsewhere",))
    _tags._pop_scope(token)

    assert _tags._scope.get() == ()


# ---------------------------------------------------------------------------
# What tags are not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config", [CaptureConfig.minimal(), CaptureConfig(capture_content=False), CaptureConfig.full()]
)
def test_tags_are_sent_whatever_the_capture_config(
    mock_client: Any, capture_trace_list: List[Dict[str, Any]], config: CaptureConfig
) -> None:
    # Tags are what the caller declares, like a trace's name, not what the SDK observes:
    # capture settings do not gate them; the secret scrub does apply.
    with trace_context(mock_client, capture_config=config, tags=["declared"]):
        emit("agent.error", {"error": "boom", "error_type": "RuntimeError"})

    assert capture_trace_list[0]["tags"] == ["declared"]


def test_tags_sit_outside_the_attestation_chain(mock_client: Any, capture_trace_list: List[Dict[str, Any]]) -> None:
    with trace_context(mock_client, tags=["label"]):
        _record()
        _record({"tool_name": "second"})

    upload = capture_trace_list[0]
    events = upload["events"]
    # Every event hash recomputes from the event alone, and the root from the event hashes:
    # the tags feed neither, so adding a tag later (as the app lets people) changes no proof.
    for event in events:
        assert "tags" not in event
        assert compute_hash(event_hash_input(event, event["previous_hash"])) == event["hash"]
    assert upload["attestation"]["root_hash"] == compute_hash({"event_hashes": [e["hash"] for e in events]})


def test_replay_snapshots_keep_the_tags(mock_client: Any) -> None:
    with trace_context(mock_client, tags=["snapshot"]) as collector:
        _record()
        snapshot = collector.to_replay_dict()

    assert snapshot["tags"] == ["snapshot"]
