"""``LAYERLENS_TRACE_TAGS`` as written and read: the value ``env run exec`` hands the SDK (LAY-4449)."""

from __future__ import annotations

import json
import random
from typing import List

import pytest

from layerlens._trace_tags import TRACE_TAGS_ENV, join_tags, split_tags


def test_the_variable_is_named_for_trace_tags() -> None:
    assert TRACE_TAGS_ENV == "LAYERLENS_TRACE_TAGS"


@pytest.mark.parametrize(
    "value, tags",
    [
        ("", []),
        ("   ", []),
        ("ci", ["ci"]),
        ("ci,nightly", ["ci", "nightly"]),
        # As written: trimming and dropping blanks are the reader's job, not the format's.
        (" ci , nightly ,", [" ci ", " nightly ", ""]),
        ("agent:support,git_sha:3f2c1ab", ["agent:support", "git_sha:3f2c1ab"]),
        ('["ci", "release 1.2, hotfix"]', ["ci", "release 1.2, hotfix"]),
        ('  ["ci"]  ', ["ci"]),
        ("[]", []),
    ],
)
def test_a_value_lists_its_tags_comma_separated_or_as_a_json_array(value: str, tags: List[str]) -> None:
    assert split_tags(value) == tags


@pytest.mark.parametrize(
    "value",
    [
        "[ci, nightly]",  # YAML's flow list, not JSON
        '["ci"',
        "[1, 2]",
        '["ci", null]',
        '[["ci"]]',
        # Nested past the parser's recursion limit: json raises RecursionError, not ValueError.
        pytest.param("[" * 100_000, id="nested-past-the-recursion-limit"),
    ],
)
def test_a_value_that_opens_with_a_bracket_must_be_a_json_array_of_strings(value: str) -> None:
    # Read any other way, "[ci, nightly]" would become the tags "[ci" and "nightly]".
    with pytest.raises(ValueError, match=r"LAYERLENS_TRACE_TAGS starts with '\[' but is not a JSON array of strings"):
        split_tags(value)


def test_only_a_leading_bracket_makes_a_value_json() -> None:
    assert split_tags('{"env": "ci"}') == ['{"env": "ci"}']
    assert split_tags("ci,[wip]") == ["ci", "[wip]"]


@pytest.mark.parametrize(
    "tags, value",
    [
        ([], ""),
        (["ci"], "ci"),
        (["ci", "nightly"], "ci,nightly"),
        (["agent:support", "git_sha:3f2c1ab"], "agent:support,git_sha:3f2c1ab"),
        # A tag with a comma, or a first tag that opens with "[", only reads back as itself in JSON.
        (["ci", "release 1.2, hotfix"], '["ci", "release 1.2, hotfix"]'),
        (["[wip]", "ci"], '["[wip]", "ci"]'),
        (["ci", "[wip]"], "ci,[wip]"),
        (["café", "日本"], "café,日本"),
        (["café", "a,b"], '["café", "a,b"]'),
    ],
)
def test_tags_are_written_comma_separated_unless_only_json_reads_them_back(tags: List[str], value: str) -> None:
    assert join_tags(tags) == value
    assert split_tags(value) == tags


# Characters that a naive format would get wrong: separators, JSON syntax, escapes, quotes,
# non-ASCII and astral code points.
_TRICKY = list(",[]{}\"'\\:; =-_./|") + ["é", "日", "\U0001f680", "a", "b", "Z", "0", "9"]


def _random_tag(rng: random.Random) -> str:
    tag = "".join(rng.choice(_TRICKY) for _ in range(rng.randint(1, 12)))
    # Tags reach the variable trimmed and non-blank (the SDK and exec both normalize first).
    return tag.strip() or "x"


def test_any_list_of_trimmed_non_blank_tags_reads_back_as_written() -> None:
    rng = random.Random(4449)
    for _ in range(5000):
        tags = [_random_tag(rng) for _ in range(rng.randint(1, 6))]
        value = join_tags(tags)
        assert split_tags(value) == tags, (tags, value)
        if value.startswith("["):
            assert json.loads(value) == tags
