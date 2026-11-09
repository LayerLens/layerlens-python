"""The tags the SDK puts on the traces it records (LAY-4449).

A trace's tags come from three places, merged in this order:

1. ``LAYERLENS_TRACE_TAGS``, which tags every trace the process records (CI tags a job's
   traces this way, without changing the agent);
2. the ``tags=`` of each ``trace(...)`` and ``trace_context(...)`` block the trace starts
   in, outermost first, including a trace an adapter opens of its own inside the block;
3. the ``tags=`` of the trace's own ``trace(...)``, ``trace_context(...)`` or
   ``TraceCollector(...)``.

Every tag is trimmed, as the API's tag filter trims what it is asked for, and secret-scrubbed
like event text. Blanks and repeats are dropped; a repeat keeps its first place, so the
variable's tags lead. A tag with a control or format character (a newline, a right-to-left
override, a zero-width space), or over :data:`MAX_TAG_BYTES` bytes, is dropped with a
warning: cut short, it would be a value nobody chose, which no filter for the real one
finds. Past :data:`MAX_TAGS`, the rest are dropped with a warning. None of this raises:
tracing must not break the program it observes. Only a ``tags=`` that is not a list of
strings raises ``TypeError``, where it is given.

Tags are labels, not evidence. They ride beside the attestation chain, not in it (the app
lets people add tags to a trace later), and they are sent whatever the capture config,
like a trace's name: put no content or personal data in them.
"""

from __future__ import annotations

import os
import logging
import threading
import unicodedata
from typing import Any, List, Tuple, Iterable, Optional
from collections import OrderedDict
from contextvars import Token, ContextVar

from .._trace_tags import TRACE_TAGS_ENV, split_tags
from ._secret_scrub import scrub_secrets

log: logging.Logger = logging.getLogger(__name__)

# The platform's own limits for a trace's tags, which its OTLP ingest already applies: a
# trace is one database record, and the traces list builds its tag filter from every
# distinct tag, so no sender adds tags without bound.
#: Tags one trace carries at most.
MAX_TAGS = 32
#: UTF-8 bytes one tag holds at most; a run's ``--label``, which tags its traces, fits.
MAX_TAG_BYTES = 128

Tags = Tuple[str, ...]

# What the API's tag filter trims (Go's strings.TrimSpace): Unicode's White_Space characters.
# Python's str.strip() trims more (U+001C..U+001F), which the API instead refuses as controls.
_WHITE_SPACE = "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"

# Unicode categories a tag may not hold anywhere: controls (Cc), format characters such as
# bidi overrides and zero-width spaces (Cf), surrogates, which is what bytes that are not
# UTF-8 become when the environment is decoded (Cs), and line and paragraph separators.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

# The tags of the trace(...) and trace_context(...) blocks running in this context.
_scope: ContextVar[Tags] = ContextVar("layerlens_trace_tags", default=())


def tags_argument(tags: Optional[Iterable[str]], *, source: str = "tags=") -> Tags:
    """A ``tags=`` argument, checked and normalized. ``None`` is no tags.

    Raises ``TypeError`` for one string, which would otherwise be read as its letters, and
    for anything but strings in it.
    """
    if tags is None:
        return ()
    if isinstance(tags, (str, bytes, bytearray)):
        raise TypeError("tags is a list of tags, not one string: pass tags=[...]")
    try:
        items = list(tags)
    except TypeError:
        raise TypeError(f"tags is a list of strings, got {type(tags).__name__}") from None
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"each tag is a string, got {type(item).__name__}")
    return _normalize(items, source)


def env_tags() -> Tags:
    """The tags ``LAYERLENS_TRACE_TAGS`` puts on every trace, normalized.

    Read as each trace starts; a problem with the value is reported once, not per trace.
    """
    try:
        written = split_tags(os.environ.get(TRACE_TAGS_ENV, ""))
    except ValueError as err:
        _warn_once("layerlens: %s; no trace is tagged from it", err)
        return ()
    return _normalize(written, TRACE_TAGS_ENV)


def trace_tags(own: Tags = ()) -> Tags:
    """The tags of a trace starting here: the variable's, the enclosing blocks', then ``own``."""
    return merge(env_tags(), _scope.get(), own)


def merge(*sources: Tags) -> Tags:
    """``sources`` one after another, without repeats (the first keeps its place), at most :data:`MAX_TAGS`."""
    merged: List[str] = []
    seen = set()
    for source in sources:
        for tag in source:
            if tag not in seen:
                seen.add(tag)
                merged.append(tag)
    if len(merged) > MAX_TAGS:
        _warn_once("layerlens: a trace has %d tags; only the first %d are sent", len(merged), MAX_TAGS)
        del merged[MAX_TAGS:]
    return tuple(merged)


def _push_scope(own: Tags) -> Token[Tags]:
    """Open a block whose traces, and the traces started inside it, carry ``own``."""
    return _scope.set(merge(_scope.get(), own))


def _pop_scope(token: Token[Tags]) -> None:
    """Close the block ``token`` opened. Never raises, so the cleanup after it always runs.

    A block exited in another context than it was entered in (a framework that copies
    contexts between callbacks) cannot reset there; the tags it found are set back instead.
    """
    try:
        _scope.reset(token)
    except ValueError:
        previous = token.old_value
        _scope.set(() if previous is Token.MISSING else previous)


def _normalize(written: Iterable[str], source: str) -> Tags:
    """``written`` trimmed, scrubbed and checked, without blanks or repeats; at most :data:`MAX_TAGS`."""
    tags: List[str] = []
    seen = set()
    for raw in written:
        tag = _clean(raw, source)
        if tag is None or tag in seen:
            continue
        if len(tags) == MAX_TAGS:
            _warn_once("layerlens: %s holds more than %d tags; only the first %d are sent", source, MAX_TAGS, MAX_TAGS)
            break
        seen.add(tag)
        tags.append(tag)
    return tuple(tags)


def _clean(raw: str, source: str) -> Optional[str]:
    """One tag as sent, or ``None`` when it is blank or unfit to send."""
    tag = raw.strip(_WHITE_SPACE)
    if not tag:
        return None
    # Scrubbed first, so no warning below can print a secret.
    scrubbed = scrub_secrets(tag)
    if scrubbed != tag:
        _warn_once("layerlens: a tag from %s held a secret-shaped value; it is sent redacted", source)
        tag = scrubbed
    for char in tag:
        if unicodedata.category(char) in _FORBIDDEN_CATEGORIES:
            _warn_once(
                "layerlens: dropped the tag %s from %s: it holds the control or format character U+%04X",
                _preview(tag),
                source,
                ord(char),
            )
            return None
    size = len(tag.encode("utf-8"))  # encodable: no surrogate is left
    if size > MAX_TAG_BYTES:
        _warn_once(
            "layerlens: dropped a tag of %d bytes from %s, over the limit of %d: %s",
            size,
            source,
            MAX_TAG_BYTES,
            _preview(tag),
        )
        return None
    return tag


def _preview(tag: str) -> str:
    """The start of ``tag``, escaped so a control character cannot rewrite the log line."""
    return ascii(tag[:40]) + ("..." if len(tag) > 40 else "")


# Warnings already given, so a tag or variable that is wrong for every trace is reported
# once, not once per trace. Bounded: the oldest are forgotten first.
_WARNED_MAX = 256
_warned: "OrderedDict[str, None]" = OrderedDict()
_warned_lock = threading.Lock()


def _warn_once(message: str, *args: Any) -> None:
    text = message % args
    with _warned_lock:
        if text in _warned:
            _warned.move_to_end(text)
            return
        _warned[text] = None
        if len(_warned) > _WARNED_MAX:
            _warned.popitem(last=False)
    log.warning("%s", text)
