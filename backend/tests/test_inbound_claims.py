"""One answer per inbound message on an agent, across its pipelines.

``services/inbound_claims.py`` against an in-memory Redis double; the engine's
use of it (an assignment run skipped when an agent account took the mail) and
the agent account's (a mail an assignment took is left) are tested here with
the claim stubbed.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.models.agent import Agent
from app.models.assignment import Assignment
from app.services import assignment_engine as ae
from app.services import event_run_dedup as dedup
from app.services import inbound_claims
from app.services.agent_accounts import pipeline
from app.services.agent_accounts.events import AccountEvent


class _MemoryRedis:
    """``SET NX EX``, ``GET`` and the release script, with a lock like Redis' single thread."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, nx=False, ex=None):
        await asyncio.sleep(0)
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.values.get(key)

    async def eval(self, _script, _numkeys, key, holder):
        if self.values.get(key) == holder:
            del self.values[key]
            return 1
        return 0


class _BrokenRedis:
    async def set(self, *a, **k):
        raise ConnectionError("down")

    async def eval(self, *a, **k):
        raise ConnectionError("down")


@pytest.fixture
def memory(monkeypatch):
    redis = _MemoryRedis()

    async def _redis():
        return redis

    monkeypatch.setattr(inbound_claims, "_redis", _redis)
    return redis


AGENT = uuid.uuid4()
KEY = "mail:knox@mail.example"


async def test_the_first_holder_wins_and_the_second_is_told_who(memory):
    account = inbound_claims.account_holder("acct-1")
    assert await inbound_claims.claim(AGENT, KEY, account, window_seconds=600) is None
    assert await inbound_claims.claim(AGENT, KEY, inbound_claims.ASSIGNMENT_HOLDER, window_seconds=600) == account
    assert memory.ttls[inbound_claims.claim_key(AGENT, KEY)] == 600


async def test_two_simultaneous_claims_yield_exactly_one_winner(memory):
    holders = [inbound_claims.ASSIGNMENT_HOLDER, inbound_claims.account_holder("acct-1")]
    answers = await asyncio.gather(
        *(inbound_claims.claim(AGENT, KEY, holder, window_seconds=600) for holder in holders)
    )
    assert answers.count(None) == 1
    loser = answers.index(next(a for a in answers if a is not None))
    assert answers[loser] == holders[1 - loser]


async def test_a_claim_is_reentrant_for_its_holder(memory):
    holder = inbound_claims.account_holder("acct-1")
    assert await inbound_claims.claim(AGENT, KEY, holder, window_seconds=600) is None
    assert await inbound_claims.claim(AGENT, KEY, holder, window_seconds=600) is None


async def test_another_agent_or_another_mail_is_not_held(memory):
    holder = inbound_claims.account_holder("acct-1")
    await inbound_claims.claim(AGENT, KEY, holder, window_seconds=600)
    assert await inbound_claims.claim(uuid.uuid4(), KEY, "assignment", window_seconds=600) is None
    assert await inbound_claims.claim(AGENT, "mail:other@mail.example", "assignment", window_seconds=600) is None


async def test_only_the_holder_releases_its_claim(memory):
    holder = inbound_claims.account_holder("acct-1")
    await inbound_claims.claim(AGENT, KEY, holder, window_seconds=600)
    await inbound_claims.release(AGENT, KEY, inbound_claims.ASSIGNMENT_HOLDER)
    assert await inbound_claims.claim(AGENT, KEY, "assignment", window_seconds=600) == holder
    await inbound_claims.release(AGENT, KEY, holder)
    assert await inbound_claims.claim(AGENT, KEY, "assignment", window_seconds=600) is None


async def test_nothing_to_claim_and_redis_down_let_the_caller_act(monkeypatch, memory):
    assert await inbound_claims.claim(None, KEY, "assignment", window_seconds=600) is None
    assert await inbound_claims.claim(AGENT, None, "assignment", window_seconds=600) is None
    assert await inbound_claims.claim(AGENT, KEY, "assignment", window_seconds=0) is None
    assert memory.values == {}

    async def _broken():
        return _BrokenRedis()

    monkeypatch.setattr(inbound_claims, "_redis", _broken)
    assert await inbound_claims.claim(AGENT, KEY, "assignment", window_seconds=600) is None
    await inbound_claims.release(AGENT, KEY, "assignment")


def test_a_holder_is_worded_for_a_skipped_run():
    assert inbound_claims.describe_holder("agent_account:abc") == "the agent account abc"
    assert inbound_claims.describe_holder("assignment") == "an assignment run of this agent"


# -- the engine ---------------------------------------------------------------


class _Session:
    def __init__(self) -> None:
        self.added: list = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        return SimpleNamespace(scalar_one_or_none=lambda: None, scalar=lambda: None)

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


@pytest.fixture
def engine(monkeypatch):
    created: list = []
    counted: list = []

    async def _no(*_a, **_k):
        return None

    async def _false(*_a, **_k):
        return False

    async def _create(db, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    async def _count(assignment_id, count):
        counted.append((assignment_id, count))

    from app.services import integration_event_filters

    monkeypatch.setattr(ae, "_refuse_if_ai_disabled", _false)
    monkeypatch.setattr(ae, "refuse_if_owner_deactivated", _no)
    monkeypatch.setattr(ae, "create_execution", _create)
    monkeypatch.setattr(ae, "notify_orchestration_wake", _no)
    monkeypatch.setattr(ae, "duplicate_event_run", _no)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)
    monkeypatch.setattr(ae.settings, "assignment_event_dedup_window_seconds", 600)
    monkeypatch.setattr(integration_event_filters, "record_filtered_events", _count)
    return SimpleNamespace(created=created, counted=counted)


def _mail(agent: Agent):
    assignment = Assignment(
        id=uuid.uuid4(), agent_id=agent.id, name="Scheduling reply", trigger_type="integration_event",
        trigger_config={}, prompt="Answer", enabled=True,
    )
    payload = {
        "from": "person@example.org", "message_id": "AAMk-1", "conversation_id": "AAQk-1",
        "internet_message_id": "<Knox@mail.example>", "trigger_type": "integration_event",
        "event": "new_email", "provider": "microsoft",
    }
    context = {"type": "integration_event", "provider": "microsoft", "event": "new_email", "message_id": "AAMk-1"}
    return assignment, payload, context


async def test_a_mail_an_agent_account_took_is_a_skipped_run_naming_it(memory, engine):
    agent = Agent(id=uuid.uuid4(), name="Executive Assistant", enabled=True)
    assignment, payload, context = _mail(agent)
    holder = inbound_claims.account_holder("f28538b0")
    await inbound_claims.claim(agent.id, "mail:knox@mail.example", holder, window_seconds=600)
    session = _Session()

    run_id = await ae.trigger_assignment(assignment, agent, payload, db=session, trigger_context=context)

    run = session.added[0]
    assert str(run.id) == run_id and run.status == "skipped"
    assert run.result_summary.startswith(f"{dedup.DUPLICATE_SKIP_PREFIX} the agent account f28538b0")
    assert run.trigger_context[dedup.DUPLICATE_OF_CONTEXT] == holder
    assert engine.created == []
    assert engine.counted == [(assignment.id, 1)]


async def test_a_mail_no_one_took_runs_and_is_claimed_for_the_assignments(memory, engine):
    agent = Agent(id=uuid.uuid4(), name="Executive Assistant", enabled=True)
    assignment, payload, context = _mail(agent)
    session = _Session()

    await ae.trigger_assignment(assignment, agent, payload, db=session, trigger_context=context)

    assert session.added[0].status == "pending"
    assert len(engine.created) == 1
    key = inbound_claims.claim_key(agent.id, "mail:knox@mail.example")
    assert memory.values[key] == inbound_claims.ASSIGNMENT_HOLDER
    # The agent account then hears the same mail: it is told the assignment has it.
    account = inbound_claims.account_holder("f28538b0")
    assert await inbound_claims.claim(agent.id, "mail:knox@mail.example", account, window_seconds=600) == "assignment"


# -- the agent account --------------------------------------------------------


def _event(internet_message_id: str | None) -> AccountEvent:
    from datetime import UTC, datetime

    return AccountEvent(
        provider="microsoft_outlook", conversation_type="direct", conversation_id="AAQk-1", thread_id="AAQk-1",
        message_id="AAMk-2", sender_id="person@example.org", sender_name="Person", text="Tuesday works",
        created_at=datetime(2026, 11, 2, 10, 56, tzinfo=UTC), internet_message_id=internet_message_id,
    )


async def test_an_agent_account_claims_the_mail_under_the_engines_key(memory):
    agent_id = uuid.uuid4()
    account = SimpleNamespace(id="f28538b0", agent_id=agent_id)
    claim = pipeline._InboundClaim.of(account, _event("<Knox@Mail.Example>"))
    assert claim.key == dedup.mail_dedup_key({"internet_message_id": "<knox@mail.example>"})
    assert await claim.take() is None
    assert await inbound_claims.claim(agent_id, claim.key, "assignment", window_seconds=600) == claim.holder
    await claim.give_back()
    assert await inbound_claims.claim(agent_id, claim.key, "assignment", window_seconds=600) is None


async def test_an_agent_account_is_told_an_assignment_took_the_mail(memory):
    agent_id = uuid.uuid4()
    key = dedup.mail_dedup_key({"internet_message_id": "<knox@mail.example>"})
    await inbound_claims.claim(agent_id, key, inbound_claims.ASSIGNMENT_HOLDER, window_seconds=600)
    claim = pipeline._InboundClaim.of(SimpleNamespace(id="acct", agent_id=agent_id), _event("<knox@mail.example>"))
    assert await claim.take() == inbound_claims.ASSIGNMENT_HOLDER


async def test_a_message_without_a_message_id_is_never_claimed(memory):
    claim = pipeline._InboundClaim.of(SimpleNamespace(id="acct", agent_id=uuid.uuid4()), _event(None))
    assert claim.key is None
    assert await claim.take() is None
    assert memory.values == {}


def test_the_message_id_survives_a_parked_event():
    event = _event("<knox@mail.example>")
    assert AccountEvent.from_dict(event.to_dict()).internet_message_id == "<knox@mail.example>"
