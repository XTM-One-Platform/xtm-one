"""The notification a work item asks for at its end, sent by the platform."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.work_item_notify as notify_mod
from app.services.work_item_notify import (
    MAX_ATTEMPTS,
    NotifyError,
    deliver_pending_notifications,
    due_for_delivery,
    ending,
    normalize_notify_payload,
    notification_text,
    notify_payload_for,
    record_attempt,
)

SPEC = {"channel": "teams_dm", "user": "someone@example.com", "integration": "Service account"}


def _item(status: str = "done", *, result: str | None = "Merged as abc123.", payload=None, **extra):
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "title": "Retest the release",
        "status": status,
        "result": result,
        "correlation_key": "handover:example",
        "blocked_on": None,
        "payload": {"notify": dict(SPEC)} if payload is None else payload,
    }
    base.update(extra)
    return SimpleNamespace(**base)


# --- the stored ask ---------------------------------------------------------


def test_normalize_keeps_a_valid_ask_and_drops_unknown_keys():
    out = normalize_notify_payload({"x": 1, "notify": {**SPEC, "channel": "teams", "junk": True}})
    assert out["x"] == 1
    assert out["notify"] == SPEC


@pytest.mark.parametrize(
    "spec, words",
    [
        ({**SPEC, "channel": "sms"}, "not supported"),
        ({**SPEC, "user": "nobody"}, "email address"),
        ({**SPEC, "integration": ""}, "Microsoft integration"),
    ],
)
def test_normalize_refuses_an_ask_it_could_not_send(spec, words):
    with pytest.raises(NotifyError, match=words):
        normalize_notify_payload({"notify": spec})


def test_normalize_refuses_a_notify_that_is_not_an_object():
    with pytest.raises(NotifyError):
        normalize_notify_payload({"notify": "DM me"})


def test_a_payload_without_notify_is_untouched():
    payload = {"a": [1, 2]}
    assert normalize_notify_payload(payload) is payload
    assert normalize_notify_payload(None) is None


def test_the_delivery_state_is_the_platforms_on_update():
    sent = {"notify": {**SPEC, "delivery": {"sent_at": "2026-11-01T00:00:00+00:00", "attempts": 1}}}
    # A writer that drops or forges the state keeps the stored one.
    out = normalize_notify_payload({"notify": {**SPEC, "delivery": {}}}, sent)
    assert out["notify"]["delivery"]["sent_at"] == "2026-11-01T00:00:00+00:00"
    fresh = normalize_notify_payload({"notify": {**SPEC, "delivery": {"sent_at": "forged"}}})
    assert "delivery" not in fresh["notify"]


def test_notify_payload_for_builds_the_ask():
    assert notify_payload_for(user="a@b.c", integration="x", message="Hi") == {
        "channel": "teams_dm",
        "user": "a@b.c",
        "integration": "x",
        "message": "Hi",
    }


def test_create_work_item_refuses_a_bad_ask_before_any_write():
    import asyncio

    from app.services.company_managed_writes import agent_writer
    from app.services.work_item import create_work_item

    with pytest.raises(NotifyError):
        asyncio.run(
            create_work_item(
                user_id=str(uuid.uuid4()),
                title="t",
                payload={"notify": {"channel": "sms"}},
                writer=agent_writer(is_company_managed=False),
            )
        )


# --- when it is due ---------------------------------------------------------


def test_ending_counts_done_cancelled_and_an_exhausted_pickup_only(monkeypatch):
    monkeypatch.setattr(notify_mod.settings, "work_item_max_dispatch_attempts", 3)
    assert ending(_item("done")) == "done"
    assert ending(_item("cancelled")) == "cancelled"
    assert ending(_item("open")) is None
    assert ending(_item("in_progress")) is None
    blocked = {"notify": SPEC, "_dispatch_attempts": 3}
    assert ending(_item("blocked", payload=blocked)) == "blocked"
    assert ending(_item("blocked", payload={"notify": SPEC, "_dispatch_attempts": 1})) is None
    assert ending(_item("blocked", payload=blocked, blocked_on=["x"])) is None


def test_due_for_delivery_honours_sent_backoff_and_give_up():
    now = datetime.now(UTC)
    assert due_for_delivery(_item("done"), now) is not None
    assert due_for_delivery(_item("open"), now) is None
    assert due_for_delivery(_item("done", payload={}), now) is None
    later = {"notify": {**SPEC, "delivery": {"next_attempt_at": (now + timedelta(minutes=1)).isoformat()}}}
    assert due_for_delivery(_item("done", payload=later), now) is None
    sent = {"notify": {**SPEC, "delivery": {"sent_at": now.isoformat()}}}
    assert due_for_delivery(_item("done", payload=sent), now) is None
    gone = {"notify": {**SPEC, "delivery": {"gave_up_at": now.isoformat()}}}
    assert due_for_delivery(_item("done", payload=gone), now) is None


def test_record_attempt_backs_off_then_gives_up():
    now = datetime.now(UTC)
    spec = dict(SPEC)
    for attempt in range(1, MAX_ATTEMPTS):
        spec = record_attempt(spec, now, "boom")
        delivery = spec["delivery"]
        assert delivery["attempts"] == attempt
        assert datetime.fromisoformat(delivery["next_attempt_at"]) == now + timedelta(minutes=2 ** (attempt - 1))
        assert "gave_up_at" not in delivery
    spec = record_attempt(spec, now, "boom")
    assert spec["delivery"]["gave_up_at"] == now.isoformat()
    assert "next_attempt_at" not in spec["delivery"]


def test_record_attempt_marks_a_send():
    now = datetime.now(UTC)
    spec = record_attempt(record_attempt(dict(SPEC), now, "boom"), now, None)
    assert spec["delivery"]["sent_at"] == now.isoformat()
    assert spec["delivery"]["attempts"] == 2
    assert "last_error" not in spec["delivery"]


# --- the text a person reads ------------------------------------------


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_a_message_is_sent_verbatim_and_alone(status):
    item = _item(status, result="Merged as abc123.")
    assert notification_text(item, {**SPEC, "message": "Test notification"}) == "Test notification"


def test_without_a_message_a_done_item_reads_one_plain_sentence():
    assert notification_text(_item("done"), SPEC) == "Retest the release is done."


def test_without_a_message_a_cancelled_item_reads_one_plain_sentence():
    item = _item("cancelled", result="Auto-cancelled by the sweep.")
    assert notification_text(item, SPEC) == "Retest the release was cancelled."
    with_cause = {**SPEC, "include_result": True}
    assert notification_text(item, with_cause) == (
        "Retest the release was cancelled: Auto-cancelled by the sweep."
    )
    assert notification_text(_item("cancelled", result=None), with_cause) == (
        "Retest the release was cancelled."
    )


def test_the_result_is_added_only_on_request():
    asked = {**SPEC, "include_result": True}
    assert notification_text(_item("done"), asked) == "Retest the release is done.\n\nMerged as abc123."
    assert notification_text(_item("done"), {**asked, "message": "Shipped."}) == (
        "Shipped.\n\nMerged as abc123."
    )


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_the_text_never_carries_a_key_an_id_or_a_label(status):
    item = _item(status, result="Something.")
    for spec in (SPEC, {**SPEC, "include_result": True}, {**SPEC, "message": "Hi"}):
        text = notification_text(item, spec)
        assert str(item.id) not in text
        assert item.correlation_key not in text
        for word in ("Key:", "Item:", "Result:", "Cause:", "Work item"):
            assert word not in text


def test_include_result_is_kept_only_when_true():
    kept = normalize_notify_payload({"notify": {**SPEC, "include_result": True}})
    assert kept["notify"]["include_result"] is True
    dropped = normalize_notify_payload({"notify": {**SPEC, "include_result": "yes"}})
    assert "include_result" not in dropped["notify"]


# --- a test item never notifies a real person -------------------------

TEST_INBOX = "assistant@example.com"


@pytest.fixture
def test_inbox(monkeypatch):
    recipients = f" {TEST_INBOX.upper()} ,other@example.com"
    monkeypatch.setattr(notify_mod.settings, "work_item_notify_test_recipients", recipients, raising=False)


@pytest.mark.parametrize(
    "key, title",
    [
        ("retest:notify-guarantee-1", "Anything"),
        ("test:something", "Anything"),
        (None, "Notify retest 2"),
        ("RETEST:upper", "Anything"),
    ],
)
def test_a_test_item_naming_a_real_person_is_refused(test_inbox, key, title):
    with pytest.raises(NotifyError, match="WORK_ITEM_NOTIFY_TEST_RECIPIENTS"):
        normalize_notify_payload({"notify": SPEC}, correlation_key=key, title=title)


def test_a_test_item_may_notify_a_listed_test_recipient(test_inbox):
    out = normalize_notify_payload(
        {"notify": {**SPEC, "user": TEST_INBOX}}, correlation_key="retest:x", title="t"
    )
    assert out["notify"]["user"] == TEST_INBOX


def test_with_no_test_recipient_configured_a_test_item_may_not_notify(monkeypatch):
    monkeypatch.setattr(notify_mod.settings, "work_item_notify_test_recipients", "", raising=False)
    with pytest.raises(NotifyError):
        normalize_notify_payload({"notify": {**SPEC, "user": TEST_INBOX}}, correlation_key="test:x")


def test_an_ordinary_item_may_notify_anyone(test_inbox):
    out = normalize_notify_payload({"notify": SPEC}, correlation_key="handover:x", title="Retest the release")
    assert out["notify"]["user"] == SPEC["user"]


def test_create_work_item_refuses_a_test_item_naming_a_real_person(test_inbox):
    import asyncio

    from app.services.company_managed_writes import agent_writer
    from app.services.work_item import create_work_item

    with pytest.raises(NotifyError, match="test work item"):
        asyncio.run(
            create_work_item(
                user_id=str(uuid.uuid4()),
                title="Notify retest 3",
                correlation_key="retest:notify-guarantee-3",
                payload={"notify": SPEC},
                writer=agent_writer(is_company_managed=False),
            )
        )


# --- the tick ---------------------------------------------------------------


class _Session:
    """The slice of an async session the delivery uses, over a list of items."""

    def __init__(self, items):
        self.items = items
        self.commits = 0
        self.flushed: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        from sqlalchemy.dialects import postgresql

        # The query must compile for PostgreSQL, the JSON paths included.
        self.sql = str(statement.compile(dialect=postgresql.dialect()))
        items = self.items
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(items)))

    async def commit(self):
        self.commits += 1


@pytest.fixture
def tick(monkeypatch):
    """Run the delivery over *items*, with a recording sender."""

    def _run(items, send):
        import asyncio

        session = _Session(items)
        monkeypatch.setattr("app.core.database.async_session", lambda: session)
        return asyncio.run(deliver_pending_notifications(send=send)), session

    return _run


def test_a_completed_item_is_notified_once(tick):
    sent: list = []

    async def send(item, spec, text):
        sent.append((spec["integration"], spec["user"], text))

    item = _item("done")
    count, session = tick([item], send)
    assert count == 1
    assert sent[0][0] == "Service account"
    assert sent[0][1] == "someone@example.com"
    assert sent[0][2] == "Retest the release is done."
    assert item.payload["notify"]["delivery"]["sent_at"]
    assert session.commits == 1
    assert "json_typeof" in session.sql and "FOR UPDATE SKIP LOCKED" in session.sql
    # The next tick finds it delivered: nothing is sent again.
    count, _ = tick([item], send)
    assert count == 0
    assert len(sent) == 1


def test_a_cancelled_item_is_notified_with_its_cause(tick):
    sent: list = []

    async def send(_item, _spec, text):
        sent.append(text)

    item = _item("cancelled", result="Auto-cancelled by the work-item retention sweep.")
    count, _ = tick([item], send)
    assert count == 1
    assert sent[0] == "Retest the release was cancelled."


def test_a_send_cut_by_a_rollout_is_retried_by_the_next_tick(tick):
    calls: list = []

    async def send_fails_once(_item, _spec, text):
        calls.append(text)
        if len(calls) == 1:
            raise ConnectionError("pod stopped mid-send")

    item = _item("done")
    count, _ = tick([item], send_fails_once)
    assert count == 0
    delivery = item.payload["notify"]["delivery"]
    assert delivery["attempts"] == 1
    assert "pod stopped" in delivery["last_error"]
    assert "sent_at" not in delivery
    # Not before its backoff...
    count, _ = tick([item], send_fails_once)
    assert count == 0 and len(calls) == 1
    # ...and once it is due, the next tick sends it.
    delivery["next_attempt_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    count, _ = tick([item], send_fails_once)
    assert count == 1 and len(calls) == 2
    assert item.payload["notify"]["delivery"]["sent_at"]


def test_the_last_failure_tells_the_user_on_the_web(tick, monkeypatch):
    told: list = []

    async def _give_up(_db, item, _spec, error):
        told.append((item.id, error))

    monkeypatch.setattr(notify_mod, "_give_up_on_web", _give_up)

    async def send(*_args):
        raise RuntimeError("Graph 403")

    item = _item(
        "done",
        payload={"notify": {**SPEC, "delivery": {"attempts": MAX_ATTEMPTS - 1}}},
    )
    count, _ = tick([item], send)
    assert count == 0
    assert item.payload["notify"]["delivery"]["gave_up_at"]
    assert told and "Graph 403" in told[0][1]


def test_an_item_that_has_not_ended_is_left_alone(tick):
    async def send(*_args):
        raise AssertionError("must not send")

    count, _ = tick([_item("in_progress"), _item("open")], send)
    assert count == 0


# --- the send goes through the creating agent's own tool policy -------------


_ANSWERS = {
    "create_microsoft_chat": "Chat created. Initial message sent.",
    "send_microsoft_chat_message": "Chat message sent. Message ID: 1.",
    "send_microsoft_channel_message": "Message sent to channel. Message ID: 1.",
}


class _Toolset:
    def __init__(self, definitions, answers=None):
        self.names = [d.name for d in definitions]
        self._by_name = {d.name: d for d in definitions}
        self.calls: list = []
        # Scripted answers, in call order; then each tool's success sentence.
        self.answers = list(answers or [])
        toolset = self

        class _Composite:
            def get_tool_definition(self, name):
                return toolset._by_name.get(name)

            async def execute_tool(self, call):
                toolset.calls.append(call)
                if toolset.answers:
                    return toolset.answers.pop(0)
                base = toolset._by_name[call.name].base_name or call.name
                return SimpleNamespace(is_error=False, content=_ANSWERS[base])

        self.composite = _Composite()


class _AgentSession:
    def __init__(self, agent):
        self.agent = agent
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        agent = self.agent
        return SimpleNamespace(scalar_one_or_none=lambda: agent)

    async def commit(self):
        self.commits += 1


@pytest.fixture
def agent_send(monkeypatch):
    """Run ``_send_teams_dm`` with the toolset the agent's policy leaves."""
    import asyncio

    import app.services.scheduled_work_item_runner as runner
    from app.tools.base import ToolDefinition

    integration_id = str(uuid.uuid4())

    async def _resolve(_db, _item, _reference, _provider="microsoft"):
        return integration_id

    monkeypatch.setattr(notify_mod, "_resolve_integration", _resolve)

    def _run(definitions, *, refusal=None, spec=SPEC, answers=None):
        seen: dict = {}
        toolset = _Toolset(definitions, answers)

        async def _resolve_toolset(_db, agent, user_id, *, pinned=None):
            seen["pinned"] = pinned
            seen["user_id"] = user_id
            return toolset

        async def _refusal(*_args):
            return refusal

        monkeypatch.setattr(runner, "resolve_exact_toolset", _resolve_toolset)
        monkeypatch.setattr(runner, "approval_refusal", _refusal)
        agent = SimpleNamespace(id=uuid.uuid4(), name="Developer")

        async def _usable(_db, _item, _agent_id):
            return agent

        monkeypatch.setattr(notify_mod, "_usable_agent", _usable)
        monkeypatch.setattr("app.core.database.async_session", lambda: _AgentSession(agent))
        item = _item("done", created_by_agent_id=agent.id, owner_agent_id=None)
        asyncio.run(notify_mod._send_teams_dm(item, spec, "hello"))
        return toolset, seen

    def _tool(name="create_microsoft_chat", entity=None, base=None):
        return ToolDefinition(
            name=name, description="", entity_id=entity or integration_id, base_name=base
        )

    return SimpleNamespace(run=_run, tool=_tool, integration_id=integration_id)


def test_the_send_is_the_agents_own_tool_call_with_the_integration_pinned(agent_send):
    toolset, seen = agent_send.run([agent_send.tool()])
    assert seen["pinned"] == {"microsoft": agent_send.integration_id}
    assert toolset.calls[0].name == "create_microsoft_chat"
    assert toolset.calls[0].arguments == {"members": "someone@example.com", "message": "hello"}


def test_the_suffixed_tool_of_the_pinned_account_is_the_one_called(agent_send):
    other = agent_send.tool("create_microsoft_chat__other", entity=str(uuid.uuid4()), base="create_microsoft_chat")
    mine = agent_send.tool("create_microsoft_chat__mine", base="create_microsoft_chat")
    toolset, _ = agent_send.run([other, mine])
    assert [c.name for c in toolset.calls] == ["create_microsoft_chat__mine"]


def test_an_agent_whose_policy_withholds_the_tool_sends_nothing(agent_send):
    # Read-only binding, out-of-scope element, allowed-tools filter or deny
    # list: the toolset the agent's policy leaves has no chat tool.
    with pytest.raises(NotifyError, match="may not send"):
        agent_send.run([agent_send.tool("list_microsoft_chats")])
    # Another account's chat tool is not this integration's.
    with pytest.raises(NotifyError, match="may not send"):
        agent_send.run([agent_send.tool(entity=str(uuid.uuid4()))])


def test_a_tool_that_asks_for_approval_sends_nothing(agent_send):
    with pytest.raises(NotifyError, match="approve"):
        agent_send.run([agent_send.tool()], refusal="Developer asks a person to approve it")


class _Rows:
    """A session answering each ``execute`` with the next value, in order."""

    def __init__(self, *values):
        self.values = list(values)

    async def execute(self, _statement):
        value = self.values.pop(0)
        return SimpleNamespace(scalar_one_or_none=lambda: value)


@pytest.mark.parametrize(
    "agent, user, visible, words",
    [
        (None, None, None, "deleted or disabled"),
        (SimpleNamespace(id=1, name="A", enabled=False), None, None, "deleted or disabled"),
        (SimpleNamespace(id=1, name="A", enabled=True), None, None, "deactivated"),
        (
            SimpleNamespace(id=1, name="A", enabled=True),
            SimpleNamespace(id=2, is_active=False, is_admin=False),
            None,
            "deactivated",
        ),
        (
            SimpleNamespace(id=1, name="A", enabled=True),
            SimpleNamespace(id=2, is_active=True, is_admin=False),
            None,
            "can no longer use",
        ),
    ],
)
def test_a_disabled_agent_or_revoked_access_sends_nothing(monkeypatch, agent, user, visible, words):
    import asyncio

    async def _groups(_db, _uid):
        return []

    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    db = _Rows(*[v for v in (agent, user) if v is not None], visible)
    with pytest.raises(NotifyError, match=words):
        asyncio.run(notify_mod._usable_agent(db, _item("done"), 1))


def test_an_enabled_agent_the_user_may_use_is_the_sender(monkeypatch):
    import asyncio

    async def _groups(_db, _uid):
        return []

    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    agent = SimpleNamespace(id=1, name="A", enabled=True)
    user = SimpleNamespace(id=2, is_active=True, is_admin=False)
    assert asyncio.run(notify_mod._usable_agent(_Rows(agent, user, 1), _item("done"), 1)) is agent


# --- a target that is not a person, the self-recipient, permanent refusals

CHAT_SPEC = {"channel": "teams_dm", "chat_id": "19:test-chat@thread.v2", "integration": "Service account"}
CHANNEL_SPEC = {
    "channel": "teams_channel",
    "team_id": "team-1",
    "channel_id": "19:channel@thread.tacv2",
    "integration": "Service account",
}


def test_normalize_accepts_a_chat_id_and_a_channel():
    assert normalize_notify_payload({"notify": {**CHAT_SPEC, "channel": "teams"}})["notify"] == CHAT_SPEC
    assert normalize_notify_payload({"notify": CHANNEL_SPEC})["notify"] == CHANNEL_SPEC
    # A team and channel id without a channel name is a channel target.
    unnamed = {k: v for k, v in CHANNEL_SPEC.items() if k != "channel"}
    assert normalize_notify_payload({"notify": unnamed})["notify"] == CHANNEL_SPEC
    # A channel target drops a stray user; a chat target keeps no user.
    out = normalize_notify_payload({"notify": {**CHANNEL_SPEC, "user": "a@b.c"}})
    assert "user" not in out["notify"]


@pytest.mark.parametrize(
    "spec, words",
    [
        ({**CHAT_SPEC, "user": "a@b.c"}, "not both"),
        ({**CHAT_SPEC, "chat_id": "19:a/b"}, "not a Teams chat id"),
        ({**CHANNEL_SPEC, "team_id": ""}, "team_id and channel_id"),
        ({**CHANNEL_SPEC, "channel_id": "a b"}, "team_id and channel_id"),
        ({"channel": "teams_dm", "integration": "x"}, "chat_id"),
    ],
)
def test_normalize_refuses_a_target_it_could_not_send_to(spec, words):
    with pytest.raises(NotifyError, match=words):
        normalize_notify_payload({"notify": spec})


def test_notify_payload_for_builds_a_chat_and_a_channel_ask():
    assert notify_payload_for(integration="x", chat_id="19:c") == {
        "channel": "teams_dm",
        "chat_id": "19:c",
        "integration": "x",
    }
    assert notify_payload_for(integration="x", team_id="t", channel_id="c") == {
        "channel": "teams_channel",
        "team_id": "t",
        "channel_id": "c",
        "integration": "x",
    }


def test_a_test_item_may_notify_a_listed_chat_or_channel(monkeypatch):
    monkeypatch.setattr(
        notify_mod.settings,
        "work_item_notify_test_recipients",
        "19:TEST-chat@thread.v2, team-1/19:channel@thread.tacv2",
        raising=False,
    )
    for spec in (CHAT_SPEC, CHANNEL_SPEC):
        normalize_notify_payload({"notify": spec}, correlation_key="retest:x", title="t")
    with pytest.raises(NotifyError, match="WORK_ITEM_NOTIFY_TEST_RECIPIENTS"):
        normalize_notify_payload(
            {"notify": {**CHAT_SPEC, "chat_id": "19:other@thread.v2"}}, correlation_key="retest:x"
        )


def test_a_test_item_may_always_notify_the_accounts_own_chat(monkeypatch):
    monkeypatch.setattr(notify_mod.settings, "work_item_notify_test_recipients", "", raising=False)
    out = normalize_notify_payload(
        {"notify": {**CHAT_SPEC, "chat_id": "48:notes"}}, correlation_key="retest:x", title="Notify retest"
    )
    assert out["notify"]["chat_id"] == "48:notes"


def test_a_chat_target_is_sent_in_that_chat(agent_send):
    tool = agent_send.tool("send_microsoft_chat_message")
    toolset, _ = agent_send.run([tool], spec=CHAT_SPEC)
    assert toolset.calls[0].name == "send_microsoft_chat_message"
    assert toolset.calls[0].arguments == {"chat_id": "19:test-chat@thread.v2", "message": "hello"}


def test_a_channel_target_is_sent_in_that_channel(agent_send):
    tool = agent_send.tool("send_microsoft_channel_message")
    toolset, _ = agent_send.run([tool], spec=CHANNEL_SPEC)
    assert toolset.calls[0].name == "send_microsoft_channel_message"
    assert toolset.calls[0].arguments == {
        "team_id": "team-1",
        "channel_id": "19:channel@thread.tacv2",
        "message": "hello",
    }


_DUPLICATE = SimpleNamespace(
    is_error=True,
    content=(
        "Error: Microsoft Teams rejected the request to create chat \u2014 bad request. "
        "Duplicate chat members is specified in the request body"
    ),
)


def test_a_self_recipient_is_sent_in_the_accounts_own_chat(agent_send):
    tools = [agent_send.tool(), agent_send.tool("send_microsoft_chat_message")]
    toolset, _ = agent_send.run(tools, answers=[_DUPLICATE])
    assert [c.name for c in toolset.calls] == ["create_microsoft_chat", "send_microsoft_chat_message"]
    assert toolset.calls[1].arguments == {"chat_id": "48:notes", "message": "hello"}


def test_a_self_recipient_without_the_chat_tool_ends_naming_the_alternative(agent_send):
    with pytest.raises(notify_mod.PermanentNotifyError, match="chat_id"):
        agent_send.run([agent_send.tool()], answers=[_DUPLICATE])


@pytest.mark.parametrize(
    "content, permanent",
    [
        ("Error: Microsoft Teams rejected the request to send chat message \u2014 bad request. x", True),
        ("Error: Microsoft Teams resource not found while trying to send chat message.", True),
        ("Error: chat_id is missing or invalid ('x').", True),
        ("Error: Microsoft Teams server error (503) while trying to send chat message.", False),
        ("Error: Microsoft Teams rate limit exceeded while trying to create chat.", False),
    ],
)
def test_a_refusal_no_retry_fixes_is_permanent(agent_send, content, permanent):
    answer = SimpleNamespace(is_error=True, content=content)
    tool = agent_send.tool("send_microsoft_chat_message")
    with pytest.raises(NotifyError) as raised:
        agent_send.run([tool], spec=CHAT_SPEC, answers=[answer])
    assert isinstance(raised.value, notify_mod.PermanentNotifyError) is permanent


def test_a_permanent_refusal_gives_up_on_the_first_attempt(tick, monkeypatch):
    told: list = []

    async def _give_up(_db, item, spec, error):
        told.append(error)

    monkeypatch.setattr(notify_mod, "_give_up_on_web", _give_up)

    async def send(*_args):
        raise notify_mod.PermanentNotifyError("bad request. Duplicate chat members")

    item = _item("done")
    count, _ = tick([item], send)
    delivery = item.payload["notify"]["delivery"]
    assert count == 0
    assert delivery["attempts"] == 1
    assert delivery["gave_up_at"]
    assert "next_attempt_at" not in delivery
    assert told and "Duplicate" in told[0]


def test_record_attempt_gives_up_at_once_on_a_permanent_failure():
    now = datetime.now(UTC)
    spec = record_attempt(dict(SPEC), now, "bad request", permanent=True)
    assert spec["delivery"]["gave_up_at"] == now.isoformat()
    assert spec["delivery"]["attempts"] == 1


def test_an_item_without_an_agent_or_user_sends_nothing():
    import asyncio

    with pytest.raises(NotifyError, match="no agent"):
        asyncio.run(
            notify_mod._send_teams_dm(
                _item("done", created_by_agent_id=None, owner_agent_id=None), SPEC, "x"
            )
        )
    with pytest.raises(NotifyError, match="no user"):
        asyncio.run(notify_mod._resolve_integration(None, _item("done", user_id=None), "x"))
