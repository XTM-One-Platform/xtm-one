"""The agent loop's hot path does less work, and gives the same answers.

Three costs a long run paid on every step, measured on main (an 80-iteration
traced tool run over a fake provider):

* ``strip_null_bytes``, which sanitizes every trace payload (each generation
  carries the whole transcript) and every ORM JSON column write, made one
  recursive call per scalar leaf - about half of the loop's CPU time;
* the Gemini provider-extras counter serialized every earlier function call's
  arguments again on each pass of the loop's context gates - about 30% of a
  long Gemini run;
* ``_stream_text_chunked`` slept after its last chunk, a wait with nothing
  left to pace before the turn could finish.

Pinned here: the same results (and, for ``strip_null_bytes``, the same objects)
as before, with the work gone.
"""

from __future__ import annotations

import random
from itertools import islice
from typing import Any

import pytest
from google.genai import types

from app.core.utils import strip_null_bytes
from app.llm.base import ChatMessage, message_payload_chars
from app.llm.gemini_provider import GEMINI_PARTS_KEY, measure_gemini_parts

# ---------------------------------------------------------------------------
# strip_null_bytes: the same answer, object for object
# ---------------------------------------------------------------------------


def _reference_strip_null_bytes(data: Any) -> Any:
    """``strip_null_bytes`` as it was before the scalar fast path, verbatim."""
    if isinstance(data, str):
        return data.replace("\u0000", "") if "\u0000" in data else data
    if isinstance(data, dict):
        out: dict | None = None
        for idx, (key, value) in enumerate(data.items()):
            new_key = (
                key.replace("\u0000", "")
                if isinstance(key, str) and "\u0000" in key
                else key
            )
            new_value = _reference_strip_null_bytes(value)
            if out is None:
                if new_key is key and new_value is value:
                    continue
                out = dict(islice(data.items(), idx))
            out[new_key] = new_value
        return data if out is None else out
    if isinstance(data, tuple):
        return [_reference_strip_null_bytes(item) for item in data]
    if isinstance(data, list):
        items: list | None = None
        for idx, item in enumerate(data):
            new_item = _reference_strip_null_bytes(item)
            if items is None:
                if new_item is item:
                    continue
                items = data[:idx]
            items.append(new_item)
        return data if items is None else items
    return data


class _Str(str):
    pass


class _Int(int):
    pass


class _Opaque:
    """Not JSON: passed through untouched, whatever it holds."""

    def __init__(self) -> None:
        self.text = "kept\u0000as is"


def _same_shape(left: Any, right: Any) -> bool:
    """Equal values of the same types, the same objects where one is kept."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return list(left) == list(right) and all(_same_shape(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_shape(a, b) for a, b in zip(left, right, strict=True))
    return left == right if not isinstance(left, _Opaque) else left is right


_EDGE_CASES = [
    "plain",
    "with\u0000nul",
    "",
    0,
    -3,
    2.5,
    float("inf"),
    True,
    False,
    None,
    b"bytes\x00kept",
    _Str("sub\u0000class"),
    _Str("subclass clean"),
    _Int(7),
    _Opaque(),
    (1, "a\u0000", ("nested", None)),
    [1, "a", None, True, 2.0, ["deep\u0000"]],
    {"k": "v", "n": 1, "b": False, "none": None, "f": 1.5},
    {"k\u0000ey": "v", "nested": {"x": ["y\u0000", 3]}},
    {1: "int key", (1, 2): "tuple key\u0000", None: [None, "z"]},
    {"messages": [{"role": "user", "content": "hi", "tool_calls": [{"arguments": {"q": 1}}]}]},
]


@pytest.mark.parametrize("payload", _EDGE_CASES, ids=[f"case{i}" for i in range(len(_EDGE_CASES))])
def test_strip_null_bytes_answers_exactly_as_before(payload):
    expected = _reference_strip_null_bytes(payload)
    got = strip_null_bytes(payload)

    assert _same_shape(got, expected)
    # Copy-on-write is kept: a payload left alone is the very same object.
    assert (got is payload) == (expected is payload)


def _random_payload(rng: random.Random, depth: int = 0) -> Any:
    leaf = rng.random() < (0.35 + 0.15 * depth)
    if leaf:
        kind = rng.randrange(9)
        if kind == 0:
            return "t" * rng.randrange(5) + ("\u0000" if rng.random() < 0.1 else "") + "x"
        if kind == 1:
            return rng.randrange(-5, 5)
        if kind == 2:
            return rng.random()
        if kind == 3:
            return rng.random() < 0.5
        if kind == 4:
            return None
        if kind == 5:
            return _Str("s\u0000" if rng.random() < 0.3 else "s")
        if kind == 6:
            return _Int(rng.randrange(3))
        if kind == 7:
            return b"\x00"
        return "clean string"
    kind = rng.randrange(3)
    size = rng.randrange(5)
    if kind == 0:
        return [_random_payload(rng, depth + 1) for _ in range(size)]
    if kind == 1:
        return tuple(_random_payload(rng, depth + 1) for _ in range(size))
    return {
        (f"k{i}\u0000" if rng.random() < 0.1 else f"k{i}"): _random_payload(rng, depth + 1)
        for i in range(size)
    }


def test_strip_null_bytes_matches_the_reference_on_a_random_corpus():
    rng = random.Random(4100)  # noqa: S311 - a seeded, reproducible corpus
    for _ in range(3_000):
        payload = _random_payload(rng)
        expected = _reference_strip_null_bytes(payload)
        got = strip_null_bytes(payload)
        assert _same_shape(got, expected), payload
        assert (got is payload) == (expected is payload), payload


@pytest.mark.parametrize("entry", ["strip_null_bytes", "sanitize_json_strings"])
def test_a_clean_payload_is_walked_without_a_call_per_scalar(monkeypatch, entry):
    """The work that is gone: scalar leaves are no longer recursive calls.

    Both sanitizers share one walk; a clean string, non-ASCII included,
    is decided inline too.
    """
    from app.core import utils

    calls = 0
    real = utils._sanitize_strings

    def _counting(data, surrogates):
        nonlocal calls
        calls += 1
        return real(data, surrogates)

    monkeypatch.setattr(utils, "_sanitize_strings", _counting)
    payload = {"messages": [{"role": "assistant", "content": "x", "n": i, "ok": True} for i in range(50)]}

    assert getattr(utils, entry)(payload) is payload
    # The top-level dict, its list and each message dict - never a leaf.
    assert calls == 1 + 1 + 50


# ---------------------------------------------------------------------------
# The Gemini replay measure: taken once per parts list
# ---------------------------------------------------------------------------


def _gemini_turn(q: int) -> ChatMessage:
    parts = [
        types.Part(text="thinking " * 20, thought=True),
        types.Part(
            function_call=types.FunctionCall(name="lookup", args={"q": q, "fields": ["a", "b"] * 10}),
            thought_signature=b"\x8f" * 300,
        ),
    ]
    return ChatMessage(role="assistant", content="", _provider_extra={GEMINI_PARTS_KEY: parts})


def _expected_weight(message: ChatMessage) -> int:
    """``len(content)`` plus what the parts add, never below the content."""
    content = len(message.content or "")
    text_chars, _images, _native = measure_gemini_parts(message._provider_extra[GEMINI_PARTS_KEY])
    return content + max(0, text_chars - content)


def test_a_gemini_turn_weighs_what_a_fresh_measure_says():
    message = _gemini_turn(1)

    first = message_payload_chars(message)
    again = message_payload_chars(message)

    assert first == again == _expected_weight(message)


def test_the_parts_are_measured_once_across_passes(monkeypatch):
    from app.llm import gemini_provider

    measured = 0
    real = gemini_provider.measure_gemini_parts

    def _counting(parts):
        nonlocal measured
        measured += 1
        return real(parts)

    monkeypatch.setattr(gemini_provider, "measure_gemini_parts", _counting)
    transcript = [_gemini_turn(q) for q in range(10)]

    weights = [sum(message_payload_chars(m) for m in transcript) for _ in range(6)]

    assert len(set(weights)) == 1
    assert measured == 10


@pytest.mark.parametrize("change", ["append", "replace_element", "replace_list", "remove"])
def test_any_change_to_the_parts_is_measured_again(change):
    message = _gemini_turn(2)
    message_payload_chars(message)
    parts = message._provider_extra[GEMINI_PARTS_KEY]
    extra = types.Part(text="a longer follow-up thought " * 5, thought=True)
    if change == "append":
        parts.append(extra)
    elif change == "replace_element":
        parts[0] = extra
    elif change == "replace_list":
        message._provider_extra[GEMINI_PARTS_KEY] = [extra, *parts]
    else:
        parts.pop(0)

    assert message_payload_chars(message) == _expected_weight(message)


def test_the_content_is_read_live_every_time():
    """A gate that truncates the content in place is re-read at once: the
    parts' measure is kept, never the message's weight."""
    message = _gemini_turn(3)
    message.content = "x" * 50_000
    large = message_payload_chars(message)

    message.content = "x"

    assert large == 50_000
    assert message_payload_chars(message) == _expected_weight(message)


def test_a_message_without_an_instance_dict_is_measured_as_before():
    from app.llm.gemini_provider import _gemini_parts_extra_chars

    class _Slotted:
        __slots__ = ("content",)

        def __init__(self) -> None:
            self.content = ""

    parts = _gemini_turn(4)._provider_extra[GEMINI_PARTS_KEY]
    message = _Slotted()

    assert _gemini_parts_extra_chars(message, parts) == measure_gemini_parts(parts)[0]
    assert _gemini_parts_extra_chars(message, parts) == measure_gemini_parts(parts)[0]


def test_a_copied_message_keeps_a_valid_measure():
    import copy

    message = _gemini_turn(5)
    message_payload_chars(message)

    for clone in (copy.copy(message), copy.deepcopy(message)):
        assert message_payload_chars(clone) == _expected_weight(clone)


# ---------------------------------------------------------------------------
# _stream_text_chunked: the pacing between chunks, nothing after the last
# ---------------------------------------------------------------------------


async def test_a_replayed_answer_waits_between_chunks_only(monkeypatch):
    from app.tools import agent_loop

    events: list[str] = []

    async def _sleep(delay):
        events.append(f"sleep {delay}")

    async def _token(chunk: str) -> None:
        events.append(f"token {len(chunk)}")

    monkeypatch.setattr(agent_loop.asyncio, "sleep", _sleep)
    text = "x" * (2 * agent_loop._SIMULATED_CHUNK_SIZE + 10)

    await agent_loop._stream_text_chunked(_token, text)

    pause = f"sleep {agent_loop._SIMULATED_CHUNK_DELAY}"
    size = agent_loop._SIMULATED_CHUNK_SIZE
    assert events == [f"token {size}", pause, f"token {size}", pause, "token 10"]
