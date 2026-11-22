"""``LAYERLENS_TRACE_TAGS``: the tags every trace a process records carries.

The SDK's tracing reads it (:mod:`layerlens.instrument`); ``layerlens env run exec`` writes it
for the agent it runs, adding the run's ``--label``. Like ``LAYERLENS_TRACE_ID_FILE``, it lets
CI tag an agent's traces without changing the agent.

A value is a comma-separated list, ``ci,nightly``, or a JSON array of strings,
``["ci", "release 1.2, hotfix"]``, which can hold a tag with a comma in it. A value whose first
character, after any whitespace, is ``[`` is JSON; any other is comma-separated.

This module is the format alone: trimming each tag, dropping blanks and repeats, and checking
a tag is fit to send are the tracer's (``layerlens.instrument._tags``).
"""

from __future__ import annotations

import json
from typing import List, Sequence

TRACE_TAGS_ENV = "LAYERLENS_TRACE_TAGS"


def split_tags(value: str) -> List[str]:
    """The tags ``value`` lists, as written: not trimmed, and blanks kept.

    Raises ``ValueError`` when ``value`` opens with ``[`` but is not a JSON array of strings.
    Read as comma-separated instead, ``[ci, nightly]`` would tag traces ``[ci`` and ``nightly]``.
    """
    if not value.strip():
        return []
    if value.lstrip().startswith("["):
        try:
            tags = json.loads(value)
        except (ValueError, RecursionError):  # a value nested past the parser's limit raises RecursionError
            tags = None
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError(
                f"{TRACE_TAGS_ENV} starts with '[' but is not a JSON array of strings; "
                'write ["ci", "nightly"], or ci,nightly'
            )
        return tags
    return value.split(",")


def join_tags(tags: Sequence[str]) -> str:
    """A ``LAYERLENS_TRACE_TAGS`` value that :func:`split_tags` reads back as ``tags``.

    Comma-separated, unless a tag holds a comma, or the first opens with ``[``: then a JSON array.
    The tags are expected trimmed and non-blank, as the tracer keeps them.
    """
    if any("," in tag for tag in tags) or (tags and tags[0].lstrip().startswith("[")):
        return json.dumps(list(tags), ensure_ascii=False)
    return ",".join(tags)
