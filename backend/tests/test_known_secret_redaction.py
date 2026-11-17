"""The secret variable values a run or a turn resolved, kept out of what it stores, traces and logs.

``core/redaction.py`` owns the rule: a per-run / per-turn registry opened by
``known_secrets_scope``, filled by ``load_variables``, applied by the engine's
write hooks, the trace sanitizers, the log filter and the user surfaces, and
undone by ``restore_known_secrets`` where the model's context is rebuilt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import pytest
from sqlalchemy import JSON, Column, Integer, String, Text, create_engine, insert, select, text, update
from sqlalchemy.orm import DeclarativeBase, Session

from app.core.database import json_serializer
from app.core.redaction import (
    MIN_KNOWN_SECRET_LENGTH,
    SECRET_DISPLAY_TEXT,
    SECRET_MARKER_RE,
    KnownSecretStream,
    bind_known_secrets,
    current_known_secrets,
    known_secrets_scope,
    known_secrets_scoped,
    redact_known_secrets,
    redact_known_secrets_for_display,
    redact_log_event,
    redact_secrets,
    remember_secret_value,
    restore_known_secrets,
)

TOKEN_ID = "11111111-1111-4111-8111-111111111111"
TOKEN = "ghp_SuperSecretValue42"
PASSWORD_ID = "22222222-2222-4222-8222-222222222222"
PASSWORD = 'pa"ss\\word\nwith é'


def _markers(text: str) -> list[str]:
    return [match.group(1) for match in SECRET_MARKER_RE.finditer(text)]


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_a_known_value_is_stored_as_its_marker_and_restored_in_the_same_run():
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        stored = redact_known_secrets(f"Use {TOKEN} for the call")
        assert TOKEN not in stored
        assert _markers(stored) == [TOKEN_ID]
        assert restore_known_secrets(stored) == f"Use {TOKEN} for the call"


def test_outside_a_scope_nothing_is_redacted_or_restored():
    assert current_known_secrets() is None
    assert redact_known_secrets(f"key {TOKEN}") == f"key {TOKEN}"
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        stored = redact_known_secrets(TOKEN)
    assert restore_known_secrets(stored) == stored


def test_a_short_value_is_never_redacted():
    short = "x" * (MIN_KNOWN_SECRET_LENGTH - 1)
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, short)
        remember_secret_value(PASSWORD_ID, "")
        assert redact_known_secrets(f"a {short} b") == f"a {short} b"
        assert not current_known_secrets()


def test_overlapping_values_are_replaced_whole_longest_first():
    inner_id = "33333333-3333-4333-8333-333333333333"
    with known_secrets_scope("run-1"):
        remember_secret_value(inner_id, "SuperSecret")  # a part of TOKEN
        remember_secret_value(TOKEN_ID, TOKEN)
        stored = redact_known_secrets(f"{TOKEN} and SuperSecret")
        assert _markers(stored) == [TOKEN_ID, inner_id]
        assert restore_known_secrets(stored) == f"{TOKEN} and SuperSecret"


def test_a_value_escaped_inside_a_json_string_is_redacted_too():
    with known_secrets_scope("run-1"):
        remember_secret_value(PASSWORD_ID, PASSWORD)
        for document in (json.dumps({"password": PASSWORD}), json.dumps({"password": PASSWORD}, ensure_ascii=False)):
            stored = redact_known_secrets(document)
            assert "pa\\\"ss" not in stored and "word\\nwith" not in stored
            assert _markers(stored) == [PASSWORD_ID]
            # The marker needs no escaping: the document stays valid JSON.
            assert json.loads(stored)["password"].startswith("[[SECRET_VARIABLE:")


def test_a_value_percent_encoded_in_a_url_is_redacted_too():
    """An HTTP error echoes the request URL, its query encoded: a value holding + / = (like an AWS secret) included."""
    from urllib.parse import quote, quote_plus

    slashed = "pass/word+with=signs"
    with known_secrets_scope("run-1"):
        remember_secret_value(PASSWORD_ID, slashed)
        for encoded in (quote(slashed, safe=""), quote_plus(slashed)):
            stored = redact_known_secrets(f"401 for https://api.example.com/v1?key={encoded}&page=1")
            assert encoded not in stored
            assert _markers(stored) == [PASSWORD_ID]
            assert stored.endswith("&page=1")


def test_a_second_pass_keeps_markers_even_when_a_value_looks_like_part_of_one():
    hex_id = "44444444-4444-4444-8444-444444444444"
    with known_secrets_scope("run-1") as scope:
        remember_secret_value(TOKEN_ID, TOKEN)
        stored = redact_known_secrets(f"x {TOKEN} y")
        # A value that happens to be a piece of the marker the first pass wrote.
        tag = SECRET_MARKER_RE.search(stored).group(2)
        remember_secret_value(hex_id, tag[:MIN_KNOWN_SECRET_LENGTH])
        assert redact_known_secrets(stored) == stored
        assert scope.restore_text(stored) == f"x {TOKEN} y"


def test_json_shaped_data_is_walked_and_untouched_containers_are_kept():
    clean = {"a": ["b", 1, None], "c": {"d": "e"}}
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        assert redact_known_secrets(clean) is clean
        stored = redact_known_secrets({"tool_calls": [{"arguments": {"token": TOKEN}}], "n": 3})
        assert stored["n"] == 3
        assert _markers(stored["tool_calls"][0]["arguments"]["token"]) == [TOKEN_ID]
        assert restore_known_secrets(stored)["tool_calls"][0]["arguments"]["token"] == TOKEN


def test_a_marker_is_restored_only_in_the_run_that_wrote_it():
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        stored = redact_known_secrets(TOKEN)
    with known_secrets_scope("run-2"):
        remember_secret_value(TOKEN_ID, TOKEN)
        # Copied into another run (a handover payload, a pasted transcript): not restored.
        assert restore_known_secrets(stored) == stored
    with known_secrets_scope("run-1"):
        # The same run, its variable not resolved this time: not restored either.
        assert restore_known_secrets(stored) == stored
        remember_secret_value(TOKEN_ID, TOKEN)
        assert restore_known_secrets(stored) == TOKEN


def test_a_forged_marker_is_restored_by_nothing():
    forged = f"[[SECRET_VARIABLE:{TOKEN_ID}:{'0' * 16}]]"
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        # A webhook payload, a channel message or a fetched page naming the variable.
        assert restore_known_secrets(f"payload {forged}") == f"payload {forged}"
        assert redact_known_secrets(forged) == forged


def test_a_scope_bound_once_its_conversation_exists():
    with known_secrets_scope() as scope:
        bind_known_secrets("conversation-9")
        bind_known_secrets("another")  # the first binding stays
        remember_secret_value(TOKEN_ID, TOKEN)
        stored = redact_known_secrets(TOKEN)
        assert scope.binding == "conversation-9"
    with known_secrets_scope("conversation-9"):
        remember_secret_value(TOKEN_ID, TOKEN)
        assert restore_known_secrets(stored) == TOKEN


def test_a_nested_scope_still_redacts_the_outer_values_and_restores_only_its_own():
    with known_secrets_scope("conversation-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        outer = redact_known_secrets(TOKEN)
        with known_secrets_scope("run-7"):
            assert TOKEN not in redact_known_secrets(f"handover {TOKEN}")
            assert restore_known_secrets(outer) == outer
            # A consulted agent's own secret: its answer is stored by the outer turn.
            remember_secret_value(PASSWORD_ID, "consulted-agent-secret")
        assert restore_known_secrets(outer) == TOKEN
        assert "consulted-agent-secret" not in redact_known_secrets("answer: consulted-agent-secret")


async def test_tasks_spawned_in_the_scope_share_it():
    seen: list[str] = []
    started = asyncio.Event()
    remembered = asyncio.Event()

    async def _spawned():
        started.set()
        await remembered.wait()
        seen.append(redact_known_secrets(TOKEN))

    with known_secrets_scope("run-1"):
        task = asyncio.create_task(_spawned())
        await started.wait()
        # Remembered after the task was spawned (load_variables runs later in the run).
        remember_secret_value(TOKEN_ID, TOKEN)
        remembered.set()
        await task
    assert _markers(seen[0]) == [TOKEN_ID]


async def test_a_value_remembered_with_no_scope_open_is_still_redacted_in_that_task():
    async def _run():
        remember_secret_value(TOKEN_ID, TOKEN)
        return redact_known_secrets(TOKEN)

    # Its own task: the scope it opened does not outlive it.
    assert _markers(await asyncio.create_task(_run())) == [TOKEN_ID]
    assert current_known_secrets() is None


async def test_an_entry_point_runs_in_a_scope_bound_to_its_id():
    seen: dict = {}

    @known_secrets_scoped("run_id")
    async def _execute(run_id: str, *, other: int = 0):
        remember_secret_value(TOKEN_ID, TOKEN)
        seen["binding"] = current_known_secrets().binding
        return redact_known_secrets(TOKEN)

    stored = await _execute("run-42", other=1)
    assert seen["binding"] == "run-42" and current_known_secrets() is None
    with known_secrets_scope("run-42"):
        remember_secret_value(TOKEN_ID, TOKEN)
        assert restore_known_secrets(stored) == TOKEN


async def test_a_streaming_entry_point_keeps_its_scope_across_steps():
    @known_secrets_scoped("conversation_id")
    async def _turn(conversation_id: str):
        remember_secret_value(TOKEN_ID, TOKEN)
        yield redact_known_secrets(TOKEN)
        # The consumer ran between the two steps; the scope is back for this one.
        yield current_known_secrets().binding

    items = [item async for item in _turn("conversation-3")]
    assert _markers(items[0]) == [TOKEN_ID] and items[1] == "conversation-3"
    assert current_known_secrets() is None


def test_the_shape_heuristics_run_after_the_exact_values():
    with known_secrets_scope("run-1"):
        remember_secret_value(PASSWORD_ID, "correct horse battery")
        assert _markers(redact_secrets("the password is correct horse battery")) == [PASSWORD_ID]


# ---------------------------------------------------------------------------
# User surfaces, logs
# ---------------------------------------------------------------------------


def test_a_surface_that_cannot_restore_gets_the_display_text():
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        assert redact_known_secrets_for_display(f"key {TOKEN}") == f"key {SECRET_DISPLAY_TEXT}"
        marker = redact_known_secrets(TOKEN)
        # A stored marker shown on such a surface is not handed out either.
        assert redact_known_secrets_for_display(f"was {marker}") == f"was {SECRET_DISPLAY_TEXT}"


@pytest.mark.parametrize("display", [False, True])
def test_a_streamed_value_is_never_shown_even_in_part(display):
    pieces = ["Your key is gh", "p_Super", "Secret", "Value4", "2, keep it.", " Bye"]
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        stream = KnownSecretStream(display=display)
        shown = [stream.feed(piece) for piece in pieces]
        shown.append(stream.flush())
    whole = "".join(shown)
    # No emitted piece holds any part of the value (a marker is hex: no "g", no "S").
    for emitted in shown:
        assert "gh" not in emitted and "Super" not in emitted and "Value" not in emitted
    if display:
        assert whole == f"Your key is {SECRET_DISPLAY_TEXT}, keep it. Bye"
    else:
        assert _markers(whole) == [TOKEN_ID] and whole.endswith(", keep it. Bye")
    # Text that cannot start a value is shown at once.
    assert shown[0] == "Your key is "


@pytest.mark.parametrize("display", [False, True])
def test_a_value_that_starts_a_longer_one_is_held_until_the_next_chunk(display):
    """With ``abcdefgh`` and ``abcdefghMORESECRET`` known, a chunk boundary after the first never shows the rest."""
    long_id = "44444444-4444-4444-8444-444444444444"
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, "abcdefgh")
        remember_secret_value(long_id, "abcdefghMORESECRET")
        stream = KnownSecretStream(display=display)
        shown = [stream.feed("key abcdefgh"), stream.feed("MORESECRET done"), stream.flush()]
        # The short value alone, once the next chunk shows it ends there.
        alone = KnownSecretStream(display=display)
        alone_shown = [alone.feed("key abcdefgh"), alone.feed(" done"), alone.flush()]
    whole = "".join(shown)
    assert "MORESECRET" not in whole and "abcdefgh" not in whole
    assert shown[0] == "key "
    assert whole == (f"key {SECRET_DISPLAY_TEXT} done" if display else whole) and (display or _markers(whole) == [long_id])
    assert "abcdefgh" not in "".join(alone_shown)
    assert display or _markers("".join(alone_shown)) == [TOKEN_ID]


async def test_a_secret_equal_to_a_protocol_word_never_rewrites_a_frame_or_an_event():
    """A secret variable worth ``cancelled`` or ``thinking_text`` leaves the frame's type and the event's status alone."""
    from app.core.redaction import redact_frame, redacted_stream_callbacks
    from app.services.chat_turn_stream import is_terminal_frame

    events: list[dict] = []

    async def _event(event):
        events.append(event)

    with known_secrets_scope("conversation-1"):
        remember_secret_value(TOKEN_ID, "cancelled")
        remember_secret_value(PASSWORD_ID, "thinking_text")
        frame = redact_frame({"type": "cancelled", "message_id": "m1", "content": "status: cancelled"}, display=True)
        _, on_event, release = redacted_stream_callbacks(None, _event)
        await on_event({"status": "thinking_text", "content": "why"})
        await on_event({"status": "tool_done", "tool": "http", "result": "cancelled"})
        await release()

    assert frame["type"] == "cancelled" and is_terminal_frame(frame)
    assert frame["content"] == f"status: {SECRET_DISPLAY_TEXT}"
    assert [event["status"] for event in events] == ["thinking_text", "tool_done"]
    assert _markers(events[1]["result"]) == [TOKEN_ID]


async def test_split_reasoning_and_answer_never_show_the_rest_of_a_longer_value():
    from app.core.redaction import redacted_stream_callbacks

    wire: list[str] = []

    async def _token(chunk):
        wire.append(chunk)

    async def _event(event):
        wire.append(event.get("content") or "")

    with known_secrets_scope("conversation-1"):
        remember_secret_value(TOKEN_ID, "abcdefgh")
        remember_secret_value("44444444-4444-4444-8444-444444444444", "abcdefghMORESECRET")
        on_token, on_event, release = redacted_stream_callbacks(_token, _event)
        await on_event({"status": "thinking_text", "content": "use abcdefgh"})
        await on_event({"status": "thinking_text", "content": "MORESECRET now"})
        await on_token("answer abcdefgh")
        await on_token("MORESECRET end")
        await release()
    assert "MORESECRET" not in "".join(wire)


async def test_the_agent_loop_streams_and_emits_redacted_and_releases_at_the_end():
    from app.tools.agent_loop import _known_secrets_streamed

    wire: list[tuple[str, object]] = []

    async def _token(chunk):
        wire.append(("token", chunk))

    async def _event(event):
        wire.append(("event", event))

    @_known_secrets_streamed
    async def _loop(llm, *, on_token=None, on_event=None):
        await on_token("Key: ghp_Super")
        await on_token("SecretValue42 ok, then ghp_")
        await on_event({"status": "tool_start", "arguments": {"token": TOKEN}})
        await on_token("tail ghp_Su")
        return "done"

    with known_secrets_scope("conversation-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        assert await _loop(None, on_token=_token, on_event=_event) == "done"
    tokens = "".join(str(item) for kind, item in wire if kind == "token")
    assert TOKEN not in tokens and "Super" not in tokens.replace("[[SECRET_VARIABLE:", "")
    assert _markers(tokens) == [TOKEN_ID]
    # What was held back before the event went out first, in order; the rest at the end.
    kinds = [kind for kind, _ in wire]
    assert kinds.index("event") > 1 and wire[-1] == ("token", "ghp_Su")
    event = next(item for kind, item in wire if kind == "event")
    assert _markers(event["arguments"]["token"]) == [TOKEN_ID]


async def test_split_reasoning_never_shows_a_value_in_part():
    from app.core.redaction import redacted_stream_callbacks

    wire: list[tuple[str, object]] = []

    async def _token(chunk):
        wire.append(("token", chunk))

    async def _event(event):
        wire.append(("event", dict(event)))

    with known_secrets_scope("conversation-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        on_token, on_event, release = redacted_stream_callbacks(_token, _event)
        await on_event({"status": "thinking_text", "content": "\n\nI will use ghp_Super"})
        await on_event({"status": "thinking_text", "content": "SecretValue42 now, then ghp_Su"})
        # The answer starts: what reasoning still held is released first.
        await on_token("Done.")
        await on_event({"status": "thinking_text", "content": "more ghp_"})
        await on_event({"status": "tool_start", "tool": "http_request"})
        await release()

    reasoning = "".join(item["content"] for kind, item in wire if kind == "event" and item.get("status") == "thinking_text")
    for kind, item in wire:
        if kind == "event" and item.get("status") == "thinking_text":
            assert "Super" not in item["content"] and "Value42" not in item["content"]
    assert TOKEN not in reasoning and _markers(reasoning) == [TOKEN_ID]
    assert reasoning.endswith(" now, then ghp_Sumore ghp_")
    order = [(kind, item if kind == "token" else item.get("status")) for kind, item in wire]
    assert order.index(("token", "Done.")) > order.index(("event", "thinking_text"))
    assert order[-1] == ("event", "tool_start")


async def test_a_retracted_answer_drops_what_was_held():
    from app.core.redaction import redacted_stream_callbacks

    sent: list[str] = []

    async def _token(chunk):
        sent.append(chunk)

    async def _event(_event):
        return None

    with known_secrets_scope("conversation-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        on_token, on_event, release = redacted_stream_callbacks(_token, _event)
        await on_token("partial ghp_Sup")
        await on_event({"status": "stream_retract"})
        await release()
    assert sent == ["partial "]


def test_outside_a_scope_the_callbacks_are_left_as_given():
    from app.core.redaction import redacted_stream_callbacks

    async def _token(_chunk):
        return None

    assert redacted_stream_callbacks(_token, None) == (_token, None, None)


def test_a_reloaded_history_gets_the_values_back_without_touching_the_stored_rows():
    from app.llm.base import ChatMessage
    from app.services.variable_resolver import restore_secrets_in_history
    from app.tools.base import ToolCall

    with known_secrets_scope("conversation-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        marker = redact_known_secrets(TOKEN)
        untouched = ChatMessage(role="user", content="hello")
        stored = ChatMessage(
            role="assistant",
            content=f"using {marker}",
            thinking=f"the token is {marker}",
            tool_calls=[ToolCall(id="c1", name="http_request", arguments={"auth": f"Bearer {marker}"})],
        )
        history = restore_secrets_in_history([untouched, stored])
    assert history[0] is untouched
    assert history[1].content == f"using {TOKEN}" and history[1].thinking == f"the token is {TOKEN}"
    assert history[1].tool_calls[0].arguments == {"auth": f"Bearer {TOKEN}"}
    assert stored.content == f"using {marker}"


async def test_a_channel_reply_leaves_with_the_display_text():
    from app.channels.base import OutboundMessage
    from app.channels.router import _reply_redacted_for_display

    @_reply_redacted_for_display
    async def _turn():
        return OutboundMessage(text=f"Here: {TOKEN}", metadata={"blocks": [{"text": TOKEN}]})

    with known_secrets_scope("conversation-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        outbound = await _turn()
    assert outbound.text == f"Here: {SECRET_DISPLAY_TEXT}"
    assert outbound.metadata == {"blocks": [{"text": SECRET_DISPLAY_TEXT}]}


def test_a_turn_frame_fans_out_with_the_display_text():
    from app.services.chat_turn_stream import TurnPublisher

    publisher = TurnPublisher("conversation-1", "token")
    with known_secrets_scope("conversation-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        publisher.offer({"type": "done", "content": f"Answer {TOKEN}"})
    assert publisher._pending[-1]["content"] == f"Answer {SECRET_DISPLAY_TEXT}"


async def test_load_variables_remembers_the_secret_values_it_resolves():
    from types import SimpleNamespace

    from app.core.security import encrypt_value
    from app.services.variable_resolver import load_variables

    secret = SimpleNamespace(
        id=uuid.UUID(TOKEN_ID), name="token", is_secret=True, value_enc=encrypt_value(TOKEN), value=None
    )
    secret.__tablename__ = "variables"
    plain = SimpleNamespace(id=uuid.uuid4(), name="region", is_secret=False, value_enc=None, value="eu-west-1-plain")

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [secret, plain]))

    with known_secrets_scope("run-1"):
        resolved = await load_variables(_Db(), None)
        assert resolved[TOKEN_ID] == TOKEN
        assert _markers(redact_known_secrets(f"{TOKEN} {plain.value}")) == [TOKEN_ID]
        assert plain.value in redact_known_secrets(plain.value)


def test_the_frontend_shows_markers_with_the_backends_pattern_and_text():
    import re
    from pathlib import Path

    mirror = (Path(__file__).resolve().parents[2] / "frontend/src/pages/chat/secretMarkers.ts").read_text(encoding="utf-8")
    frontend_pattern = re.search(r"SECRET_MARKER_RE =\s*/(.+?)/g;", mirror, re.S).group(1)
    # The same marker, without the backend's capture groups.
    assert frontend_pattern == SECRET_MARKER_RE.pattern.replace("(", "").replace(")", "")
    assert f'SECRET_DISPLAY_TEXT = "{SECRET_DISPLAY_TEXT}"' in mirror


def test_a_stream_ending_on_a_prefix_releases_it():
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        stream = KnownSecretStream()
        assert stream.feed("tail ghp_Su") == "tail "
        assert stream.flush() == "ghp_Su"


@pytest.mark.parametrize("log_format", ["json", "console"])
def test_a_log_line_carries_markers_message_extra_and_traceback(log_format, capsys):
    from app.core.logging import setup_logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    setup_logging(log_format=log_format)
    try:
        logger = logging.getLogger("tests.known_secrets")
        with known_secrets_scope("run-1"):
            remember_secret_value(TOKEN_ID, TOKEN)
            logger.warning("calling with %s", TOKEN, extra={"arguments": {"token": TOKEN}})
            try:
                raise RuntimeError(f"401 for {TOKEN}")
            except RuntimeError:
                logger.exception("failed")
        inside = capsys.readouterr().out
        logger.warning("outside %s", TOKEN)
        outside = capsys.readouterr().out
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    assert TOKEN not in inside
    assert "[[SECRET_VARIABLE:" in inside and "RuntimeError" in inside
    assert TOKEN in outside


def test_the_log_processor_walks_the_event():
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        event = redact_log_event(None, "info", {"event": f"x {TOKEN}", "exception": f"Traceback ... {TOKEN}", "n": 1})
    assert TOKEN not in json.dumps(event) and event["n"] == 1


# ---------------------------------------------------------------------------
# The engine's write hooks
# ---------------------------------------------------------------------------


class _Base(DeclarativeBase):
    pass


class _Row(_Base):
    __tablename__ = "known_secret_rows"
    id = Column(Integer, primary_key=True)
    name = Column(String(64))
    body = Column(Text)
    document = Column(JSON)


@pytest.fixture
def sqlite_engine():
    engine = create_engine("sqlite://", json_serializer=json_serializer)
    _Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_every_write_in_the_scope_stores_markers(sqlite_engine):
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        with Session(sqlite_engine) as session:
            row = _Row(id=1, name="n", body=f"text {TOKEN}", document={"messages": [{"content": TOKEN}]})
            session.add(row)
            session.commit()
            # The flushed object says what the database holds.
            assert TOKEN not in row.body
            row.name = "renamed"
            session.commit()
        with sqlite_engine.begin() as connection:
            # Core writes too, in the content columns they set.
            connection.execute(insert(_Row).values(id=2, name="core", body=TOKEN, document=[TOKEN]))
            connection.execute(update(_Row).where(_Row.id == 2).values(body=f"updated {TOKEN}"))
    with sqlite_engine.connect() as connection:
        raw = connection.execute(text("SELECT name, body, document FROM known_secret_rows ORDER BY id")).all()
    assert all(TOKEN not in str(value) for row in raw for value in row)
    assert _markers(raw[0][1]) == [TOKEN_ID] and _markers(raw[0][2]) == [TOKEN_ID]
    assert raw[0][0] == "renamed"
    assert _markers(raw[1][1]) == [TOKEN_ID] and _markers(raw[1][2]) == [TOKEN_ID]


def test_a_batched_insert_stores_markers_in_every_row(sqlite_engine):
    """``insert(...).returning(...)`` of several rows is one statement, its parameters one flat sequence."""
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        with sqlite_engine.begin() as connection:
            connection.execute(
                insert(_Row).returning(_Row.id),
                [{"id": n, "name": TOKEN, "body": f"row {n} {TOKEN}"} for n in (10, 11, 12)],
            )
    with sqlite_engine.connect() as connection:
        raw = connection.execute(text("SELECT name, body FROM known_secret_rows WHERE id >= 10 ORDER BY id")).all()
    assert len(raw) == 3
    assert all(TOKEN not in body and _markers(body) == [TOKEN_ID] for _name, body in raw)
    # A bounded name column is structure, never rewritten.
    assert {name for name, _body in raw} == {TOKEN}


def test_a_status_a_key_and_a_predicate_are_never_rewritten(sqlite_engine):
    """A secret that equals a structural value ("completed") is redacted from content columns only."""
    status_id = "55555555-5555-4555-8555-555555555555"
    with known_secrets_scope("run-1"):
        remember_secret_value(status_id, "completed")
        with Session(sqlite_engine) as session:
            session.add(_Row(id=1, name="completed", body="the run completed", document={"status": "completed"}))
            session.commit()
            row = session.get(_Row, 1)
            row.name = "completed"
            row.body = "completed again"
            session.commit()
        with sqlite_engine.begin() as connection:
            # Core: the WHERE value is the status, the SET values are a status and content.
            result = connection.execute(
                update(_Row).where(_Row.name == "completed").values(name="completed", body="done: completed")
            )
            assert result.rowcount == 1
    with sqlite_engine.connect() as connection:
        name, body, document = connection.execute(text("SELECT name, body, document FROM known_secret_rows")).one()
    assert name == "completed"
    assert _markers(body) == [status_id] and "completed" not in body
    assert _markers(document) == [status_id]


def test_a_lookup_by_a_value_is_not_rewritten_and_writes_outside_a_scope_are_untouched(sqlite_engine):
    with sqlite_engine.begin() as connection:
        connection.execute(insert(_Row).values(id=1, name=TOKEN, body=TOKEN))
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        with Session(sqlite_engine) as session:
            found = session.execute(select(_Row.id).where(_Row.name == TOKEN)).scalar_one_or_none()
    assert found == 1


def test_a_loaded_row_keeps_what_this_flush_does_not_write(sqlite_engine):
    with sqlite_engine.begin() as connection:
        connection.execute(insert(_Row).values(id=1, name="old", body=f"persona typed {TOKEN} literally"))
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        with Session(sqlite_engine) as session:
            row = session.get(_Row, 1)
            row.name = "new"
            session.commit()
    with sqlite_engine.connect() as connection:
        assert connection.execute(text("SELECT body FROM known_secret_rows")).scalar_one() == (
            f"persona typed {TOKEN} literally"
        )


def test_the_trace_sanitizers_redact_in_the_capturing_task():
    from app.services.langfuse_tracing import _sanitize_for_langfuse, _sanitize_for_storage

    payload = {"messages": [{"role": "system", "content": f"key {TOKEN}"}], "id": str(uuid.uuid4())}
    with known_secrets_scope("run-1"):
        remember_secret_value(TOKEN_ID, TOKEN)
        for sanitized in (_sanitize_for_storage(payload), _sanitize_for_langfuse(payload)):
            assert TOKEN not in json.dumps(sanitized)
            assert _markers(sanitized["messages"][0]["content"]) == [TOKEN_ID]
