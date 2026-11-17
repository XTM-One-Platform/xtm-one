"""The agent a restored chat conversation belongs to.

Without it the ``@filigran/chatbot`` panel had nothing to label history with
and fell back to whichever agent happened to be selected: reopening a CTEM
Assistant thread while another agent was picked relabelled the whole
conversation — even though the stored agent went on answering it (see the
``conv.agent_id`` branch in ``send_platform_chat_message``). The label lied
about who replied, which is the kind of thing that costs an hour of debugging.

These lock the wire contract the panel depends on. What the restore's SQL
really selects, and in which order, is pinned against PostgreSQL in
``test_platform_chat_restore_postgres.py``: a session double returns whatever
rows it is handed.
"""

import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.api.v1.platform import (
    PlatformChatSessionResponse,
    attribute_message_agents,
    recorded_message_agents,
)


def _row(role: str, content: str, metadata_json: str | None = None) -> SimpleNamespace:
    """A stored message as the restore reads it."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        content=content,
        metadata_json=metadata_json,
        created_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
    )


def _sql(statement) -> str:
    """The statement as PostgreSQL receives it, values inlined, one line."""
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return " ".join(str(compiled).split())


def _sql_limit(statement) -> int | None:
    match = re.search(r"LIMIT (\d+)$", _sql(statement))
    return int(match.group(1)) if match else None


def _user(text: str = "hi") -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str = "ok", chain: list | None = None) -> dict:
    entry: dict = {"role": "assistant", "content": text}
    if chain is not None:
        entry["transfer_chain"] = chain
    return entry


class TestPerMessageAttribution:
    """Reconstructed from data already persisted — nothing new is written.

    A transfer rewrites ``conv.agent_id`` in place rather than starting a new
    conversation, so a thread can hold answers from several agents.
    """

    def test_untransferred_thread_is_all_the_starting_agent(self):
        messages = [_user(), _assistant(), _user(), _assistant()]
        attribute_message_agents(messages, "Alpha")
        assert [m.get("agent_name") for m in messages] == [None, "Alpha", None, "Alpha"]

    def test_handover_names_the_turn_it_happened_on(self):
        """The transfer happens *during* a turn, so that turn is already Beta's.

        Attributing it to Alpha — the agent that merely started the turn —
        would credit the answer to someone who did not write it.
        """
        messages = [
            _user(),
            _assistant("by alpha"),
            _user(),
            _assistant("by beta", chain=[{"agent_id": "b", "agent_name": "Beta"}]),
        ]
        attribute_message_agents(messages, "Alpha")
        assert [m.get("agent_name") for m in messages] == [None, "Alpha", None, "Beta"]

    def test_the_handover_sticks_for_later_turns(self):
        messages = [
            _assistant("by alpha"),
            _assistant("by beta", chain=[{"agent_name": "Beta"}]),
            _assistant("still beta"),
        ]
        attribute_message_agents(messages, "Alpha")
        assert [m.get("agent_name") for m in messages] == ["Alpha", "Beta", "Beta"]

    def test_last_link_of_a_multi_hop_chain_wins(self):
        messages = [_assistant(chain=[{"agent_name": "Beta"}, {"agent_name": "Gamma"}])]
        attribute_message_agents(messages, "Alpha")
        assert messages[0]["agent_name"] == "Gamma"

    def test_user_messages_are_never_stamped(self):
        """An agent's name on the user's own words would be a plain lie."""
        messages = [_user(), _assistant()]
        attribute_message_agents(messages, "Alpha")
        assert "agent_name" not in messages[0]

    def test_unknown_start_names_nothing_until_a_handover_is_seen(self):
        """Truncated restore, or a conversation with no ``original_agent_id``.

        The walk cannot know who opened the thread, so it says nothing and
        lets the panel fall back — rather than guessing confidently.
        """
        messages = [_assistant("unknown"), _assistant("beta", chain=[{"agent_name": "Beta"}]), _assistant("still beta")]
        attribute_message_agents(messages, None)
        assert [m.get("agent_name") for m in messages] == [None, "Beta", "Beta"]

    def test_malformed_chains_are_ignored_not_fatal(self):
        """``transfer_chain`` is parsed from stored JSON, so treat it as hostile."""
        messages = [
            _assistant(chain=[]),
            _assistant(chain=["not-a-dict"]),
            _assistant(chain=[{"agent_id": "b"}]),
            _assistant(chain=[{"agent_name": ""}]),
        ]
        attribute_message_agents(messages, "Alpha")
        assert [m.get("agent_name") for m in messages] == ["Alpha"] * 4


class TestRecordedAgentsWin:
    """What a message records of its agent wins over the walk, and moves it.

    "Continue with another agent" rebinds the conversation in place with no transfer
    chain: only the note it writes, and the author each answer is stamped with, say who
    holds the conversation after it.
    """

    def test_a_note_hands_the_later_answers_to_its_agent(self):
        messages = [_assistant("by alpha"), _assistant("Continued by Beta"), _user(), _assistant("by beta")]
        for i, m in enumerate(messages):
            m["id"] = f"m{i}"
        attribute_message_agents(messages, "Alpha", {"m1": "Beta"})
        assert [m.get("agent_name") for m in messages] == ["Alpha", "Beta", None, "Beta"]

    def test_a_stamped_author_names_its_answer_and_the_ones_after(self):
        messages = [_assistant("by beta"), _assistant("still beta")]
        messages[0]["id"] = "m0"
        attribute_message_agents(messages, None, {"m0": "Beta"})
        assert [m.get("agent_name") for m in messages] == ["Beta", "Beta"]

    def test_a_mentioned_agent_still_answers_its_one_message(self):
        messages = [_assistant("by beta"), _assistant("by gamma"), _assistant("beta again")]
        messages[0]["id"] = "m0"
        messages[1]["answered_by"] = {"agent_name": "Gamma"}
        attribute_message_agents(messages, "Alpha", {"m0": "Beta"})
        assert [m.get("agent_name") for m in messages] == ["Beta", "Gamma", "Beta"]

    def test_the_general_assistant_is_left_to_the_panel(self):
        messages = [_assistant("by alpha"), _assistant("Continued by General Assistant"), _assistant("by ga")]
        for i, m in enumerate(messages):
            m["id"] = f"m{i}"
        attribute_message_agents(messages, "Alpha", {"m1": None})
        assert [m.get("agent_name") for m in messages] == ["Alpha", None, None]

    def test_the_rows_record_their_note_and_their_authors(self, monkeypatch):
        import asyncio
        import json as _json

        from app.services import conversation_continuity

        beta = uuid.uuid4()

        async def _labels(_db, ids):
            return {i: SimpleNamespace(name="Beta") for i in ids if i == beta}

        monkeypatch.setattr(conversation_continuity, "agent_labels", _labels)
        note = _row("assistant", "Continued by Beta", _json.dumps({"agent_switch": {"to": {"agent_id": str(beta), "agent_name": "Beta"}}}))
        stamped = _row("assistant", "by beta", _json.dumps({"author_agent_id": str(beta)}))
        to_ga = _row("assistant", "Continued by General Assistant", _json.dumps({"agent_switch": {"to": {"general_assistant": True}}}))
        by_ga = _row("assistant", "by ga", _json.dumps({"author_agent_id": None}))
        mention = _row("assistant", "by gamma", _json.dumps({"answered_by": {"agent_id": str(uuid.uuid4())}, "author_agent_id": str(beta)}))
        older = _row("assistant", "before authors", None)
        question = _row("user", "hi", _json.dumps({"author_agent_id": str(beta)}))
        recorded = asyncio.run(recorded_message_agents(None, [note, stamped, to_ga, by_ga, mention, older, question]))  # type: ignore[arg-type]
        assert recorded == {str(note.id): "Beta", str(stamped.id): "Beta", str(to_ga.id): None, str(by_ga.id): None}


class TestSessionResponseCarriesAgent:
    def test_exposes_the_agent_fields(self):
        res = PlatformChatSessionResponse(
            conversation_id="c1",
            messages=[],
            agent_id="a1",
            agent_name="CTEM Assistant",
        )
        assert res.agent_id == "a1"
        assert res.agent_name == "CTEM Assistant"

    def test_agent_is_optional(self):
        """Conversations predating per-conversation routing carry no agent.

        Required fields would 500 the endpoint on exactly the old rows the
        panel most needs to keep listing, so both must stay nullable.
        """
        res = PlatformChatSessionResponse(conversation_id="c1", messages=[])
        assert res.agent_id is None
        assert res.agent_name is None

    def test_serialises_the_keys_even_when_null(self):
        """The panel distinguishes "no agent recorded" from "field absent".

        A missing key reads as an older backend and makes the panel keep its
        previous fallback; an explicit ``null`` says the conversation genuinely
        has no agent. Pydantic includes both by default — pin it, because
        adding ``exclude_none`` anywhere upstream would silently swap the
        meanings.
        """
        dumped = PlatformChatSessionResponse(conversation_id="c1", messages=[]).model_dump()
        assert dumped["agent_id"] is None
        assert dumped["agent_name"] is None


class TestRestoreReportsTheStoredAgent:
    """What the restore says about the conversation's own agent.

    These cases are the ones the nullable contract exists for, and neither was
    guarded until now: a conversation from before per-conversation routing, and
    one whose agent row has since been deleted. Both must answer "I don't know"
    rather than naming the agent the caller happened to ask as — that would put
    a name on a thread the agent never touched, which is the exact defect this
    change set removes.
    """

    @staticmethod
    def _run(
        monkeypatch,
        *,
        conv_agent_id,
        stored_name,
        original_agent_id=None,
        original_name=None,
        rows=None,
        db_out=None,
    ):
        import asyncio
        import uuid as _uuid
        from types import SimpleNamespace

        from app.api.v1 import platform as platform_api

        conv = SimpleNamespace(
            id=_uuid.uuid4(),
            agent_id=conv_agent_id,
            original_agent_id=original_agent_id,
            workspace_id=None,
            channel="platform",
        )
        names = {}
        if conv_agent_id is not None and stored_name is not None:
            names[conv_agent_id] = stored_name
        if original_agent_id is not None and original_name is not None:
            names[original_agent_id] = original_name

        async def _fake_resolve_platform_agent(_db, _agent_slug):
            # Deliberately NOT the conversation's agent: this is the agent the
            # caller asked as, and it must never leak into the answer.
            return SimpleNamespace(id=_uuid.uuid4(), name="Requested Agent")

        monkeypatch.setattr(platform_api, "_resolve_platform_agent", _fake_resolve_platform_agent)

        class _Result:
            def __init__(self, scalar=None, rows=None, pairs=None):
                self._scalar = scalar
                self._rows = rows or []
                self._pairs = pairs or []

            def scalar_one_or_none(self):
                return self._scalar

            def scalars(self):
                return SimpleNamespace(all=lambda: self._rows)

            def all(self):
                return self._pairs

        class _DB:
            """Dispatches on the table a statement reads, never on call order,
            so adding or merging a query cannot silently shift meanings.

            ``rows`` are given oldest first. The fake stands in for the
            restore's ``ORDER BY ... DESC LIMIT n`` page: it hands back the
            newest ``n`` rows, newest first, the shape
            ``chat.slice_message_page`` reads. That the statement really asks
            for this is pinned by ``test_the_query_is_the_web_chat_history_page``.
            """

            def __init__(self):
                self.tables: list[str] = []
                self.statements: list = []

            async def execute(self, statement):
                table = statement.get_final_froms()[0].name
                self.tables.append(table)
                self.statements.append(statement)
                if table == "conversations":
                    return _Result(scalar=conv)
                if table == "messages":
                    newest_first = list(reversed(rows or []))
                    limit = _sql_limit(statement)
                    return _Result(rows=newest_first if limit is None else newest_first[:limit])
                if table == "agents":
                    wanted = set(statement.whereclause.right.value)
                    return _Result(pairs=[(i, n) for i, n in names.items() if i in wanted])
                if table == "message_feedback":
                    return _Result(rows=[])
                raise AssertionError(f"unexpected table {table}")

        db = _DB()
        if db_out is not None:
            db_out.append(db)
        user = SimpleNamespace(id=_uuid.uuid4(), email="u@example.com")
        return asyncio.run(
            platform_api.create_platform_chat_session(
                platform_api.PlatformChatSessionRequest(agent_slug="agent", conversation_id=str(conv.id)),
                db=db,  # type: ignore[arg-type]
                user=user,  # type: ignore[arg-type]
            )
        )

    @staticmethod
    def _assistant_rows(count):
        return [_row("assistant", f"m{i}") for i in range(count)]

    def test_exactly_the_limit_is_a_complete_thread(self, monkeypatch):
        """N messages fit the window: the walk starts at the original agent."""
        import uuid as _uuid

        from app.api.v1.platform import _PLATFORM_CHAT_RESTORE_LIMIT

        original = _uuid.uuid4()
        resp = self._run(
            monkeypatch,
            conv_agent_id=original,
            stored_name="Alpha",
            original_agent_id=original,
            original_name="Alpha",
            rows=self._assistant_rows(_PLATFORM_CHAT_RESTORE_LIMIT),
        )
        assert len(resp.messages) == _PLATFORM_CHAT_RESTORE_LIMIT
        assert all(m["agent_name"] == "Alpha" for m in resp.messages)

    def test_one_past_the_limit_is_truncated_and_trimmed(self, monkeypatch):
        """N + 1 rows: the oldest sentinel is dropped and the walk is skipped."""
        import uuid as _uuid

        from app.api.v1.platform import _PLATFORM_CHAT_RESTORE_LIMIT

        original, current = _uuid.uuid4(), _uuid.uuid4()
        rows = self._assistant_rows(_PLATFORM_CHAT_RESTORE_LIMIT + 1)
        dbs: list = []
        resp = self._run(
            monkeypatch,
            conv_agent_id=current,
            stored_name="Beta",
            original_agent_id=original,
            original_name="Alpha",
            rows=rows,
            db_out=dbs,
        )
        # The production query asks for exactly one sentinel row past the window.
        (message_query,) = [
            s for s, t in zip(dbs[0].statements, dbs[0].tables, strict=True) if t == "messages"
        ]
        assert _sql_limit(message_query) == _PLATFORM_CHAT_RESTORE_LIMIT + 1
        assert len(resp.messages) == _PLATFORM_CHAT_RESTORE_LIMIT
        assert resp.messages[0]["content"] == "m1"
        assert all("agent_name" not in m for m in resp.messages)
        assert resp.agent_name == "Beta"

    def test_one_name_query_for_both_agents(self, monkeypatch):
        import uuid as _uuid

        agent_id = _uuid.uuid4()
        dbs: list = []
        resp = self._run(
            monkeypatch,
            conv_agent_id=agent_id,
            stored_name="Alpha",
            original_agent_id=agent_id,
            original_name="Alpha",
            rows=self._assistant_rows(2),
            db_out=dbs,
        )
        assert dbs[0].tables.count("agents") == 1
        assert resp.agent_name == "Alpha"
        assert [m["agent_name"] for m in resp.messages] == ["Alpha", "Alpha"]

    def test_the_query_is_the_web_chat_history_page(self, monkeypatch):
        """One newest-first page, stable tie-breakers, one sentinel row.

        The web chat's contract (``message_order_desc`` +
        ``slice_message_page``): ``sequence`` alone orders nothing once rows
        share one (legacy ``sequence=0``, the duplicate), and only the
        roles the panel renders take a place in the window. Both names come
        from one ``IN`` lookup, the caller's ratings from another, and
        the statements do not grow with the thread.
        """
        import uuid as _uuid

        from app.api.v1.platform import _PLATFORM_CHAT_RESTORE_LIMIT

        original, current = _uuid.uuid4(), _uuid.uuid4()

        def _statements(count):
            dbs: list = []
            self._run(
                monkeypatch,
                conv_agent_id=current,
                stored_name="Beta",
                original_agent_id=original,
                original_name="Alpha",
                rows=self._assistant_rows(count),
                db_out=dbs,
            )
            return dbs[0]

        db = _statements(3)
        assert db.tables == ["conversations", "messages", "agents", "message_feedback"]
        assert _statements(_PLATFORM_CHAT_RESTORE_LIMIT).tables == db.tables

        messages_sql = _sql(db.statements[1])
        assert "IN (SELECT" not in messages_sql
        assert "messages.role IN ('user', 'assistant')" in messages_sql
        assert messages_sql.endswith(
            "ORDER BY messages.sequence DESC, messages.created_at DESC, messages.id DESC"
            f" LIMIT {_PLATFORM_CHAT_RESTORE_LIMIT + 1}"
        )

        agents_sql = _sql(db.statements[2])
        assert agents_sql.startswith("SELECT agents.id, agents.name FROM agents WHERE agents.id IN (")
        assert str(original) in agents_sql
        assert str(current) in agents_sql

    def test_a_hidden_wake_is_not_restored(self, monkeypatch):
        """A parent wake is the platform's prompt, not the user's words.

        The web chat never renders it (``_message_is_hidden``); restored here
        it came back as a user bubble holding the specialist's raw output.
        """
        import json
        import uuid as _uuid

        from app.services.parent_wake import PARENT_WAKE_SOURCE

        agent_id = _uuid.uuid4()
        wake = json.dumps({"hidden": True, "source": PARENT_WAKE_SOURCE})
        rows = [
            _row("user", "question"),
            _row("assistant", "delegated"),
            _row("user", "task finished: raw output", wake),
            _row("assistant", "summary"),
        ]
        resp = self._run(
            monkeypatch,
            conv_agent_id=agent_id,
            stored_name="Alpha",
            original_agent_id=agent_id,
            original_name="Alpha",
            rows=rows,
        )
        assert [m["content"] for m in resp.messages] == ["question", "delegated", "summary"]
        assert [m.get("agent_name") for m in resp.messages] == [None, "Alpha", "Alpha"]

    def test_conversation_without_an_agent_reports_neither(self, monkeypatch):
        """Predates per-conversation routing, so there is nothing to report.

        The panel then falls back to the selected agent on its own — which is
        also who will answer, since the message route resolves from the slug
        when ``conv.agent_id`` is unset.
        """
        resp = self._run(monkeypatch, conv_agent_id=None, stored_name="Ignored")
        assert resp.agent_id is None
        assert resp.agent_name is None
        # The channel the conversation was started in, as its list row says.
        assert resp.channel == "platform"

    def test_deleted_agent_keeps_the_id_and_drops_the_name(self, monkeypatch):
        """The id is still the truth; the name is simply no longer available.

        Naming the requested agent here would be the original bug in reverse —
        crediting the thread to someone who never wrote in it.
        """
        import uuid as _uuid

        agent_id = _uuid.uuid4()
        resp = self._run(monkeypatch, conv_agent_id=agent_id, stored_name=None)
        assert resp.agent_id == str(agent_id)
        assert resp.agent_name is None

    def test_live_agent_is_reported_over_the_requested_one(self, monkeypatch):
        import uuid as _uuid

        agent_id = _uuid.uuid4()
        resp = self._run(monkeypatch, conv_agent_id=agent_id, stored_name="CTEM Assistant")
        assert resp.agent_id == str(agent_id)
        assert resp.agent_name == "CTEM Assistant"
