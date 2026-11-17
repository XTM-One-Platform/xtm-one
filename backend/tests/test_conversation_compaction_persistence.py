"""Persisted conversation compaction.

Before this fix the compaction output (Pinned Directives + Rolling
Summary) lived only in the in-memory history of the current turn: history
is rebuilt from raw DB messages on every turn, so

- the roll-forward path could never fire (the compaction message was
  never part of the loaded history), and
- a FULL summarization LLM call re-ran from scratch on EVERY message past
  ``SUMMARIZE_THRESHOLD``.

These tests pin the persistence contract:

1. A successful compaction persists ``{directives, summary,
   through_sequence, compacted_count}`` keyed by the DB sequence of the
   newest compacted message.
2. A rehydrated prior compaction (stored state rendered at history[0])
   rolls forward: prior directives reach the compactor prompt and the
   cumulative ``compacted_count`` grows.
3. Below the threshold no LLM call and no persistence happen.
4. Synthetic histories without DB sequences still compact but skip
   persistence (legacy behaviour).
5. ``parse_compaction_state`` round-trips and rejects malformed payloads.
"""

from __future__ import annotations

import json

import pytest

import app.services.conversation_memory as cm
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse


class _CompactorStubLLM(BaseLLMProvider):
    """Returns a fixed, valid compactor JSON payload and records prompts."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, directives: list[str] | None = None, summary: str = "- recap") -> None:
        self.directives = directives if directives is not None else ["use subregion filter"]
        self.summary = summary
        self.chat_calls: list[list[ChatMessage]] = []

    async def chat(self, messages, **kwargs):
        self.chat_calls.append(list(messages))
        return ChatResponse(
            content=json.dumps(
                {"directives": self.directives, "summary": self.summary}
            ),
            model="stub-model",
        )

    async def chat_stream(self, messages, **kwargs):  # pragma: no cover - unused
        yield ""

    async def embed(self, text, model=None):  # pragma: no cover - unused
        return [0.0]

    async def list_models(self):  # pragma: no cover - unused
        return []

    async def list_embedding_models(self):  # pragma: no cover - unused
        return []

    async def list_image_models(self):  # pragma: no cover - unused
        return []


def _make_history(count: int, *, start_seq: int = 1, with_sequence: bool = True):
    """Alternating user/assistant messages, long enough to beat the
    ``_SEMANTIC_COMPACTION_MIN_DROP_CHARS``-style minimums in the compactor."""
    out: list[ChatMessage] = []
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        msg = ChatMessage(role=role, content=f"message {i} " + "x" * 50)
        if with_sequence:
            msg.db_sequence = start_seq + i
        out.append(msg)
    return out


@pytest.fixture()
def persist_spy(monkeypatch):
    calls: list[dict] = []

    async def _spy(conversation_id, **kwargs):
        calls.append({"conversation_id": conversation_id, **kwargs})

    monkeypatch.setattr(cm, "_persist_compaction_state", _spy)
    return calls


@pytest.mark.asyncio
async def test_compaction_persists_state_with_watermark(persist_spy):
    llm = _CompactorStubLLM()
    history = _make_history(cm.SUMMARIZE_THRESHOLD + 2)  # THRESHOLD+2 msgs, seq 1..N

    result = await cm.manage_conversation_memory(
        llm, history, conversation_id="conv-1"
    )

    # Compacted: [compaction message, *last KEEP_RECENT verbatim]
    assert cm._is_compaction_message(result[0])
    assert len(result) == 1 + cm.KEEP_RECENT
    assert result[1:] == history[-cm.KEEP_RECENT :]

    assert len(persist_spy) == 1
    call = persist_spy[0]
    assert call["conversation_id"] == "conv-1"
    old_count = len(history) - cm.KEEP_RECENT
    # Watermark = sequence of the newest compacted (old) message.
    assert call["through_sequence"] == history[old_count - 1].db_sequence
    assert call["compacted_count"] == old_count
    assert "use subregion filter" in call["directives"]


@pytest.mark.asyncio
async def test_rollforward_from_stored_state(persist_spy):
    prior_state = {
        "directives": "- currency prefix is EUR",
        "summary": "- discussed dashboards",
        "through_sequence": 5,
        "compacted_count": 5,
    }
    rendered = cm.render_stored_compaction(prior_state)
    assert cm._is_compaction_message(rendered)

    llm = _CompactorStubLLM(directives=["currency prefix is EUR"])
    real = _make_history(cm.SUMMARIZE_THRESHOLD + 1, start_seq=6)  # THRESHOLD+1 msgs
    history = [rendered, *real]

    result = await cm.manage_conversation_memory(
        llm, history, conversation_id="conv-2"
    )

    assert cm._is_compaction_message(result[0])
    # Prior directives were fed to the compactor for roll-forward.
    compactor_prompt = llm.chat_calls[0][1].content
    assert "currency prefix is EUR" in compactor_prompt

    old_count = len(real) - cm.KEEP_RECENT
    call = persist_spy[0]
    # Cumulative count: prior 5 + newly compacted messages.
    assert call["compacted_count"] == 5 + old_count
    assert call["through_sequence"] == real[old_count - 1].db_sequence


@pytest.mark.asyncio
async def test_below_threshold_no_llm_and_no_persist(persist_spy):
    llm = _CompactorStubLLM()
    history = _make_history(cm.SUMMARIZE_THRESHOLD)  # exactly at threshold

    result = await cm.manage_conversation_memory(
        llm, history, conversation_id="conv-3"
    )

    assert result == history
    assert llm.chat_calls == []
    assert persist_spy == []


class _CutCompactorStubLLM(_CompactorStubLLM):
    """Every answer stops at ``max_tokens`` inside the directives."""

    async def chat(self, messages, **kwargs):
        self.chat_calls.append(list(messages))
        return ChatResponse(
            content='{"directives": ["use subregion filter", "curr',
            model="stub-model",
            stop_reason="max_tokens",
        )


@pytest.mark.asyncio
async def test_a_compaction_cut_at_max_tokens_is_never_persisted(persist_spy):
    """An answer still cut after its retry is not a compaction: the stored one
    stays in force and nothing is persisted, so the next turn compacts the
    same messages again instead of a fragment being frozen behind a new
    watermark."""
    prior_state = {
        "directives": "- currency prefix is EUR",
        "summary": "- discussed dashboards",
        "through_sequence": 5,
        "compacted_count": 5,
    }
    rendered = cm.render_stored_compaction(prior_state)
    llm = _CutCompactorStubLLM()
    real = _make_history(cm.SUMMARIZE_THRESHOLD + 1, start_seq=6)

    result = await cm.manage_conversation_memory(
        llm, [rendered, *real], conversation_id="conv-5"
    )

    assert persist_spy == []
    assert len(llm.chat_calls) == 2
    assert cm._is_compaction_message(result[0])
    directives, summary = cm._parse_compaction_message(result[0].content)
    assert directives == "- currency prefix is EUR"
    assert summary == "- discussed dashboards"
    assert result[1:] == real[-cm.KEEP_RECENT :]


# ---------------------------------------------------------------------------
# A conversation whose compaction is always cut does not pay the compactor on
# every turn: the calls are paused after repeated cuts, for a bounded time.
# ---------------------------------------------------------------------------


@pytest.fixture()
def clock(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(cm, "_monotonic", lambda: now[0])
    return now


def _history_with_prior_compaction() -> tuple[list[ChatMessage], list[ChatMessage]]:
    rendered = cm.render_stored_compaction(
        {
            "directives": "- currency prefix is EUR",
            "summary": "- discussed dashboards",
            "through_sequence": 5,
            "compacted_count": 5,
        }
    )
    real = _make_history(cm.SUMMARIZE_THRESHOLD + 1, start_seq=6)
    return [rendered, *real], real


async def _turn(llm, history, conversation_id: str | None = "conv-cut"):
    return await cm.manage_conversation_memory(
        llm, history, conversation_id=conversation_id
    )


@pytest.mark.asyncio
async def test_a_single_cut_is_retried_on_the_next_turn(persist_spy, clock):
    """A long answer is often a one-off: the first cut pauses nothing."""
    llm = _CutCompactorStubLLM()
    history, _real = _history_with_prior_compaction()

    await _turn(llm, history)
    assert cm.should_compact(history, conversation_id="conv-cut")
    await _turn(llm, history)

    assert len(llm.chat_calls) == 4


@pytest.mark.asyncio
async def test_repeated_cuts_pause_the_compactor_with_a_doubling_backoff(
    persist_spy, clock
):
    llm = _CutCompactorStubLLM()
    history, real = _history_with_prior_compaction()

    await _turn(llm, history)
    await _turn(llm, history)
    assert len(llm.chat_calls) == 4

    # Paused: no compactor call and no "compacting" status, the previous
    # compaction still in force in front of the recent window.
    assert not cm.should_compact(history, conversation_id="conv-cut")
    assert cm.should_compact(history)
    result = await _turn(llm, history)
    assert len(llm.chat_calls) == 4
    directives, summary = cm._parse_compaction_message(result[0].content)
    assert (directives, summary) == ("- currency prefix is EUR", "- discussed dashboards")
    assert result[1:] == real[-cm.KEEP_RECENT :]

    # The pause ends and the compactor runs again; cut once more, the next
    # pause is twice as long.
    clock[0] += cm._CUT_BACKOFF_BASE_SECS
    await _turn(llm, history)
    assert len(llm.chat_calls) == 6
    clock[0] += cm._CUT_BACKOFF_BASE_SECS
    await _turn(llm, history)
    assert len(llm.chat_calls) == 6
    clock[0] += cm._CUT_BACKOFF_BASE_SECS
    await _turn(llm, history)
    assert len(llm.chat_calls) == 8
    assert persist_spy == []


def test_the_pause_is_capped(clock):
    pauses = [cm._record_compaction_cut("conv-cap") for _ in range(40)]

    assert pauses[:4] == [
        0,
        cm._CUT_BACKOFF_BASE_SECS,
        2 * cm._CUT_BACKOFF_BASE_SECS,
        4 * cm._CUT_BACKOFF_BASE_SECS,
    ]
    assert max(pauses) == pauses[-1] == cm._CUT_BACKOFF_MAX_SECS


def test_the_pause_table_is_bounded(clock, monkeypatch):
    monkeypatch.setattr(cm, "_CUT_BACKOFF_MAX_CONVERSATIONS", 3)

    for index in range(5):
        cm._record_compaction_cut(f"conv-{index}")

    assert list(cm._compaction_cuts) == ["conv-2", "conv-3", "conv-4"]


@pytest.mark.asyncio
async def test_a_successful_compaction_resets_the_cut_count(persist_spy, clock):
    cut_llm = _CutCompactorStubLLM()
    history, _real = _history_with_prior_compaction()

    await _turn(cut_llm, history)
    await _turn(_CompactorStubLLM(), history)
    await _turn(cut_llm, history)

    # One cut since the success: the next turn still runs the compactor.
    assert cm.should_compact(history, conversation_id="conv-cut")
    assert len(cut_llm.chat_calls) == 4
    assert len(persist_spy) == 1


@pytest.mark.asyncio
async def test_a_history_without_a_conversation_is_never_paused(persist_spy, clock):
    llm = _CutCompactorStubLLM()
    history, _real = _history_with_prior_compaction()

    for _ in range(3):
        await _turn(llm, history, conversation_id=None)

    assert len(llm.chat_calls) == 6


@pytest.mark.asyncio
async def test_synthetic_history_compacts_without_persisting(persist_spy):
    llm = _CompactorStubLLM()
    history = _make_history(cm.SUMMARIZE_THRESHOLD + 2, with_sequence=False)

    result = await cm.manage_conversation_memory(
        llm, history, conversation_id="conv-4"
    )

    assert cm._is_compaction_message(result[0])
    assert persist_spy == []


def test_parse_compaction_state_roundtrip_and_validation():
    payload = json.dumps(
        {
            "directives": "- a",
            "summary": "- b",
            "through_sequence": 7,
            "compacted_count": 12,
        }
    )
    state = cm.parse_compaction_state(payload)
    assert state == {
        "directives": "- a",
        "summary": "- b",
        "through_sequence": 7,
        "compacted_count": 12,
    }
    # Rendered message round-trips through the compactor's own parser.
    rendered = cm.render_stored_compaction(state)
    directives, summary = cm._parse_compaction_message(rendered.content)
    assert directives == "- a"
    assert summary == "- b"
    match = cm._COMPACTED_COUNT_RE.match(rendered.content)
    assert match and int(match.group(1)) == 12

    assert cm.parse_compaction_state(None) is None
    assert cm.parse_compaction_state("") is None
    assert cm.parse_compaction_state("not json") is None
    # Missing watermark -> unusable.
    assert cm.parse_compaction_state(json.dumps({"directives": "- a"})) is None


class _Row:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> str | None:
        return self.value


class _ConversationRowSession:
    """The conversations row a persist reads under its lock, then writes."""

    def __init__(self, stored: str | None) -> None:
        self.stored = stored
        self.locked_reads = 0
        self.written: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement):
        if getattr(statement, "is_select", False):
            assert statement._for_update_arg is not None, "the read must lock the row"
            self.locked_reads += 1
            return _Row(self.stored)
        self.written.append(statement.compile().params["compaction_state_json"])
        return None

    async def commit(self):
        self.stored = self.written[-1]


def _stored_state(through_sequence: int) -> str:
    return json.dumps(
        {"directives": "- kept", "summary": "- kept", "through_sequence": through_sequence, "compacted_count": 20}
    )


@pytest.mark.parametrize(
    ("stored", "through_sequence", "written"),
    [
        (None, 40, True),
        ("not json", 40, True),
        (_stored_state(30), 40, True),
        (_stored_state(40), 40, False),
        (_stored_state(55), 40, False),
    ],
    ids=["none-stored", "malformed", "older", "same", "newer"],
)
async def test_the_persisted_state_only_moves_forward(monkeypatch, stored, through_sequence, written):
    """A compaction precomputed after a turn runs outside the turn
    lock: a turn on another process may persist a newer compaction before it
    finishes. Written over, the newer summary and watermark would be lost -
    the stored state is replaced only by one that covers more."""
    session = _ConversationRowSession(stored)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    await cm._persist_compaction_state(
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        directives="- new",
        summary="- new",
        through_sequence=through_sequence,
        compacted_count=25,
    )

    assert session.locked_reads == 1
    if written:
        assert [json.loads(value)["through_sequence"] for value in session.written] == [through_sequence]
    else:
        assert session.written == []
        assert session.stored == stored


@pytest.mark.parametrize(
    ("stored", "cleared"),
    [(_stored_state(40), True), ("not json", True), (None, True), (_stored_state(55), False)],
    ids=["still-the-stale-one", "malformed", "already-cleared", "newer-persisted-since"],
)
async def test_a_stale_state_is_cleared_only_while_it_is_the_one_that_was_read(monkeypatch, stored, cleared):
    """The history loader clears a state whose watermark is past the last
    message (messages were purged). A compaction persisted between that read
    and the clear - by a precompute or a turn on another process - is newer
    and must survive: the clear takes the persist's row lock and compares."""
    session = _ConversationRowSession(stored)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    await cm.clear_compaction_state("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", observed_through_sequence=40)

    assert session.locked_reads == 1
    assert session.written == ([None] if cleared else [])


async def test_a_clear_without_an_observed_watermark_stays_unconditional(monkeypatch):
    session = _ConversationRowSession(_stored_state(55))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)

    await cm.clear_compaction_state("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    assert session.locked_reads == 0
    assert session.written == [None]
