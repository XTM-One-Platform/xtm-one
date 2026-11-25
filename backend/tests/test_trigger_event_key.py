"""The agent builder writes ``event``; a legacy ``event_type`` row keeps its behaviour.

The agent builder's ``create_assignment`` documented an integration-event
trigger as ``{"provider": ..., "event_type": ...}`` and stored what the model
sent, while every reader (the event poller, the webhook receivers, the
notification handlers) reads ``event``: a polled assignment never ran, a
webhook one ran on every delivery. The builder now documents and stores
``event``. A row stored with ``event_type`` keeps running exactly as it did -
every reader still reads ``event`` alone - until its own trigger is switched
to exact matching (``event_matching: "exact"``): it then loads with ``event``
set, and a polled one starts from now. No data migration.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import inspect

from app.api.v1 import webhooks
from app.models.assignment import (
    EVENT_MATCHING_EXACT,
    EVENT_MATCHING_KEY,
    Assignment,
    _load_trigger_config,
    _refresh_trigger_config,
    _saved_trigger_keeps_its_matching,
    event_read_from_legacy_key,
    normalize_trigger_event_key,
    read_trigger_config,
)

EXACT = {EVENT_MATCHING_KEY: EVENT_MATCHING_EXACT}

BACKEND = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# The key and its legacy spelling
# ---------------------------------------------------------------------------


def test_a_legacy_key_is_moved_to_event_on_integration_event_triggers_only():
    legacy = {"provider": "microsoft", "event_type": "new_email"}
    assert normalize_trigger_event_key("integration_event", legacy) == {
        "provider": "microsoft",
        "event": "new_email",
    }
    # Copy-on-write: the input is untouched.
    assert legacy == {"provider": "microsoft", "event_type": "new_email"}
    # An ``event`` already set wins; the stray legacy key is dropped.
    assert normalize_trigger_event_key(
        "integration_event", {"event": "push", "event_type": "pull_request"}
    ) == {"event": "push"}
    # Other trigger types keep whatever they carry (``event_type`` means
    # nothing to them), and a config with nothing to move is returned as is.
    webhook = {"path": "hook", "event_type": "x"}
    assert normalize_trigger_event_key("webhook", webhook) is webhook
    canonical = {"provider": "github", "event": "push"}
    assert normalize_trigger_event_key("integration_event", canonical) is canonical
    assert normalize_trigger_event_key("integration_event", None) is None


def test_a_legacy_key_naming_no_event_matches_nothing_rather_than_everything():
    """The legacy value moves as it is: a list a model wrote under
    ``event_type`` then matches no delivery, instead of the row reading as no
    selection and running on every delivery of its provider."""
    listed = {"provider": "github", "event_type": ["issues", "pull_request"]}
    normalized = normalize_trigger_event_key("integration_event", listed)
    assert normalized == {"provider": "github", "event": ["issues", "pull_request"]}
    assert not webhooks._event_selected("issues.opened", normalized["event"], exact=False)
    # An ``event`` that names none gives way to a legacy value that does...
    assert normalize_trigger_event_key(
        "integration_event", {"event": "  ", "event_type": "pull_request"}
    ) == {"event": "pull_request"}
    assert normalize_trigger_event_key(
        "integration_event", {"event": 7, "event_type": "pull_request"}
    ) == {"event": "pull_request"}
    # ...never to one that names none either, which would widen it.
    assert normalize_trigger_event_key(
        "integration_event", {"event": ["issues"], "event_type": ""}
    ) == {"event": ["issues"]}


@pytest.mark.parametrize("present", [[], False, 0, {}])
def test_a_present_falsy_event_is_not_widened_by_an_empty_legacy_value(present):
    """A present ``event`` that names nothing is a selection admitting
    nothing, not a missing key: an empty legacy value must not replace it
    with ``""``, which reads as no selection at all."""
    config = {"provider": "github", "event": present, "event_type": ""}
    assert normalize_trigger_event_key("integration_event", config) == {
        "provider": "github",
        "event": present,
    }
    # A legacy value that names an event still gives it one.
    config = {"provider": "github", "event": present, "event_type": "pull_request"}
    assert normalize_trigger_event_key("integration_event", config)["event"] == "pull_request"


@pytest.mark.parametrize("absent", [None, ""])
def test_an_absent_event_takes_the_legacy_value_as_it_is(absent):
    for legacy in ("pull_request", 3, ""):
        config = {"provider": "github", "event": absent, "event_type": legacy}
        assert normalize_trigger_event_key("integration_event", config)["event"] == legacy


# ---------------------------------------------------------------------------
# Read side: the legacy key is read on a trigger marked exact, and only there
# ---------------------------------------------------------------------------


def test_the_listener_runs_on_every_load_and_refresh():
    assert sa_event.contains(Assignment, "load", _load_trigger_config)
    assert sa_event.contains(Assignment, "refresh", _refresh_trigger_config)


def test_a_row_saved_before_exact_matching_loads_as_it_is_stored():
    """Every reader then sees what it saw before this change - no ``event`` -
    so the row keeps running on every delivery of its provider and stays
    unpolled, and the Slack and LinkedIn handlers keep their old answer."""
    config = {"provider": "github", "event_type": "pull_request"}
    row = Assignment(trigger_type="integration_event", trigger_config=config)
    _load_trigger_config(row, None)
    assert row.trigger_config is config
    assert not event_read_from_legacy_key(row)
    assert read_trigger_config("integration_event", config) is config


def test_a_row_marked_exact_loads_with_event_set_and_is_not_written_back():
    row = Assignment(
        trigger_type="integration_event",
        trigger_config={"provider": "github", "event_type": "pull_request", **EXACT},
    )
    state = inspect(row)
    state.attrs.trigger_config.history  # noqa: B018 - materialise the history
    _load_trigger_config(row, None)

    assert row.trigger_config == {"provider": "github", "event": "pull_request", **EXACT}
    # Committed, not dirty: loading a row never schedules a write.
    assert not state.attrs.trigger_config.history.has_changes()


def test_the_global_switch_does_not_read_the_legacy_key(monkeypatch):
    """``WEBHOOK_EXACT_EVENT_MATCHING`` only narrows the match: it never starts
    running a row the poller always skipped."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "webhook_exact_event_matching", True)
    config = {"provider": "microsoft", "event_type": "new_email"}
    row = Assignment(trigger_type="integration_event", trigger_config=config)
    _load_trigger_config(row, None)
    assert row.trigger_config is config


def test_a_canonical_row_is_left_alone():
    for config in ({"provider": "github", "event": "push"}, {"provider": "github", "event": "push", **EXACT}):
        row = Assignment(trigger_type="integration_event", trigger_config=config)
        _load_trigger_config(row, None)
        assert row.trigger_config is config


def test_a_refresh_of_other_columns_keeps_what_the_row_holds():
    stored = {"provider": "github", "event_type": "issues", **EXACT}
    row = Assignment(trigger_type="integration_event", trigger_config=stored)
    _load_trigger_config(row, None)
    _refresh_trigger_config(row, None, {"enabled"})
    assert row.__dict__["_stored_trigger_config"] == stored


class _Result:
    def __init__(self, *, scalar=None, rows=()):
        self._scalar = scalar
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows


class _Session:
    def __init__(self, results):
        self._results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_args, **_kwargs):
        return self._results.pop(0)


async def _snapshot(config: dict):
    agent_id, assignment_id = uuid.uuid4(), uuid.uuid4()
    session = _Session(
        [
            _Result(scalar=None),
            # ``(id, owner, bindings, unreadable)``.
            _Result(rows=[(agent_id, None, None, False)]),
            _Result(rows=[(assignment_id, agent_id, "integration_event", config)]),
        ]
    )
    with patch.object(webhooks, "async_session", new=lambda: session):
        return await webhooks._load_provider_routing("github"), str(assignment_id)


@pytest.mark.asyncio
async def test_the_webhook_routing_snapshot_reads_a_legacy_row_as_it_always_did():
    """The routing cache selects columns, not ORM rows, so the load listener
    does not run there: it applies the listener's own ``read_trigger_config``.
    A row saved before exact matching names no ``event``: it keeps running on
    every delivery of its provider."""
    routing, assignment_id = await _snapshot({"provider": "github", "event_type": "pull_request"})
    (matcher,) = routing.matchers
    assert matcher.event == ""
    for key in ("push", "pull_request.opened"):
        matched = webhooks._match_provider_assignments(routing, "github", key)
        assert [m.assignment_id for m in matched] == [assignment_id]


@pytest.mark.asyncio
async def test_the_webhook_routing_snapshot_reads_the_legacy_key_of_a_trigger_marked_exact():
    routing, assignment_id = await _snapshot({"provider": "github", "event_type": "pull_request", **EXACT})
    (matcher,) = routing.matchers
    assert matcher.event == "pull_request"
    # So a push no longer fires it, and a pull request still does.
    assert webhooks._match_provider_assignments(routing, "github", "push") == []
    matched = webhooks._match_provider_assignments(routing, "github", "pull_request.opened")
    assert [m.assignment_id for m in matched] == [assignment_id]


# ---------------------------------------------------------------------------
# Write side: the agent builder documents and stores ``event``
# ---------------------------------------------------------------------------


def test_the_agent_builder_documents_the_key_the_readers_use():
    from app.services.builtin_tools.agent_builder import build_agent_builder_tool

    definitions, _ = build_agent_builder_tool(None, "")
    by_name = {d.name: d for d in definitions}
    for name in ("create_assignment", "update_assignment"):
        description = by_name[name].input_schema["properties"]["trigger_config"]["description"]
        assert '"event_type"' not in description
    assert '"event": "new_email"' in (
        by_name["create_assignment"].input_schema["properties"]["trigger_config"]["description"]
    )


def test_create_assignment_writes_a_new_trigger():
    source = (BACKEND / "app/services/builtin_tools/agent_builder.py").read_text(encoding="utf-8")
    assert "trigger_type, new_trigger_config(trigger_type, trig_config)" in source


class _BuilderDb:
    """Session stand-in for ``update_assignment``: the rows come from the
    patched loader, so only the commit is observed."""

    def __init__(self):
        self.committed = False

    async def execute(self, _query):
        raise AssertionError("update_assignment ran an unexpected query")

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.asyncio
async def test_update_assignment_stores_the_config_as_sent(monkeypatch):
    """The tool no longer rewrites the key: the save rule of the model decides
    the mode (``settle_event_matching``), so an edit that leaves the events of
    a trigger saved before exact matching alone keeps it as it was."""
    from app.api.v1 import helpers as helpers_mod
    from app.core import database as database_mod
    from app.services import builtin_tools

    caller_id = uuid.uuid4()
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=caller_id,
        is_company_managed=False,
        name="Review PRs",
        description=None,
        trigger_type="integration_event",
        trigger_config={"provider": "github", "event": "push"},
        delay=None,
        enabled=True,
        human_in_the_loop=False,
        ai_label=False,
        activation_prompt=None,
        prompt="Review the pull request.",
        ai_label_prompt=None,
        human_in_the_loop_prompt=None,
    )
    agent = SimpleNamespace(
        id=assignment.agent_id,
        name="Reviewer",
        user_id=caller_id,
        is_company_managed=False,
        shared_group_ids=[],
        shared_group_permissions={},
    )
    db = _BuilderDb()

    async def _groups(_db, _uid):
        return []

    async def _caller(_db, _uid):
        return SimpleNamespace(id=caller_id, is_admin=False, role="member")

    async def _load_writable(_db, _uid, _assignment_uuid, *, action="modify", written=frozenset()):
        return assignment, agent, None

    monkeypatch.setattr(helpers_mod, "get_user_group_ids", _groups)
    monkeypatch.setattr(database_mod, "async_session", lambda: db)
    monkeypatch.setattr(builtin_tools, "_load_caller_user_for_builder", _caller)
    monkeypatch.setattr(builtin_tools, "_load_writable_assignment_for_builder", _load_writable)

    _tools, executor = builtin_tools.build_agent_builder_tool(None, str(caller_id))
    result = await executor._handlers["update_assignment"](
        str(assignment.id),
        trigger_config='{"provider": "github", "event_type": "pull_request"}',
    )

    assert "updated" in result
    assert db.committed
    assert assignment.trigger_config == {"provider": "github", "event_type": "pull_request"}


def _loaded(config: dict) -> Assignment:
    from sqlalchemy.orm.attributes import set_committed_value

    row = Assignment()
    set_committed_value(row, "trigger_type", "integration_event")
    set_committed_value(row, "trigger_config", config)
    _load_trigger_config(row, None)
    return row


def test_a_new_event_under_the_legacy_key_is_a_new_selection():
    """What ``update_assignment`` stores above, on a real row: the event it
    selected (``push``) is gone, so the trigger matches exactly and reads the
    event it now names under the legacy key."""
    row = _loaded({"provider": "github", "event": "push"})
    row.trigger_config = {"provider": "github", "event_type": "pull_request"}
    _saved_trigger_keeps_its_matching(None, None, row)
    assert row.trigger_config == {"provider": "github", "event_type": "pull_request", **EXACT}
    assert read_trigger_config("integration_event", row.trigger_config)["event"] == "pull_request"


def test_an_edit_that_keeps_the_legacy_key_keeps_a_legacy_row_as_it_was():
    row = _loaded({"provider": "microsoft", "event_type": "new_email"})
    row.trigger_config = {"provider": "microsoft", "event_type": "new_email", "filter": {"from_contains": "x"}}
    _saved_trigger_keeps_its_matching(None, None, row)
    assert EVENT_MATCHING_KEY not in row.trigger_config


@pytest.mark.parametrize("marker", [{}, EXACT], ids=["legacy", "exact"])
@pytest.mark.parametrize(
    "config",
    [
        {"provider": "github", "event_type": "pull_request"},
        {"provider": "github", "event": "push", "event_type": "pull_request"},
        {"provider": "github", "event": "", "event_type": "issues"},
        {"provider": "github", "event_type": ""},
        {"provider": "github", "event": "issues"},
    ],
)
def test_the_routing_snapshot_reads_the_key_like_an_orm_row(config, marker):
    """The snapshot selects columns and ORM readers get the load listener:
    both must end up with the same selection."""
    stored = {**config, **marker}
    row = Assignment(trigger_type="integration_event", trigger_config=dict(stored))
    _load_trigger_config(row, None)

    matcher = webhooks._assignment_matcher(
        "github", uuid.uuid4(), uuid.uuid4(), "integration_event", stored
    )

    assert matcher is not None
    assert matcher.event == (row.trigger_config.get("event") or "")


# ---------------------------------------------------------------------------
# A row revived by the legacy key starts from now (zero-regression audit)
# ---------------------------------------------------------------------------


def test_a_row_selecting_its_event_through_the_legacy_key_alone_is_flagged():
    switched = Assignment(
        trigger_type="integration_event",
        trigger_config={"provider": "microsoft", "event_type": "new_email", **EXACT},
    )
    _load_trigger_config(switched, None)
    assert event_read_from_legacy_key(switched)

    for config in (
        # Not switched: the key is not read, so nothing is revived either.
        {"provider": "microsoft", "event_type": "new_email"},
        {"provider": "microsoft", "event": "new_email", **EXACT},
        {"provider": "microsoft", "event": "new_email", "event_type": "new_teams_message", **EXACT},
    ):
        row = Assignment(trigger_type="integration_event", trigger_config=config)
        _load_trigger_config(row, None)
        assert not event_read_from_legacy_key(row)


@pytest.mark.asyncio
async def test_the_poller_still_skips_a_legacy_key_row_that_was_not_switched(monkeypatch):
    """As before this change: the poller reads ``event`` alone."""
    from app.services import event_poller
    from tests.test_background_edition_gate import (
        ENTERPRISE,
        _patch_poller,
        _pin,
        _poller_rows,
        _Session,
    )

    _pin(monkeypatch, **ENTERPRISE)
    wf, agent, integration = _poller_rows()
    wf.trigger_config = {"provider": "microsoft", "event_type": "new_email"}
    trigger = AsyncMock(return_value="run-1")
    _redis, poll = _patch_poller(monkeypatch, trigger=trigger)

    assert await event_poller.poll_integration_events(_Session([[wf], [agent], integration])) == 0
    trigger.assert_not_awaited()
    poll.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_first_poll_of_a_legacy_key_row_runs_nothing_then_new_events_run(monkeypatch):
    """A legacy-key row was never polled, so it has no cursor and its
    first poll returns the provider's latest page - possibly weeks old. That
    page is marked seen; only what arrives afterwards runs."""
    from app.services import event_poller
    from tests.test_background_edition_gate import (
        ENTERPRISE,
        _patch_poller,
        _pin,
        _poller_rows,
        _Session,
    )

    _pin(monkeypatch, **ENTERPRISE)
    wf, agent, integration = _poller_rows()
    wf._event_from_legacy_key = True
    trigger = AsyncMock(return_value="run-1")
    redis, _poll = _patch_poller(monkeypatch, trigger=trigger)

    assert await event_poller.poll_integration_events(_Session([[wf], [agent], integration])) == 0
    trigger.assert_not_awaited()
    assert redis.claimed == ["m-1", "m-2"]
    assert redis.values[f"event_legacy_seeded:{wf.id}"] == "1"

    redis.values.pop(f"event_poll:{wf.id}")
    monkeypatch.setattr(
        event_poller,
        "_poll_provider",
        AsyncMock(return_value=[{"message_id": "m-2"}, {"message_id": "m-3"}]),
    )
    assert await event_poller.poll_integration_events(_Session([[wf], [agent], integration])) == 1
    trigger.assert_awaited_once()
    assert trigger.await_args.kwargs["payload"] == {"message_id": "m-3"}


@pytest.mark.asyncio
async def test_a_failed_first_poll_of_a_legacy_key_row_does_not_count_as_the_seed(monkeypatch):
    """A provider request that fails returns no
    events and writes no cursor. That poll must not mark the row seeded, or
    the next successful poll - still without a cursor - runs the provider's
    latest page as a backlog."""
    from app.services import event_poller
    from tests.test_background_edition_gate import (
        ENTERPRISE,
        _patch_poller,
        _pin,
        _poller_rows,
        _Session,
    )

    _pin(monkeypatch, **ENTERPRISE)
    wf, agent, integration = _poller_rows()
    wf._event_from_legacy_key = True
    trigger = AsyncMock(return_value="run-1")
    redis, _poll = _patch_poller(monkeypatch, trigger=trigger)
    seeded_key = f"event_legacy_seeded:{wf.id}"

    # A non-200 answer: every provider poller returns [] and writes no cursor.
    monkeypatch.setattr(event_poller, "_poll_provider", AsyncMock(return_value=[]))
    assert await event_poller.poll_integration_events(_Session([[wf], [agent], integration])) == 0
    assert seeded_key not in redis.values

    # The next poll succeeds and establishes the cursor: it seeds, nothing runs.
    async def _succeeds(_provider, _event, _token, wf_, redis_, *_a, **_kw):
        await redis_.set(f"event_cursor:{wf_.id}", "after-this-poll")
        return [{"message_id": "m-1"}, {"message_id": "m-2"}]

    monkeypatch.setattr(event_poller, "_poll_provider", AsyncMock(side_effect=_succeeds))
    redis.values.pop(f"event_poll:{wf.id}", None)
    assert await event_poller.poll_integration_events(_Session([[wf], [agent], integration])) == 0
    trigger.assert_not_awaited()
    assert redis.values[seeded_key] == "1"

