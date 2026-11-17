"""A deactivated owner's assignments are paused unless company-managed.

Unit half. ``test_assignment_owner_deactivation_postgres.py`` proves the rule,
the scheduler's own queries, the trigger funnel, the manual trigger (as a group
member and as an admin), the human-in-the-loop answer, runs created before the
deactivation, queued deliveries and the list / detail flag against PostgreSQL.

Here, without a database: the rule's instance and batch forms, the refusal
every request path answers with, the paths whose wiring is not a query (the
per-assignment webhook, the flow node, the agent-builder and delegation tools),
and a tripwire pinning the rule into every query that picks assignments to fire
and into the chain / retry dispatch of a finished run.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.services import assignment_pause
from app.services.agentic_gate import refusal_code
from app.services.assignment_pause import (
    OWNER_DEACTIVATED_CODE,
    OWNER_DEACTIVATED_MESSAGE,
    AssignmentOwnerDeactivatedError,
    is_paused_by_deactivated_owner,
    owner_not_deactivated,
    paused_entity_ids,
    refuse_if_owner_deactivated,
)

OWNER = uuid.uuid4()


def _assignment(**overrides) -> SimpleNamespace:
    fields = {
        "id": uuid.uuid4(),
        "name": "Nightly triage",
        "agent_id": uuid.uuid4(),
        "user_id": OWNER,
        "is_company_managed": False,
        "enabled": True,
        "trigger_type": "webhook",
        "trigger_config": {},
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


@dataclass
class _RuleDb:
    """Answers the rule's own query with the *paused* entity ids; records
    every statement."""

    paused: frozenset = frozenset()

    def __post_init__(self):
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        # The SQL form itself, never a second definition of the rule.
        assert "FROM assignments" in sql and "users.is_active IS false" in sql, sql
        return _Rows([(uid,) for uid in self.paused])


class _NoDb:
    async def execute(self, statement):
        raise AssertionError(f"no lookup expected, got {statement}")


# ===================================================================
# The rule
# ===================================================================


async def test_company_managed_and_ownerless_assignments_are_never_looked_up():
    """The exemptions cost nothing: no query for them, however their owner is."""
    company = _assignment(is_company_managed=True)
    ownerless = _assignment(user_id=None)

    assert await is_paused_by_deactivated_owner(_NoDb(), company) is False
    assert await is_paused_by_deactivated_owner(_NoDb(), ownerless) is False
    assert await paused_entity_ids(_NoDb(), [company, ownerless]) == set()


async def test_a_private_assignment_is_paused_exactly_while_the_rule_says_so():
    assignment = _assignment()

    assert await is_paused_by_deactivated_owner(_RuleDb({assignment.id}), assignment) is True
    assert await is_paused_by_deactivated_owner(_RuleDb(), assignment) is False
    # Read-only: the owner's own switch and pause stamp are never touched.
    assert assignment.enabled is True
    assert not hasattr(assignment, "paused_reason")


async def test_the_batch_form_asks_once_for_every_candidate():
    other = uuid.uuid4()
    rows = [
        _assignment(),
        _assignment(user_id=other),
        _assignment(user_id=other),
        _assignment(is_company_managed=True),
    ]
    db = _RuleDb({rows[0].id})

    paused = await paused_entity_ids(db, rows)

    assert len(db.statements) == 1
    assert paused == {rows[0].id}


def test_the_sql_form_is_the_negation_of_the_instance_form():
    """Company-managed, or no inactive owner row: ``NOT EXISTS`` lets an
    ownerless assignment through, as the instance form does. No grant is read:
    a shared assignment pauses like any other private one."""
    sql = " ".join(
        str(owner_not_deactivated().compile(dialect=postgresql.dialect())).split()
    )
    assert sql.startswith("assignments.is_company_managed IS true OR NOT (EXISTS")
    assert "users.id = assignments.user_id AND users.is_active IS false" in sql
    assert "shared_" not in sql


@pytest.mark.parametrize(
    ("owner_active", "company_managed", "owned", "fires"),
    [
        (False, False, True, False),
        (False, True, True, True),
        (True, False, True, True),
        (False, False, False, True),
    ],
    ids=["deactivated", "company_managed", "active", "ownerless"],
)
def test_the_in_memory_session_answers_the_rule_like_postgres(
    owner_active, company_managed, owned, fires
):
    """The delivery tests' in-memory session runs this clause itself; its
    correlated ``NOT EXISTS`` reads the session's own user rows."""
    from app.models.assignment import Assignment
    from app.models.user import User
    from tests.integration_credential_fakes import evaluate

    owner = User(id=uuid.uuid4(), is_active=owner_active)
    row = Assignment(
        id=uuid.uuid4(),
        user_id=owner.id if owned else None,
        is_company_managed=company_managed,
    )
    other = User(id=uuid.uuid4(), is_active=False)
    assert evaluate(owner_not_deactivated(), row, {User: [other, owner]}) is fires


def test_the_refusal_is_a_409_a_client_branches_on():
    exc = AssignmentOwnerDeactivatedError(_assignment())

    assert isinstance(exc, HTTPException)
    assert exc.status_code == 409
    assert exc.detail == {
        "code": OWNER_DEACTIVATED_CODE,
        "message": OWNER_DEACTIVATED_MESSAGE,
    }
    # Not the agentic gate's refusal: a push receiver that answers an AI
    # refusal with a quiet 200 must not mistake this one for it.
    assert refusal_code(exc) is None


async def test_refuse_if_owner_deactivated_raises_only_for_a_paused_assignment():
    paused = _assignment()
    with pytest.raises(AssignmentOwnerDeactivatedError):
        await refuse_if_owner_deactivated(_RuleDb({paused.id}), paused)
    await refuse_if_owner_deactivated(_RuleDb(), _assignment())
    await refuse_if_owner_deactivated(_NoDb(), _assignment(is_company_managed=True))


# ===================================================================
# Paths whose wiring is not a query
# ===================================================================


async def test_a_direct_webhook_delivery_is_ignored_with_the_reason(monkeypatch):
    """Answered after the secret check, 200 ``ignored``, never a drift miss,
    never deduplicated or queued: there is nothing to replay later."""
    from app.api.v1 import webhooks

    wf = _assignment()

    class _Result:
        def scalar_one_or_none(self):
            return wf

    class _DB:
        async def execute(self, statement):
            if "FROM users" in str(statement):
                return _Rows([(wf.id,)])
            return _Result()

    class _Session:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _Session())
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    monkeypatch.setattr(webhooks, "_clear_direct_webhook_drift_state", AsyncMock())
    miss = AsyncMock()
    monkeypatch.setattr(webhooks, "_handle_direct_webhook_miss", miss)
    claim = AsyncMock(side_effect=AssertionError("no dedup claim expected"))
    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", claim)
    queue = AsyncMock(side_effect=AssertionError("no trigger expected"))
    monkeypatch.setattr(webhooks, "trigger_or_queue_assignment", queue)

    request = SimpleNamespace(
        headers={},
        method="POST",
        url="http://t",
        client=SimpleNamespace(host="203.0.113.9"),
        body=AsyncMock(return_value=b'{"alert": 1}'),
    )
    response = await webhooks.receive_direct_assignment_webhook(str(wf.id), request)

    assert response == {"status": "ignored", "reason": webhooks._OWNER_DEACTIVATED_REASON}
    miss.assert_not_awaited()


@pytest.mark.parametrize("queue_enabled", [True, False], ids=["queue", "legacy drop"])
async def test_a_direct_delivery_racing_the_deactivation_is_ignored_uncached(
    monkeypatch, queue_enabled
):
    """Active at the pre-claim check, deactivated before the trigger: the
    funnel's refusal becomes the same ``ignored``, the slot released."""
    from app.api.v1 import webhooks

    wf = _assignment()

    class _Result:
        def scalar_one_or_none(self):
            return wf

    class _DB:
        async def execute(self, statement):
            if "FROM users" in str(statement):
                return _Rows([])
            return _Result()

        async def commit(self):
            return None

    class _Session:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _Session())
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)
    monkeypatch.setattr(webhooks.settings, "assignment_queue_enabled", queue_enabled)
    monkeypatch.setattr(webhooks, "_clear_direct_webhook_drift_state", AsyncMock())
    monkeypatch.setattr(webhooks, "_handle_direct_webhook_miss", AsyncMock())
    monkeypatch.setattr(
        webhooks, "_claim_webhook_dedup_slot", AsyncMock(return_value=(False, None, "k"))
    )
    release = AsyncMock()
    monkeypatch.setattr(webhooks, "_release_webhook_dedup_slot", release)
    cache = AsyncMock(side_effect=AssertionError("the answer must not be cached"))
    monkeypatch.setattr(webhooks, "_persist_webhook_dedup_response", cache)
    refusal = AsyncMock(side_effect=AssignmentOwnerDeactivatedError(wf))
    monkeypatch.setattr(webhooks, "trigger_assignment", refusal)
    monkeypatch.setattr(webhooks, "trigger_or_queue_assignment", refusal)

    request = SimpleNamespace(
        headers={},
        method="POST",
        url="http://t",
        client=SimpleNamespace(host="203.0.113.9"),
        body=AsyncMock(return_value=b'{"alert": 1}'),
    )
    response = await webhooks.receive_direct_assignment_webhook(str(wf.id), request)

    assert response == {"status": "ignored", "reason": webhooks._OWNER_DEACTIVATED_REASON}
    release.assert_awaited_once_with("k")
    refusal.assert_awaited_once()


@pytest.mark.parametrize(
    "raced", [False, True], ids=["inactive at the check", "deactivated after it"]
)
@pytest.mark.parametrize(
    ("provider", "body"),
    [
        ("custom", b'{"alert": 1}'),
        ("hubspot", b'[{"subscriptionType": "contact.creation", "objectId": 7}]'),
    ],
    ids=["single event", "HubSpot batch"],
)
async def test_a_per_assignment_delivery_is_answered_without_caching(
    monkeypatch, provider, body, raced
):
    """This URL answers after claiming the dedup slot, so it releases it: a
    cached ``ignored`` would answer the same delivery retried once the owner is
    back, and it would never run. An owner deactivated between the check and
    the trigger (the funnel's refusal) gets the same answer, not a 409."""
    from app.api.v1 import webhooks

    wf = _assignment()
    integration = SimpleNamespace(
        id=uuid.uuid4(), provider=provider, extra_config_enc=None
    )

    class _One:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        async def execute(self, statement):
            sql = str(statement)
            if "FROM users" in sql:
                return _Rows([] if raced else [(wf.id,)])
            if "FROM integrations" in sql:
                return _One(integration)
            return _One(wf)

    class _Session:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _Session())
    monkeypatch.setattr(webhooks, "enforce_rate_limit", AsyncMock())
    monkeypatch.setattr(
        webhooks,
        "_claim_webhook_dedup_slot",
        AsyncMock(return_value=(False, None, "dedup-key")),
    )
    release = AsyncMock()
    monkeypatch.setattr(webhooks, "_release_webhook_dedup_slot", release)
    cache = AsyncMock(side_effect=AssertionError("the answer must not be cached"))
    monkeypatch.setattr(webhooks, "_persist_webhook_dedup_response", cache)
    queue = AsyncMock(side_effect=AssertionError("no trigger expected"))
    monkeypatch.setattr(webhooks, "trigger_or_queue_assignment", queue)
    trigger = AsyncMock(
        side_effect=AssignmentOwnerDeactivatedError(wf)
        if raced
        else AssertionError("no trigger expected")
    )
    monkeypatch.setattr(webhooks, "trigger_assignment", trigger)

    request = SimpleNamespace(
        headers={},
        method="POST",
        url="http://t",
        client=SimpleNamespace(host="203.0.113.9"),
        body=AsyncMock(return_value=body),
    )
    response = await webhooks.receive_assignment_webhook(
        str(integration.id), str(wf.id), request
    )

    assert response == {"status": "ignored", "reason": webhooks._OWNER_DEACTIVATED_REASON}
    release.assert_awaited_once_with("dedup-key")
    assert trigger.await_count == (1 if raced else 0)


async def test_an_event_polled_before_the_owner_was_deactivated_is_given_back(monkeypatch):
    """The owner deactivated between the poller's query and the trigger: the
    event and the cursor the poll advanced are given back, like the AI gate's
    mid-tick refusal, and nothing is raised."""
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
    redis, _poll = _patch_poller(
        monkeypatch, trigger=AsyncMock(side_effect=AssignmentOwnerDeactivatedError(wf))
    )
    cursor_key = f"event_cursor:{wf.id}"
    redis.values[cursor_key] = "before-this-poll"

    triggered = await event_poller.poll_integration_events(
        _Session([[wf], [agent], integration])
    )

    assert triggered == 0
    assert redis.claimed == ["m-1"] and redis.released == ["m-1"]
    assert redis.values.get(cursor_key) == "before-this-poll"
    assert redis.stamped == []


async def test_a_flow_node_that_starts_it_fails_with_the_reason(monkeypatch):
    from app.services import assignment_engine
    from app.services.flow_runner import FlowWalker

    assignment = _assignment()
    agent = SimpleNamespace(id=assignment.agent_id, enabled=True)
    answers = [assignment, agent]

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        async def execute(self, statement):
            return _Result(answers.pop(0))

    async def _refuse(assignment, *_args, **_kwargs):
        raise AssignmentOwnerDeactivatedError(assignment)

    monkeypatch.setattr(assignment_engine, "trigger_assignment", _refuse)
    walker = SimpleNamespace(
        db=_DB(),
        _renderer=None,
        run=SimpleNamespace(id=uuid.uuid4()),
        user=SimpleNamespace(id=uuid.uuid4()),
    )

    result = await FlowWalker._exec_trigger_assignment(
        walker, {"id": "node-1"}, {"assignment_id": str(assignment.id)}
    )

    assert result.error is not None
    assert OWNER_DEACTIVATED_MESSAGE in result.error
    assert result.park is None


@pytest.mark.parametrize(
    "raced", [False, True], ids=["paused at the check", "deactivated after it"]
)
async def test_the_agent_builder_refuses_to_trigger_it(monkeypatch, raced):
    """Deactivated after the tool's own check, the funnel's refusal gets the
    same answer, never the generic support message."""
    from app.api.v1 import helpers as api_helpers
    from app.core import database
    from app.services import assignment_engine
    from app.services.builtin_tools import agent_builder, build_agent_builder_tool
    from app.tools.base import ToolCall

    caller = uuid.uuid4()
    assignment = _assignment(user_id=OWNER)
    agent = SimpleNamespace(id=assignment.agent_id, name="Triage agent", enabled=True)
    answers = [assignment, agent]

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, statement):
            return _Result(answers.pop(0))

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(api_helpers, "get_user_group_ids", AsyncMock(return_value=[]))
    # The caller is loaded before the assignment lookup, which takes the admin
    # bypass for an admin; a member here.
    monkeypatch.setattr(
        agent_builder,
        "_load_caller_user_for_builder",
        AsyncMock(return_value=SimpleNamespace(id=caller, is_admin=False)),
    )
    monkeypatch.setattr(
        assignment_pause,
        "is_paused_by_deactivated_owner",
        AsyncMock(return_value=not raced),
    )
    monkeypatch.setattr(assignment_engine, "find_active_run", AsyncMock(return_value=None))
    trigger = AsyncMock(
        side_effect=AssignmentOwnerDeactivatedError(assignment)
        if raced
        else AssertionError("no trigger expected")
    )
    monkeypatch.setattr(assignment_engine, "trigger_assignment", trigger)

    _, executor = build_agent_builder_tool(None, str(caller))
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="trigger_assignment",
            arguments={"assignment_id": str(assignment.id)},
        )
    )

    body = result.content if isinstance(result.content, str) else str(result.content)
    assert "Nightly triage" in body
    assert OWNER_DEACTIVATED_MESSAGE in body


@pytest.mark.parametrize(
    "raced", [False, True], ids=["paused at the check", "deactivated after it"]
)
async def test_a_delegation_to_it_is_refused(monkeypatch, raced):
    """Checked past the shared manual-trigger gate, before the overlap guard;
    deactivated after that check, the funnel's refusal gets the same answer."""
    from app.api.v1 import helpers as api_helpers
    from app.core import database
    from app.services import assignment_engine
    from app.services.builtin_tools import work_items as wi_tools_mod

    assignment = _assignment()
    caller = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    agent = SimpleNamespace(id=assignment.agent_id, name="Triage agent", enabled=True)
    # The tool reads the caller, then the agent; the assignment lookup is patched.
    answers = [caller, agent]

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, statement):
            return _Result(answers.pop(0))

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(
        api_helpers, "find_user_assignment", AsyncMock(return_value=assignment)
    )
    monkeypatch.setattr(
        assignment_pause,
        "is_paused_by_deactivated_owner",
        AsyncMock(return_value=not raced),
    )
    monkeypatch.setattr(assignment_engine, "find_active_run", AsyncMock(return_value=None))
    trigger = AsyncMock(
        side_effect=AssignmentOwnerDeactivatedError(assignment)
        if raced
        else AssertionError("no trigger expected")
    )
    monkeypatch.setattr(assignment_engine, "trigger_assignment", trigger)

    result = await wi_tools_mod._run_delegate_to_assignment(
        assignment_id=str(assignment.id),
        task="contain",
        acting_user_id=str(caller.id),
        delegating_agent_id=None,
    )

    assert "Nightly triage" in result
    assert OWNER_DEACTIVATED_MESSAGE in result


# ===================================================================
# Tripwire: every query that picks assignments to fire applies the rule
# ===================================================================


def _sources():
    from app.api.v1 import assignments, integration_notifications, webhooks
    from app.services import (
        assignment_engine,
        autonomous,
        event_poller,
        slack_events,
        work_item,
    )

    discovery = {
        "schedule tick": autonomous._process_scheduled_assignments,
        # The inbox selection the dispatcher and the work-item views share.
        "work-item inbox": work_item.load_assignment_inboxes,
        "integration-event poller": event_poller.poll_integration_events,
        "Microsoft Graph push": integration_notifications.microsoft_graph_notification,
        # The receiver hands the message to its dispatcher.
        "Slack push": slack_events.dispatch_slack_message,
        "LinkedIn push": integration_notifications.linkedin_social_action_notification,
        "provider webhook fan-out": webhooks.receive_provider_webhook,
        "HubSpot integration webhook": webhooks._handle_hubspot_webhook,
        "integration webhook fan-out": webhooks.receive_integration_webhook,
        "legacy agent webhook": webhooks.receive_webhook,
        "Schedule tab": assignments.list_assignment_schedule,
    }
    single = {
        "trigger funnel": assignment_engine.trigger_assignment,
        "queued trigger funnel": assignment_engine.trigger_or_queue_assignment,
        "queue drain": assignment_engine._drain_one_assignment,
        "HubSpot per-assignment webhook": webhooks._handle_hubspot_assignment_webhook,
        "per-assignment webhook": webhooks.receive_assignment_webhook,
        "direct assignment webhook": webhooks.receive_direct_assignment_webhook,
        "manual trigger": assignments.trigger_assignment_manual,
        "follow-up run": assignments.followup_run,
        "human-in-the-loop answer": assignments.answer_paused_run,
    }
    return discovery, single


@pytest.mark.parametrize("label", list(_sources()[0]))
def test_every_query_that_picks_assignments_to_fire_applies_the_rule(label):
    """The SQL form is proven against PostgreSQL; this pins where it is used.

    A new discovery query that forgets it would fire a paused assignment's
    trigger into the funnel's 409 on every event or tick instead of skipping it.
    """
    source = inspect.getsource(_sources()[0][label])
    assert "owner_not_deactivated()" in source, label


@pytest.mark.parametrize("label", list(_sources()[1]))
def test_every_path_that_starts_one_assignment_asks_the_rule(label):
    source = inspect.getsource(_sources()[1][label])
    assert (
        "refuse_if_owner_deactivated(" in source
        or "is_paused_by_deactivated_owner(" in source
    ), label


def _automatic_callers():
    from app.api.v1 import integration_notifications, webhooks
    from app.services import assignment_engine, autonomous, event_poller, slack_events

    return {
        "schedule tick": autonomous._process_scheduled_assignments,
        # The inbox pass starts each claimed item through its helper.
        "work-item inbox": autonomous._dispatch_inbox_item,
        # Each page of a poll is dispatched by its own helper.
        "integration-event poller": event_poller._dispatch_poll_page,
        "Microsoft Graph push": integration_notifications.microsoft_graph_notification,
        "Slack push": slack_events.dispatch_slack_message,
        "LinkedIn push": integration_notifications.linkedin_social_action_notification,
        "provider webhook fan-out": webhooks.receive_provider_webhook,
        "HubSpot integration webhook": webhooks._handle_hubspot_webhook,
        "integration webhook fan-out": webhooks.receive_integration_webhook,
        "legacy agent webhook": webhooks.receive_webhook,
        "HubSpot per-assignment webhook": webhooks._handle_hubspot_assignment_webhook,
        "per-assignment webhook": webhooks.receive_assignment_webhook,
        "direct assignment webhook": webhooks.receive_direct_assignment_webhook,
        "chaining": assignment_engine.execute_assignment_run,
    }


@pytest.mark.parametrize("label", list(_automatic_callers()))
def test_every_automatic_caller_absorbs_a_deactivation_racing_its_query(label):
    """The funnel's 409 is the backstop for an owner deactivated between a
    discovery query and the trigger. An automatic caller has nobody to show
    it to: it takes its pause path (skip, give the event back, answer 200)
    instead of raising, logging an error or failing a provider delivery."""
    assert "AssignmentOwnerDeactivatedError" in inspect.getsource(_automatic_callers()[label])


_BUSY_QUEUE_CALLS = frozenset(
    {"queue_busy_integration_event", "queue_busy_pushed_event", "trigger_or_queue_assignment"}
)


def _absorbs_owner_refusal(handler: ast.ExceptHandler) -> bool:
    caught = ast.unparse(handler.type) if handler.type is not None else ""
    return "AssignmentOwnerDeactivatedError" in caught or (
        "HTTPException" in caught and "AssignmentOwnerDeactivatedError" in ast.unparse(handler)
    )


def _busy_queue_calls(func) -> tuple[int, list[int]]:
    """How many busy-queue calls *func* makes, and the lines of those no
    ``try`` absorbing the owner refusal encloses."""
    found = 0
    exposed: list[int] = []

    def visit(node: ast.AST, covered: bool) -> None:
        nonlocal found
        if isinstance(node, ast.Try):
            inside = covered or any(_absorbs_owner_refusal(h) for h in node.handlers)
            for child in node.body:
                visit(child, inside)
            for child in (*node.handlers, *node.orelse, *node.finalbody):
                visit(child, covered)
            return
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in _BUSY_QUEUE_CALLS:
                found += 1
                if not covered:
                    exposed.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, covered)

    visit(ast.parse(textwrap.dedent(inspect.getsource(func))), False)
    return found, exposed


@pytest.mark.parametrize("label", list(_automatic_callers()))
def test_a_busy_assignment_queue_absorbs_a_deactivation_racing_its_trigger(label):
    """Queuing a busy assignment's event asks the rule again (queues go
    through ``trigger_or_queue_assignment``): an owner deactivated between the
    trigger and the queue must meet the same pause path, not escape as a 409
    that fails the provider delivery or the rest of its batch."""
    found, exposed = _busy_queue_calls(_automatic_callers()[label])
    assert exposed == [], f"{label}: busy-queue call outside the owner handler at {exposed}"
    if label not in {"schedule tick", "work-item inbox", "direct assignment webhook", "chaining"}:
        assert found, f"{label}: no busy-queue call found, so nothing was checked"


def test_a_finished_run_neither_chains_into_nor_retries_a_paused_assignment():
    """The start gate, the chain link and the retry of ``execute_assignment_run``.

    Its tail runs after the whole agentic loop, which no unit harness reaches;
    the start gate is exercised against PostgreSQL.
    """
    from app.services.assignment_engine import execute_assignment_run

    source = inspect.getsource(execute_assignment_run)
    assert "is_paused_by_deactivated_owner(db, assignment)" in source
    assert "db, next_assign" in source
    assert "not retried: the owner" in source
