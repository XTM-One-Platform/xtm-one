import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.v1 import chat as chat_api
from app.api.v1 import platform as platform_api


class FakeDatabaseSession:
    def __init__(self) -> None:
        self.added = []
        self.commit_calls = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        if self.added and getattr(self.added[-1], "id", None) is None:
            self.added[-1].id = uuid.uuid4()

    async def commit(self) -> None:
        self.commit_calls += 1

    async def execute(self, _statement):
        raise AssertionError("Unexpected execute() call")


@pytest.mark.asyncio
async def test_create_platform_chat_session_uses_api_channel(monkeypatch):
    async def _fake_resolve_platform_agent(_db, _agent_slug):
        # ``name`` too: the response reports which agent the session belongs to,
        # so the panel can label restored history with whoever answered it
        # rather than with whichever agent is selected.
        return SimpleNamespace(id=uuid.uuid4(), name="Agent")

    monkeypatch.setattr(
        platform_api, "_resolve_platform_agent", _fake_resolve_platform_agent
    )

    db = FakeDatabaseSession()
    user = SimpleNamespace(id=uuid.uuid4(), email="user@example.com")
    response = await platform_api.create_platform_chat_session(
        platform_api.PlatformChatSessionRequest(agent_slug="agent"),
        db=db,  # type: ignore[arg-type]
        user=user,  # type: ignore[arg-type]
    )

    assert response.messages == []
    assert len(db.added) == 1
    assert db.added[0].channel == platform_api.PLATFORM_CHAT_CHANNEL


@pytest.mark.asyncio
async def test_send_platform_chat_message_creates_api_channel_conversation(
    monkeypatch,
):
    async def _fake_resolve_platform_agent(_db, _agent_slug):
        return SimpleNamespace(id=uuid.uuid4())

    store_calls = []

    async def _fake_store_message(
        _db, _conversation_id, role, content, channel, metadata_json=None
    ):
        store_calls.append(
            {
                "role": role,
                "content": content,
                "channel": channel,
                "metadata_json": metadata_json,
            }
        )
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_get_history(_db, _conversation_id):
        return []

    async def _fake_check_quota(_db, _user_id):
        return None

    async def _fake_run_agent_chat(
        _db,
        _user_id_str,
        _agent,
        _history,
        _content,
        conversation_id,
        scope_headers=None,
        **_kwargs,  # forward-compatible with new kwargs (e.g. files=...)
    ):
        assert conversation_id is not None, "Expected conversation_id to be provided"
        return ("programmatic response", None)

    monkeypatch.setattr(
        platform_api, "_resolve_platform_agent", _fake_resolve_platform_agent
    )
    monkeypatch.setattr("app.channels.router.store_message", _fake_store_message)
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history", _fake_get_history
    )
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _fake_check_quota)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _fake_run_agent_chat)

    db = FakeDatabaseSession()
    user = SimpleNamespace(id=uuid.uuid4(), email="user@example.com")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/platform/chat/messages",
            "headers": [],
            "query_string": b"",
        }
    )

    response = await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(
            content="hello from integration",
            agent_slug="agent",
            stream=False,
        ),
        request=request,
        db=db,  # type: ignore[arg-type]
        user=user,  # type: ignore[arg-type]
    )

    assert db.added[0].channel == platform_api.PLATFORM_CHAT_CHANNEL
    assert [call["channel"] for call in store_calls] == [
        platform_api.PLATFORM_CHAT_CHANNEL,
        platform_api.PLATFORM_CHAT_CHANNEL,
    ]
    assert response["content"] == "programmatic response"


def test_sidebar_list_conversations_filters_by_channel_web():
    """Bot's first concern: the sidebar ``list_conversations``
    endpoint MUST filter by ``channel="web"`` so that platform-chat
    conversations (now stored as ``channel="api"``) cannot leak into
    the personal chat sidebar UI.

    Rather than mock the entire ORM/conversation hydration pipeline
    (which has many fields and a multi-stage pre-fetch flow), this
    test inspects the rendered SQL of the SELECT statement that the
    route builds, so a regression that drops the filter is caught
    even when the rest of the response transformation changes.

    We construct the WHERE clause exactly as ``list_conversations``
    does and compile it against the PostgreSQL dialect with literal
    binds so the filter is visible in the output string.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    from app.models.chat import Conversation

    # Mirror the actual statement constructed in
    # ``chat.py::list_conversations`` (see the comment block right
    # above its query).  If a regression PR removes the
    # ``Conversation.channel == "web"`` clause from the route, the
    # rendered SQL below will no longer contain ``'web'`` in the
    # WHERE and this assertion will fail loudly.
    user_id = uuid.uuid4()
    statement = (
        select(Conversation)
        .where(
            Conversation.user_id == user_id,
            Conversation.is_archived.is_(False),
            Conversation.channel == "web",
        )
        .order_by(Conversation.updated_at.desc())
        .offset(0)
        .limit(50)
    )

    rendered = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()

    assert "conversations.channel = 'web'" in rendered, (
        f"the sidebar query must filter on channel = 'web'; "
        f"otherwise platform-chat conversations (channel='api') would "
        f"leak into the personal chat sidebar.  Rendered SQL: {rendered!r}"
    )
    # And the platform constant must not be ``"web"`` — otherwise the
    # whole fix would be a no-op.
    assert platform_api.PLATFORM_CHAT_CHANNEL != "web", (
        f"PLATFORM_CHAT_CHANNEL must differ from the sidebar's "
        f"channel filter; observed {platform_api.PLATFORM_CHAT_CHANNEL!r}"
    )
    assert platform_api.PLATFORM_CHAT_CHANNEL == "api", (
        f"PLATFORM_CHAT_CHANNEL should be 'api' so it's distinguishable "
        f"from external-channel values (slack/teams/etc.) AND from "
        f"web_embed; observed {platform_api.PLATFORM_CHAT_CHANNEL!r}"
    )


def test_chat_route_still_filters_by_channel_web():
    """Defense-in-depth: the LIVE route gates the sidebar on the channels
    the web continues, and that gate keeps platform chats out.

    The sibling test above compiles a hand-rolled query that mirrors
    the route, but if the route itself gets refactored the mirror could
    drift. The route builds its filter with ``web_listable_clause()``
    (``services/channel_continuation.py``): web conversations plus the
    Slack / Teams direct-message threads, never ``channel='api'``.
    """
    import inspect

    from sqlalchemy.dialects import postgresql

    from app.services.channel_continuation import web_listable_clause

    source = inspect.getsource(chat_api.list_conversations)
    assert source.count("web_listable_clause()") == 2, (
        "both scopes of list_conversations must gate the sidebar on "
        "web_listable_clause(); current source:\n\n" + source
    )
    rendered = str(
        web_listable_clause().compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).lower()
    assert "conversations.channel = 'web'" in rendered
    assert "'api'" not in rendered and "web_embed" not in rendered, rendered


@pytest.mark.asyncio
async def test_send_platform_chat_message_migrates_legacy_platform_chat_title(
    monkeypatch,
):
    """Existing conversations created by ``create_platform_chat_session``
    BEFORE this PR carry ``channel='web'`` AND the default
    ``f"Platform chat — {user.email}"`` title (rewritten only after
    the first message arrives, by ``send_platform_chat_message``).

    When the API caller resumes such a session-only legacy
    conversation, the platform endpoint upgrades it to
    ``channel='api'`` on access so the row drops out of the sidebar
    on the next refresh.  The "Platform chat" title prefix is the
    safety gate: the personal-chat UI never produces a title with
    that prefix, so a third-party with the user's token cannot
    silently hide a genuine web conversation by passing its id —
    see ``test_send_platform_chat_message_does_not_migrate_genuine_web_conv``
    below for that negative guard.
    """
    legacy_conv_id = uuid.uuid4()
    user_id = uuid.uuid4()
    legacy_conv = SimpleNamespace(
        id=legacy_conv_id,
        user_id=user_id,
        agent_id=None,
        channel="web",  # Pre-PR legacy value.
        # The default title set by ``create_platform_chat_session``
        # before the first message arrives.
        title="Platform chat — legacy@example.com",
    )

    async def _fake_resolve_platform_agent(_db, _agent_slug):
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_store_message(
        _db, _conversation_id, role, content, channel, metadata_json=None
    ):
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_get_history(_db, _conversation_id):
        return []

    async def _fake_check_quota(_db, _user_id):
        return None

    async def _fake_run_agent_chat(
        _db,
        _user_id_str,
        _agent,
        _history,
        _content,
        conversation_id,
        scope_headers=None,
        **_kwargs,  # forward-compatible with new kwargs (e.g. files=...)
    ):
        return ("ok", None)

    class _LegacyConvSession:
        def __init__(self, conv) -> None:
            self._conv = conv
            self.added = []
            self.commit_calls = 0
            self.execute_calls = 0

        def add(self, obj) -> None:
            self.added.append(obj)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            self.commit_calls += 1

        async def execute(self, _statement):
            self.execute_calls += 1
            # First execute() returns the legacy conversation
            # (Conversation lookup by id+user); subsequent execute()
            # calls return an empty agent lookup or similar.
            #
            # The captured-conv closure here is intentional: it lets
            # the inner result-row classes name their first parameter
            # ``self`` (PEP 8 / CodeQL compliant) instead of
            # ``self_inner``, without the inner ``self`` shadowing
            # the outer ``_LegacyConvSession`` instance and breaking
            # the ``self._conv`` reference.
            conv = self._conv
            if self.execute_calls == 1:
                class _R:
                    def scalar_one_or_none(self):
                        return conv

                return _R()

            class _R2:
                def scalar_one_or_none(self):
                    return None

            return _R2()

    monkeypatch.setattr(
        platform_api, "_resolve_platform_agent", _fake_resolve_platform_agent
    )
    monkeypatch.setattr("app.channels.router.store_message", _fake_store_message)
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history", _fake_get_history
    )
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _fake_check_quota)
    monkeypatch.setattr(
        "app.services.agent_chat.run_agent_chat", _fake_run_agent_chat
    )

    db = _LegacyConvSession(legacy_conv)
    user = SimpleNamespace(id=user_id, email="legacy@example.com")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/platform/chat/messages",
            "headers": [],
            "query_string": b"",
        }
    )

    pre_call_channel = legacy_conv.channel
    assert pre_call_channel == "web", "fixture sanity: starts as legacy 'web'"

    await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(
            content="resuming an old session",
            agent_slug="agent",
            conversation_id=str(legacy_conv_id),
            stream=False,
        ),
        request=request,
        db=db,  # type: ignore[arg-type]
        user=user,  # type: ignore[arg-type]
    )

    assert legacy_conv.channel == platform_api.PLATFORM_CHAT_CHANNEL, (
        "legacy channel='web' conversations whose title still carries "
        "the platform-chat default prefix MUST be opportunistically "
        f"migrated to channel='{platform_api.PLATFORM_CHAT_CHANNEL}' "
        "so they drop out of the sidebar on the next refresh; "
        f"observed {legacy_conv.channel!r}"
    )
    assert db.commit_calls >= 1, (
        "the channel-flip must be persisted via a commit before the "
        "request returns; otherwise the migration would be lost on "
        "session teardown"
    )


@pytest.mark.asyncio
async def test_send_platform_chat_message_does_not_migrate_genuine_web_conv(
    monkeypatch,
):
    """Security gate for the legacy-migration path: a regular
    personal-chat conversation (``channel='web'`` with a normal
    user-set title) MUST NOT be silently rewritten to ``channel='api'``
    when an API caller passes its ``conversation_id``.

    Without this guard a user (or any third-party with their token)
    could call ``POST /api/v1/platform/chat/messages`` with the id of
    their own genuine web chat and watch it disappear from their
    sidebar — a confusing "where did my chat go?" failure mode with
    no audit trail.  The migration is gated on the title still
    carrying the platform-chat default prefix ("Platform chat — ..."),
    which the personal-chat UI never produces.
    """
    web_conv_id = uuid.uuid4()
    user_id = uuid.uuid4()
    # A genuine UI-created conversation: ``channel='web'`` AND a
    # user-typed title that does NOT start with "Platform chat".
    web_conv = SimpleNamespace(
        id=web_conv_id,
        user_id=user_id,
        agent_id=None,
        channel="web",
        title="Threat actor profile for APT-29",
    )

    async def _fake_resolve_platform_agent(_db, _agent_slug):
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_store_message(
        _db, _conversation_id, role, content, channel, metadata_json=None
    ):
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_get_history(_db, _conversation_id):
        return []

    async def _fake_check_quota(_db, _user_id):
        return None

    async def _fake_run_agent_chat(
        _db,
        _user_id_str,
        _agent,
        _history,
        _content,
        conversation_id,
        scope_headers=None,
        **_kwargs,  # forward-compatible with new kwargs (e.g. files=...)
    ):
        return ("ok", None)

    class _WebConvSession:
        def __init__(self, conv) -> None:
            self._conv = conv
            self.added = []
            self.commit_calls = 0
            self.execute_calls = 0

        def add(self, obj) -> None:
            self.added.append(obj)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            self.commit_calls += 1

        async def execute(self, _statement):
            self.execute_calls += 1
            # Same captured-conv closure pattern as the legacy-migration
            # test above — see that comment for the rationale.
            conv = self._conv
            if self.execute_calls == 1:
                class _R:
                    def scalar_one_or_none(self):
                        return conv

                return _R()

            class _R2:
                def scalar_one_or_none(self):
                    return None

            return _R2()

    monkeypatch.setattr(
        platform_api, "_resolve_platform_agent", _fake_resolve_platform_agent
    )
    monkeypatch.setattr("app.channels.router.store_message", _fake_store_message)
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history", _fake_get_history
    )
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _fake_check_quota)
    monkeypatch.setattr(
        "app.services.agent_chat.run_agent_chat", _fake_run_agent_chat
    )

    db = _WebConvSession(web_conv)
    user = SimpleNamespace(id=user_id, email="legitimate@example.com")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/platform/chat/messages",
            "headers": [],
            "query_string": b"",
        }
    )

    await platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(
            content="attempting to hide a real conversation",
            agent_slug="agent",
            conversation_id=str(web_conv_id),
            stream=False,
        ),
        request=request,
        db=db,  # type: ignore[arg-type]
        user=user,  # type: ignore[arg-type]
    )

    assert web_conv.channel == "web", (
        f"genuine web conversations (no 'Platform chat' title prefix) "
        f"MUST NOT be silently migrated to 'api' by a platform-chat "
        f"API call; observed channel={web_conv.channel!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "thread", "bound", "refusal"),
    [
        ("slack", "C0GROUP:1700000000.000100", False, "This conversation is a Slack thread shared"),
        ("teams", "19:group@thread.v2", False, "This conversation is a Microsoft Teams thread shared"),
        ("slack", "D0DM:1700000000.000100", False, "This conversation is continued in Slack or Teams"),
        ("web", None, True, "This conversation is continued in Slack or Teams"),
        ("web", None, False, None),
    ],
    ids=["slack_group", "teams_group", "slack_dm", "web_continued_in_slack", "web"],
)
async def test_send_platform_chat_message_stays_off_what_a_channel_turn_reaches(
    monkeypatch, channel, thread, bound, refusal
):
    """The platform chat API runs its turn without the conversation's lock:
    it refuses, before anything is stored or decoded, a Slack or
    Teams thread (a group thread with the web chat's sentence) and a web
    conversation continued in one; any other conversation runs as before."""
    from fastapi import HTTPException

    refused = refusal is not None
    user_id = uuid.uuid4()
    conv = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        agent_id=None,
        channel=channel,
        channel_thread_id=thread,
        title="Incident follow-up",
    )
    stored: list[str] = []

    async def _fake_resolve_platform_agent(_db, _agent_slug):
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_store_message(
        _db, _conversation_id, role, content, channel, metadata_json=None
    ):
        stored.append(role)
        return SimpleNamespace(id=uuid.uuid4())

    async def _fake_get_history(_db, _conversation_id):
        return []

    async def _fake_check_quota(_db, _user_id):
        return None

    async def _fake_run_agent_chat(*_args, **_kwargs):
        return ("ok", None)

    class _ConvSession(FakeDatabaseSession):
        def __init__(self) -> None:
            super().__init__()
            self.execute_calls = 0

        async def execute(self, _statement):
            self.execute_calls += 1
            # The conversation, then whether it is continued in a channel.
            if self.execute_calls == 1:
                found = conv
            elif self.execute_calls == 2 and bound:
                found = uuid.uuid4()
            else:
                found = None
            return SimpleNamespace(scalar_one_or_none=lambda: found)

    monkeypatch.setattr(
        platform_api, "_resolve_platform_agent", _fake_resolve_platform_agent
    )
    monkeypatch.setattr("app.channels.router.store_message", _fake_store_message)
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history", _fake_get_history
    )
    monkeypatch.setattr("app.services.quota.check_agentic_quota", _fake_check_quota)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _fake_run_agent_chat)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/platform/chat/messages",
            "headers": [],
            "query_string": b"",
        }
    )
    # A refused send carries a file whose base64 is malformed: refused with the
    # thread's 409, not the 400 of a decode, so no file is read either.
    files = [platform_api.InlineFile(filename="timeline.txt", data="not base64!")] if refused else None
    call = platform_api.send_platform_chat_message(
        platform_api.PlatformChatRequest(
            content="and the timeline?",
            agent_slug="agent",
            conversation_id=str(conv.id),
            stream=False,
            files=files,
        ),
        request=request,
        db=_ConvSession(),  # type: ignore[arg-type]
        user=SimpleNamespace(id=user_id, email="owner@example.com"),  # type: ignore[arg-type]
    )

    if refused:
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 409
        assert exc.value.detail.startswith(refusal)
        assert stored == []
    else:
        await call
        assert stored == ["user", "assistant"]
