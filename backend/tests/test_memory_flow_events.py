"""Memory / knowledge event triggers for agent coordination.

An agent records ``Status: remediation_needed`` and the flow that should act
on it starts within a reconciler tick, instead of waiting for the next
scheduled reconciliation. The mechanism is the flow builder's existing typed
event bus: a builtin ``Memory persisted`` declaration, emitted from the single
durable-write chokepoint, heard by any ``trigger.event`` node bound to it.

The pure-function half (status parsing, preview, filter semantics, debounce
arithmetic) runs anywhere. The dispatch half needs a real database — the
listener query, the run rows and the chain-depth guard are what actually
decide whether a flow reacts — so it uses the same throwaway-database recipe
as ``test_flow_runner_postgres.py``.

Covered, one test per acceptance criterion: event emission, status
token extraction, listener filtering, debounce, self-trigger and recursive
loop prevention, and the trigger payload's shape.

Plus the two halves of keeping ``flow_events`` bounded, which memory writes
made necessary: superseding an entry's older emissions as it is rewritten,
and the age sweep — including the floor that keeps either from stranding a
parked ``Wait for event``.

And the three properties that are only visible from outside one write: the
chain surviving the ``action.trigger_assignment`` process hop (without which
the depth resets and the cycle is unbounded rather than capped), the
attribution that decides whose flows a memory may reach and whose identity
the resulting run carries, and the hook bus firing on the commit rather than
on the write.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.models.agent import Agent
from app.models.flow import FLOW_STATUS_ACTIVE, Flow
from app.models.flow_event import EVENT_CHAIN_MAX_DEPTH, FlowEvent, FlowEventType
from app.models.flow_run import FlowRun
from app.models.user import User
from app.services import flow_events
from app.services import persistent_memory as pm
from app.services.flow_runner import matches_event_filter
from app.services.memory_flow_events import (
    MEMORY_EVENT_PERSISTED,
    content_preview,
    current_memory_chain,
    ensure_memory_event_types,
    extract_status_token,
    reset_memory_chain_context,
    set_memory_chain_context,
)
from tests.agentic_gate_stub import stub_agentic_gate
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    """Listener dispatch asks the background gate first, and the gate
    reads the licence on the app's own engine, not this throwaway database;
    these tests are about the event bus once agentic AI may run."""
    stub_agentic_gate(monkeypatch)


# ═══════════════════════════════════════════════════════════════════════════
# Status token extraction
# ═══════════════════════════════════════════════════════════════════════════


def test_status_token_reads_an_exact_status_line():
    content = "Task: rotate the leaked key\nStatus: remediation_needed\nOwner: soc"
    assert extract_status_token(content) == "remediation_needed"


def test_status_token_is_case_insensitive_on_the_label_and_value():
    assert extract_status_token("STATUS:  Planned  ") == "planned"


def test_status_token_takes_the_last_line_when_a_block_is_rewritten():
    """An operational block rewritten in place appends its new state — the
    newest one is what a listener should route on."""
    content = "Status: planned\nchecked the host\nStatus: completed\n"
    assert extract_status_token(content) == "completed"


def test_status_token_ignores_a_mention_inside_prose():
    """Anchoring matters: a memory *about* statuses must not be classified by
    the words it quotes, or every summary becomes a routing key."""
    content = "The Status: planned entries from last week are stale."
    assert extract_status_token(content) == ""


def test_status_token_rejects_a_token_outside_the_vocabulary():
    assert extract_status_token("Status: whatever_i_want") == ""


def test_status_token_handles_crlf_line_endings():
    """Memory arrives from pasted documents and Windows-authored sources.

    Under ``re.MULTILINE`` the ``$`` anchor matches before the ``\\n``, leaving
    the ``\\r`` between the token and the anchor — so without ``\\r`` in the
    trailing class this parses to no status and the listener silently never
    fires.
    """
    assert (
        extract_status_token("Task\r\nStatus: remediation_needed\r\nOwner: soc")
        == "remediation_needed"
    )
    assert extract_status_token("Status: planned\r\n") == "planned"
    # The anchoring must still reject prose and unknown tokens on CRLF input.
    assert extract_status_token("The Status: planned rows are stale.\r\n") == ""
    assert extract_status_token("Status: whatever_i_want\r\n") == ""


def test_status_token_on_content_without_one():
    assert extract_status_token("The C2 rotates every 6 hours.") == ""
    assert extract_status_token("") == ""
    assert extract_status_token(None) == ""


def test_content_preview_collapses_whitespace_and_truncates():
    preview = content_preview("  alpha \n\n beta   gamma  ")
    assert preview == "alpha beta gamma"

    long_preview = content_preview("x" * 500)
    assert len(long_preview) <= 281  # 280 chars + the ellipsis
    assert long_preview.endswith("…")


# ═══════════════════════════════════════════════════════════════════════════
# Filter semantics
# ═══════════════════════════════════════════════════════════════════════════


def test_filter_list_means_any_of():
    """Subscribing to a SET of status tokens is a filtering case;
    without any-of it would need one trigger node per token."""
    payload = {"status": "remediation_needed", "source": "assignment"}
    assert matches_event_filter(payload, {"status": ["planned", "remediation_needed"]})
    assert not matches_event_filter(payload, {"status": ["planned", "completed"]})


def test_filter_combines_keys_conjunctively():
    payload = {"status": "planned", "source": "tool", "memory_type": "fact"}
    assert matches_event_filter(
        payload, {"status": ["planned"], "source": ["assignment", "tool"]}
    )
    assert not matches_event_filter(
        payload, {"status": ["planned"], "source": ["consolidation"]}
    )


def test_filter_absent_key_never_matches():
    """An optional field the emission dropped (no status line) must not match a
    status filter — silence is not a wildcard."""
    assert not matches_event_filter({"source": "tool"}, {"status": ["planned"]})


def test_filter_none_option_in_a_list_does_not_match_everything():
    """The empty-predicate short-circuit would make a bare ``None`` option a
    wildcard if list options recursed."""
    assert not matches_event_filter({"status": "planned"}, {"status": [None]})


def test_filter_scalar_equality_and_nesting_still_work():
    payload = {"action": "opened", "issue": {"severity": "high"}}
    assert matches_event_filter(payload, {"action": "opened"})
    assert matches_event_filter(payload, {"issue": {"severity": ["high", "critical"]}})
    assert not matches_event_filter(payload, {"action": "closed"})
    assert matches_event_filter(payload, None)


# ═══════════════════════════════════════════════════════════════════════════
# Debounce arithmetic and fail-open
# ═══════════════════════════════════════════════════════════════════════════


def test_debounce_seconds_parsing():
    assert flow_events._debounce_seconds({}) == 0
    assert flow_events._debounce_seconds({"debounce_seconds": ""}) == 0
    assert flow_events._debounce_seconds({"debounce_seconds": 60}) == 60
    assert flow_events._debounce_seconds({"debounce_seconds": " 60 "}) == 60
    assert flow_events._debounce_seconds({"debounce_seconds": 1.9}) == 1
    assert flow_events._debounce_seconds({"debounce_seconds": -5}) == 0
    assert flow_events._debounce_seconds({"debounce_seconds": "soon"}) == 0


class _FakeRedis:
    """SET NX EX over a dict — enough to prove the window, not the clock."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


@pytest.mark.asyncio
async def test_debounce_blocks_the_second_call_inside_the_window():
    fake = _FakeRedis()
    flow_id = uuid.uuid4()
    with patch("app.core.redis.redis_client", new=AsyncMock(return_value=fake)):
        first = await flow_events._debounce_allows(
            flow_id, "n1", {"debounce_seconds": 60}
        )
        second = await flow_events._debounce_allows(
            flow_id, "n1", {"debounce_seconds": 60}
        )
        # A different node keeps its own window — the limit is per listener.
        other = await flow_events._debounce_allows(
            flow_id, "n2", {"debounce_seconds": 60}
        )
    assert (first, second, other) == (True, False, True)


@pytest.mark.asyncio
async def test_debounce_fails_open_when_redis_is_unavailable():
    """Redis trouble must degrade to the pre-debounce behaviour, never to a
    silently deaf trigger."""
    with patch(
        "app.core.redis.redis_client", new=AsyncMock(side_effect=OSError("down"))
    ):
        assert await flow_events._debounce_allows(
            uuid.uuid4(), "n1", {"debounce_seconds": 60}
        )


@pytest.mark.asyncio
async def test_debounce_unset_never_touches_redis():
    client = AsyncMock(side_effect=AssertionError("must not be called"))
    with patch("app.core.redis.redis_client", new=client):
        assert await flow_events._debounce_allows(uuid.uuid4(), "n1", {})


# ═══════════════════════════════════════════════════════════════════════════
# Dispatch — real database
# ═══════════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("memevent", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _listener_graph(event_type_id: uuid.UUID, node_id="t1", **config) -> dict:
    """A minimal ACTIVE listener: one `On event` node, nothing that needs AI.

    Deliberately AI-free — ``hold_back_if_ai_required`` records a *skipped*
    run for an AI-dependent graph on a platform with AI off, which would make
    these assertions measure the AI gate rather than the event dispatch.
    """
    return {
        "nodes": [
            {
                "id": node_id,
                "kind": "trigger.event",
                "position": {"x": 0, "y": 0},
                "config": {"event_type_id": str(event_type_id), **config},
            }
        ],
        "edges": [],
    }


async def _seed(session, *, graphs: list[dict]) -> tuple[User, Agent, list[Flow]]:
    """One owner, one agent to write memory as, and N listener flows."""
    user = User(
        id=uuid.uuid4(),
        email=f"mem-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Memory Event Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()

    agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Recorder")
    session.add(agent)
    await session.flush()

    flows = []
    for index, graph in enumerate(graphs):
        flow = Flow(
            id=uuid.uuid4(),
            user_id=user.id,
            name=f"Listener {index}",
            graph=graph,
            status=FLOW_STATUS_ACTIVE,
        )
        session.add(flow)
        flows.append(flow)

    await ensure_memory_event_types(session)
    await session.commit()
    return user, agent, flows


async def _event_type_id(session) -> uuid.UUID:
    return await session.scalar(
        select(FlowEventType.id).where(
            FlowEventType.name == MEMORY_EVENT_PERSISTED,
            FlowEventType.user_id.is_(None),
        )
    )


async def _write_memory(session, agent, user, content, **kwargs):
    """Persist one durable memory with the embedding provider stubbed out.

    ``(None, None)`` rather than a vector: it skips both the provider call and
    the semantic-dedup query, neither of which these tests are about.
    """
    with patch.object(pm, "embed_text_with_model", new=AsyncMock(return_value=(None, None))):
        entry = await pm.persist_memory(
            session,
            agent_id=str(agent.id),
            content=content,
            memory_type=kwargs.pop("memory_type", "fact"),
            source=kwargs.pop("source", "assignment"),
            user_id=str(user.id),
            **kwargs,
        )
    await session.commit()
    return entry


async def _runs_for(session, flow) -> list[FlowRun]:
    return list(
        (
            await session.execute(
                select(FlowRun).where(FlowRun.flow_id == flow.id)
            )
        )
        .scalars()
        .all()
    )


async def _events(session) -> list[FlowEvent]:
    return list((await session.execute(select(FlowEvent))).scalars().all())


@pytest.mark.asyncio
async def test_a_durable_memory_write_emits_and_starts_the_listener():
    """AC: emission + trigger without waiting for scheduled reconciliation,
    and the payload shape the issue specifies."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        type_id = None
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        # The graph is written before the seeded type exists, so bind it now.
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)
        await session.commit()

        entry = await _write_memory(
            session,
            agent,
            user,
            "Rotate the leaked deploy key.\nStatus: remediation_needed\n",
        )

        events = await _events(session)
        assert len(events) == 1
        assert events[0].event_type_id == type_id

        runs = await _runs_for(session, listener)
        assert len(runs) == 1
        run = runs[0]
        assert run.trigger_type == "event"

        fields = run.trigger_payload["fields"]
        assert fields["memory_id"] == str(entry.id)
        assert fields["agent_id"] == str(agent.id)
        assert fields["user_id"] == str(user.id)
        assert fields["memory_type"] == "fact"
        assert fields["source"] == "assignment"
        assert fields["status"] == "remediation_needed"
        assert fields["content_preview"].startswith("Rotate the leaked deploy key.")
        assert fields["created_at"]
        # The preview is a preview: the full entry is fetched by memory_id.
        assert "content" not in fields

        assert run.trigger_payload["event"]["name"] == MEMORY_EVENT_PERSISTED
        assert run.trigger_context["event_id"] == str(events[0].id)


@pytest.mark.asyncio
async def test_the_listener_filter_selects_only_actionable_statuses():
    """AC: filtering on status token, memory source and memory type."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(
            type_id,
            filter={
                "status": ["remediation_needed", "failed_verification"],
                "source": ["assignment"],
                "memory_type": "fact",
            },
        )
        await session.commit()

        # Wrong status → recorded, but no run.
        await _write_memory(session, agent, user, "Done.\nStatus: completed\n")
        assert len(await _events(session)) == 1
        assert await _runs_for(session, listener) == []

        # No status line at all → the filter key is absent → no run.
        await _write_memory(session, agent, user, "The C2 rotates every 6h.")
        assert len(await _events(session)) == 2
        assert await _runs_for(session, listener) == []

        # Right status, wrong source → no run.
        await _write_memory(
            session,
            agent,
            user,
            "Check failed.\nStatus: failed_verification\n",
            source="chat",
        )
        assert await _runs_for(session, listener) == []

        # All three match → exactly one run.
        await _write_memory(
            session, agent, user, "Fix me.\nStatus: remediation_needed\n"
        )
        runs = await _runs_for(session, listener)
        assert len(runs) == 1
        assert runs[0].trigger_payload["fields"]["status"] == "remediation_needed"


@pytest.mark.asyncio
async def test_the_listener_filter_selects_a_source_agent():
    """AC: filtering on source agent ID — one coordinator watching one worker,
    not every agent on the platform."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, watched, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        other = Agent(id=uuid.uuid4(), user_id=user.id, name="Unrelated")
        session.add(other)
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(
            type_id, filter={"agent_id": str(watched.id)}
        )
        await session.commit()

        await _write_memory(session, other, user, "Theirs.\nStatus: planned\n")
        assert await _runs_for(session, listener) == []

        await _write_memory(session, watched, user, "Mine.\nStatus: planned\n")
        runs = await _runs_for(session, listener)
        assert len(runs) == 1
        assert runs[0].trigger_payload["fields"]["agent_id"] == str(watched.id)


@pytest.mark.asyncio
async def test_debounce_bounds_a_write_storm_without_losing_the_events():
    """AC: debounce prevents excessive duplicate triggers.

    The event rows are still written — the audit trail and parked waiters must
    not lose emissions because one listener is rate limited.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id, debounce_seconds=60)
        await session.commit()

        fake = _FakeRedis()
        with patch(
            "app.core.redis.redis_client", new=AsyncMock(return_value=fake)
        ):
            for index in range(4):
                await _write_memory(
                    session,
                    agent,
                    user,
                    f"Reconciled fact {index}.\nStatus: planned\n",
                )

        assert len(await _events(session)) == 4
        assert len(await _runs_for(session, listener)) == 1


@pytest.mark.asyncio
async def test_a_filtered_out_event_does_not_consume_the_debounce_window():
    """The window belongs to runs that actually start.

    Checked before the filter, a stream of memory the listener does not care
    about would claim the window and silence the one write it was waiting for
    — the failure mode is a trigger that looks configured and never fires.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(
            type_id,
            filter={"status": ["remediation_needed"]},
            debounce_seconds=60,
        )
        await session.commit()

        fake = _FakeRedis()
        with patch("app.core.redis.redis_client", new=AsyncMock(return_value=fake)):
            for index in range(3):
                await _write_memory(
                    session, agent, user, f"Noise {index}.\nStatus: planned\n"
                )
            assert await _runs_for(session, listener) == []

            await _write_memory(
                session, agent, user, "Act on me.\nStatus: remediation_needed\n"
            )

        assert len(await _runs_for(session, listener)) == 1


@pytest.mark.asyncio
async def test_a_flow_never_triggers_itself_from_its_own_memory_write():
    """AC: self-trigger prevention.

    The writer is excluded outright rather than merely depth-bounded — a flow
    whose own run wrote the memory would otherwise re-enter on every write.
    Any OTHER listener still hears it: this excludes the writer, not the event.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (writer, bystander) = await _seed(
            session,
            graphs=[
                _listener_graph(uuid.uuid4()),
                _listener_graph(uuid.uuid4()),
            ],
        )
        type_id = await _event_type_id(session)
        writer.graph = _listener_graph(type_id)
        bystander.graph = _listener_graph(type_id)
        await session.commit()

        # What FlowWalker.run_burst binds around a burst.
        token = set_memory_chain_context(writer.id, 0)
        try:
            await _write_memory(
                session, agent, user, "Wrote this.\nStatus: planned\n"
            )
        finally:
            reset_memory_chain_context(token)

        assert await _runs_for(session, writer) == []
        assert len(await _runs_for(session, bystander)) == 1


@pytest.mark.asyncio
async def test_a_cross_flow_memory_cycle_dies_at_the_chain_depth_cap():
    """AC: recursive loops safely bounded.

    A → B → A cannot be caught by the self-exclusion above, so it converges on
    EVENT_CHAIN_MAX_DEPTH instead: at the cap the event is still recorded (for
    the audit trail and for parked waiters) but starts no further runs.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (peer,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        peer.graph = _listener_graph(type_id)
        await session.commit()

        # A run already EVENT_CHAIN_MAX_DEPTH deep in the chain: its write
        # emits at depth + 1, one past the cap.
        token = set_memory_chain_context(uuid.uuid4(), EVENT_CHAIN_MAX_DEPTH)
        try:
            await _write_memory(
                session, agent, user, "Deep.\nStatus: planned\n"
            )
        finally:
            reset_memory_chain_context(token)

        events = await _events(session)
        assert len(events) == 1
        assert events[0].chain_depth == EVENT_CHAIN_MAX_DEPTH + 1
        assert await _runs_for(session, peer) == []


@pytest.mark.asyncio
async def test_rewriting_a_consolidated_memory_emits_and_supersedes():
    """The upsert path is a durable write like any other — a rewritten
    operational block is exactly the state change a listener cares about.

    Both writes dispatch (a run each, at their own emit time), but only the
    newest emission is KEPT: the earlier one describes a state of that memory
    that no longer exists, and it can no longer start anything.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)
        await session.commit()

        first = await _write_memory(
            session,
            agent,
            user,
            "Summary.\nStatus: planned\n",
            memory_type="consolidated",
            source="consolidation",
        )
        second = await _write_memory(
            session,
            agent,
            user,
            "Summary.\nStatus: completed\n",
            memory_type="consolidated",
            source="consolidation",
        )
        # One memory row, upserted — and one surviving emission, the newest.
        assert first.id == second.id
        events = await _events(session)
        assert len(events) == 1
        assert events[0].payload["status"] == "completed"
        assert events[0].payload["memory_id"] == str(first.id)

        # Superseding is bookkeeping, not suppression: both writes already
        # dispatched at their own emit time and both runs stand.
        runs = await _runs_for(session, listener)
        statuses = sorted(r.trigger_payload["fields"]["status"] for r in runs)
        assert statuses == ["completed", "planned"]


@pytest.mark.asyncio
async def test_superseding_only_touches_the_same_memory():
    """Each memory entry keeps its own newest emission — a busy agent writing
    many facts must not erase the events of the others."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)
        await session.commit()

        # Three distinct facts — distinct memory ids, nothing superseded.
        for index in range(3):
            await _write_memory(
                session, agent, user, f"Fact {index}.\nStatus: planned\n"
            )
        assert len(await _events(session)) == 3

        # Rewriting the consolidated memory twice collapses to one, and leaves
        # the three facts untouched.
        for status in ("planned", "completed"):
            await _write_memory(
                session,
                agent,
                user,
                f"Summary.\nStatus: {status}\n",
                memory_type="consolidated",
                source="consolidation",
            )
        assert len(await _events(session)) == 4


@pytest.mark.asyncio
async def test_a_parked_waiter_still_hears_the_surviving_emission():
    """The safety property behind superseding.

    A ``Wait for event`` polls for the first emission after it parked and never
    marks one consumed, so deleting rows out from under it would strand the run
    — unless the newest always survives, which moves the waiter onto the
    current state instead.
    """
    from app.services.flow_events import find_matching_event

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)
        await session.commit()

        parked_at = datetime.now(UTC).isoformat()
        for status in ("planned", "running", "completed"):
            await _write_memory(
                session,
                agent,
                user,
                f"Summary.\nStatus: {status}\n",
                memory_type="consolidated",
                source="consolidation",
            )

        heard = await find_matching_event(
            session,
            event_type_id=str(type_id),
            user_id=user.id,
            flow_id=listener.id,
            run_id=uuid.uuid4(),
            after_iso=parked_at,
        )
        assert heard is not None
        assert heard.payload["status"] == "completed"


@pytest.mark.asyncio
async def test_a_memory_write_survives_a_broken_event_declaration():
    """Fail-open: recording what an agent learned must never depend on the
    flow bus. An edited declaration the payload no longer satisfies skips the
    emission, it does not lose the memory."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)

        declaration = await session.get(FlowEventType, type_id)
        declaration.fields = [
            *declaration.fields,
            {"name": "nonexistent", "type": "string", "required": True},
        ]
        await session.commit()

        entry = await _write_memory(
            session, agent, user, "Still durable.\nStatus: planned\n"
        )

        assert entry is not None
        assert await session.get(type(entry), entry.id) is not None
        assert await _events(session) == []
        assert await _runs_for(session, listener) == []


# ═══════════════════════════════════════════════════════════════════════════
# The chain across the process boundary
# ═══════════════════════════════════════════════════════════════════════════


def test_the_handoff_serialises_the_bound_chain():
    """What ``action.trigger_assignment`` freezes onto the run row."""
    from app.services.flow_runner import _memory_chain_handoff

    assert _memory_chain_handoff() is None  # nothing bound, nothing added

    flow_id = uuid.uuid4()
    token = set_memory_chain_context(flow_id, 3)
    try:
        assert _memory_chain_handoff() == {
            "flow_id": str(flow_id),
            "chain_depth": 3,
        }
    finally:
        reset_memory_chain_context(token)


@pytest.mark.asyncio
async def test_the_worker_rebinds_the_chain_frozen_on_the_run():
    """The other half of the hop, and the reason the cycle stays bounded.

    Without this the worker's memory writes emit at depth 0 with no
    originating flow, so a flow whose assignment writes memory retriggers
    itself indefinitely — the depth cap is never approached because the depth
    resets, not merely weakens.
    """
    from app.models.assignment import AssignmentRun
    from app.services.orchestration_adapters.assignment import _bind_memory_chain
    from app.services.orchestration_adapters.base import AdapterContext

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner, agent, (flow,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        run = AssignmentRun(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=owner.id,
            status="pending",
            trigger_context={
                "type": "flow",
                "memory_chain": {"flow_id": str(flow.id), "chain_depth": 4},
            },
        )
        session.add(run)
        await session.commit()

        ctx = AdapterContext(db=session, worker_id="test")
        token = await _bind_memory_chain(ctx, run.id)
        try:
            chain = current_memory_chain()
            assert chain is not None
            assert chain.flow_id == flow.id
            assert chain.chain_depth == 4
        finally:
            reset_memory_chain_context(token)
        assert current_memory_chain() is None

        # A run with no chain (schedule, webhook, manual) binds nothing and
        # must never block the assignment from executing.
        plain = AssignmentRun(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=owner.id,
            status="pending",
            trigger_context={"type": "schedule"},
        )
        session.add(plain)
        await session.commit()
        assert await _bind_memory_chain(ctx, plain.id) is None


@pytest.mark.asyncio
async def test_a_rebound_chain_keeps_the_worker_from_retriggering_its_flow():
    """End to end: with the chain rebound, the dispatching flow is excluded
    from its own assignment's memory writes and the depth carries forward."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner, agent, (dispatcher, bystander) = await _seed(
            session,
            graphs=[_listener_graph(uuid.uuid4()), _listener_graph(uuid.uuid4())],
        )
        type_id = await _event_type_id(session)
        dispatcher.graph = _listener_graph(type_id)
        bystander.graph = _listener_graph(type_id)
        await session.commit()

        # What the adapter establishes before running the assignment.
        token = set_memory_chain_context(dispatcher.id, 4)
        try:
            await _write_memory(
                session, agent, owner, "Worker wrote this.\nStatus: planned\n"
            )
        finally:
            reset_memory_chain_context(token)

        assert await _runs_for(session, dispatcher) == []
        assert len(await _runs_for(session, bystander)) == 1
        events = await _events(session)
        assert len(events) == 1
        assert events[0].chain_depth == 5


# ═══════════════════════════════════════════════════════════════════════════
# Attribution — who a memory emission may reach, and run as
# ═══════════════════════════════════════════════════════════════════════════


async def _add_user(session, label: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{label}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=label,
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_one_users_memory_never_starts_another_users_flow():
    """The isolation property of a global-scope emission.

    Every listener binds the same builtin declaration, so nothing but the
    emission's attribution keeps a memory from reaching a flow that has no
    business seeing its content preview.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        owner, agent, (mine,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        stranger = await _add_user(session, "stranger")
        type_id = await _event_type_id(session)
        mine.graph = _listener_graph(type_id)
        theirs = Flow(
            id=uuid.uuid4(),
            user_id=stranger.id,
            name="Someone else's listener",
            graph=_listener_graph(type_id),
            status=FLOW_STATUS_ACTIVE,
        )
        session.add(theirs)
        await session.commit()

        await _write_memory(session, agent, owner, "Mine.\nStatus: planned\n")

        assert len(await _runs_for(session, mine)) == 1
        assert await _runs_for(session, theirs) == []


@pytest.mark.asyncio
async def test_an_agent_level_memory_runs_as_the_agents_owner():
    """An agent-level memory (``user_id IS NULL``) has no scope owner.

    Passing that None through made the emission ownerless, which reaches every
    company-managed flow platform-wide and — because company-managed flows keep
    their creator — started the run under THAT creator's identity: their
    quotas, their integration bindings, for a memory that was never theirs.
    Resolving the agent's owner keeps the run attributable to the write.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        agent_owner = await _add_user(session, "agent-owner")
        creator = await _add_user(session, "flow-creator")
        agent = Agent(id=uuid.uuid4(), user_id=agent_owner.id, name="Recorder")
        session.add(agent)
        await ensure_memory_event_types(session)
        await session.flush()
        type_id = await _event_type_id(session)

        # Company-managed, but the creator is preserved on the row — the exact
        # shape that made the old fallback reachable.
        company_flow = Flow(
            id=uuid.uuid4(),
            user_id=creator.id,
            name="Company listener",
            graph=_listener_graph(type_id),
            status=FLOW_STATUS_ACTIVE,
            is_company_managed=True,
        )
        session.add(company_flow)
        await session.commit()

        with patch.object(
            pm, "embed_text_with_model", new=AsyncMock(return_value=(None, None))
        ):
            await pm.persist_memory(
                session,
                agent_id=str(agent.id),
                content="Agent-level lesson.\nStatus: planned\n",
                memory_type="fact",
                source="assignment",
                user_id=None,
            )
        await session.commit()

        events = await _events(session)
        assert len(events) == 1
        assert events[0].user_id == agent_owner.id

        runs = await _runs_for(session, company_flow)
        assert len(runs) == 1
        assert runs[0].user_id == agent_owner.id
        assert runs[0].user_id != creator.id


# ═══════════════════════════════════════════════════════════════════════════
# Platform hook bus — the half that escapes the transaction
# ═══════════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def _capturing_hook_deliveries() -> AsyncIterator[list]:
    """Record every ``memory.persisted`` reaching the platform hook bus."""
    from app.services.event_bus import MEMORY_PERSISTED, event_bus

    received: list = []

    async def _listener(event) -> None:
        received.append(event.data)

    event_bus.add_listener(MEMORY_PERSISTED, _listener)
    try:
        yield received
    finally:
        event_bus.remove_listener(MEMORY_PERSISTED, _listener)


async def _settle() -> None:
    """Let the after-commit delivery task run — it is scheduled, not awaited."""
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_hook_bus_hears_a_memory_only_once_it_is_committed():
    """A hook listener is an outbound POST to a third party: the one half of
    the announcement a rollback cannot take back. It must therefore fire on
    the commit, not on the write."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)
        await session.commit()

        async with _capturing_hook_deliveries() as received:
            with patch.object(
                pm, "embed_text_with_model", new=AsyncMock(return_value=(None, None))
            ):
                entry = await pm.persist_memory(
                    session,
                    agent_id=str(agent.id),
                    content="Committed.\nStatus: planned\n",
                    memory_type="fact",
                    source="assignment",
                    user_id=str(user.id),
                )
            # Written but not yet durable — nothing may have gone out.
            await _settle()
            assert received == []

            await session.commit()
            await _settle()

        assert len(received) == 1
        assert received[0]["memory_id"] == str(entry.id)
        assert received[0]["status"] == "planned"


@pytest.mark.asyncio
async def test_a_rolled_back_memory_never_reaches_the_hook_bus():
    """The failure this ordering exists to prevent: a caller that persists
    several memories in one transaction and then fails would otherwise have
    told an external system about writes that never landed."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)
        await session.commit()

        async with _capturing_hook_deliveries() as received:
            with patch.object(
                pm, "embed_text_with_model", new=AsyncMock(return_value=(None, None))
            ):
                for index in range(3):
                    await pm.persist_memory(
                        session,
                        agent_id=str(agent.id),
                        content=f"Doomed {index}.\nStatus: planned\n",
                        memory_type="fact",
                        source="assignment",
                        user_id=str(user.id),
                    )
            await session.rollback()
            await _settle()

        assert received == []
        # The flow-event side rolled back with the memory, as it should.
        assert await _events(session) == []


@pytest.mark.asyncio
async def test_a_failed_flow_emission_does_not_lose_the_hook_delivery():
    """The savepoint rollback must not be mistaken for the outer one.

    ``after_rollback`` fires for a failed SAVEPOINT as well, but the memory row
    itself survived it — only the flow emission was undone — so the hook still
    owes a delivery on the eventual commit. This is why both listeners key off
    ``in_nested_transaction`` rather than the event alone.
    """
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)
        await session.commit()

        async with _capturing_hook_deliveries() as received:
            with (
                patch.object(
                    pm, "embed_text_with_model", new=AsyncMock(return_value=(None, None))
                ),
                patch(
                    "app.services.memory_flow_events.emit_memory_persisted",
                    new=AsyncMock(side_effect=RuntimeError("bus down")),
                ),
            ):
                entry = await pm.persist_memory(
                    session,
                    agent_id=str(agent.id),
                    content="Survived.\nStatus: planned\n",
                    memory_type="fact",
                    source="assignment",
                    user_id=str(user.id),
                )
            await session.commit()
            await _settle()

        # No event row (its savepoint rolled back) but the memory is durable,
        # so the hook heard about it exactly once.
        assert await _events(session) == []
        assert await session.get(type(entry), entry.id) is not None
        assert len(received) == 1
        assert received[0]["memory_id"] == str(entry.id)


@pytest.mark.asyncio
async def test_every_memory_of_one_transaction_is_delivered_once():
    """``once=True`` is per registration, not per session: N memories written
    before one commit deliver N events, exactly once each."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)
        await session.commit()

        async with _capturing_hook_deliveries() as received:
            with patch.object(
                pm, "embed_text_with_model", new=AsyncMock(return_value=(None, None))
            ):
                for index in range(3):
                    await pm.persist_memory(
                        session,
                        agent_id=str(agent.id),
                        content=f"Batch {index}.\nStatus: planned\n",
                        memory_type="fact",
                        source="assignment",
                        user_id=str(user.id),
                    )
            await session.commit()
            await _settle()

            # A second, unrelated commit must not re-deliver the first three.
            await session.commit()
            await _settle()

        previews = sorted(d["content_preview"] for d in received)
        assert previews == [
            "Batch 0. Status: planned",
            "Batch 1. Status: planned",
            "Batch 2. Status: planned",
        ]


# ═══════════════════════════════════════════════════════════════════════════
# Age retention — the bound superseding cannot provide
# ═══════════════════════════════════════════════════════════════════════════


async def _age_events(session, *, older_than_days: float) -> None:
    """Backdate every emission so the sweep sees it as aged."""
    await session.execute(
        FlowEvent.__table__.update().values(
            created_at=datetime.now(UTC) - timedelta(days=older_than_days)
        )
    )
    await session.commit()


@asynccontextmanager
async def _purging_against(sessionmaker) -> AsyncIterator[None]:
    """Point ``purge_old_flow_events``'s own session at the scratch database."""
    with patch("app.core.database.async_session", new=sessionmaker):
        yield


@pytest.mark.asyncio
async def test_the_sweep_purges_aged_emissions_and_keeps_recent_ones():
    """Superseding bounds rewrites of one memory; only age bounds the table,
    because every distinct fact emits a row nothing will ever supersede."""
    from app.services.flow_events import purge_old_flow_events

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)
        await session.commit()

        for index in range(3):
            await _write_memory(
                session, agent, user, f"Old {index}.\nStatus: planned\n"
            )
        await _age_events(session, older_than_days=60)

        await _write_memory(session, agent, user, "Fresh.\nStatus: planned\n")

        with (
            patch.object(settings, "flow_event_retention_days", 30),
            patch.object(settings, "flow_run_timeout_seconds", 86400),
        ):
            async with _purging_against(sessionmaker):
                purged = await purge_old_flow_events()

        assert purged == 3
        remaining = await _events(session)
        assert len(remaining) == 1
        assert remaining[0].payload["content_preview"].startswith("Fresh.")


@pytest.mark.asyncio
async def test_the_sweep_never_uses_a_window_shorter_than_the_run_timeout():
    """The floor is a correctness property, not a preference.

    A parked ``Wait for event`` polls for emissions newer than its park, so a
    retention window shorter than the longest possible run could delete the
    event a live run is still waiting for. Configuring one degrades to keeping
    more than asked.
    """
    from app.services.flow_events import purge_old_flow_events

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)
        await session.commit()

        await _write_memory(session, agent, user, "Waited on.\nStatus: planned\n")
        await _age_events(session, older_than_days=3)

        # Retention says one day; runs may last seven. The floor wins.
        with (
            patch.object(settings, "flow_event_retention_days", 1),
            patch.object(settings, "flow_run_timeout_seconds", 7 * 86400),
        ):
            async with _purging_against(sessionmaker):
                purged = await purge_old_flow_events()

        assert purged == 0
        assert len(await _events(session)) == 1


@pytest.mark.asyncio
async def test_the_sweep_is_disabled_by_a_zero_retention():
    from app.services.flow_events import purge_old_flow_events

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)
        await session.commit()

        await _write_memory(session, agent, user, "Kept.\nStatus: planned\n")
        await _age_events(session, older_than_days=3650)

        with patch.object(settings, "flow_event_retention_days", 0):
            async with _purging_against(sessionmaker):
                assert await purge_old_flow_events() == 0
        assert len(await _events(session)) == 1


@pytest.mark.asyncio
async def test_the_sweep_is_capped_per_pass():
    """Bounded like the other retention sweeps, so one hourly tick cannot hold
    a long transaction over a table every emit writes to."""
    from app.services.flow_events import purge_old_flow_events

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user, agent, (listener,) = await _seed(
            session, graphs=[_listener_graph(uuid.uuid4())]
        )
        type_id = await _event_type_id(session)
        listener.graph = _listener_graph(type_id)
        await session.commit()

        for index in range(5):
            await _write_memory(
                session, agent, user, f"Old {index}.\nStatus: planned\n"
            )
        await _age_events(session, older_than_days=60)

        with (
            patch.object(settings, "flow_event_retention_days", 30),
            patch.object(settings, "flow_run_timeout_seconds", 86400),
        ):
            async with _purging_against(sessionmaker):
                assert await purge_old_flow_events(limit=2) == 2
                assert await purge_old_flow_events(limit=2) == 2
                assert await purge_old_flow_events(limit=2) == 1
                assert await purge_old_flow_events(limit=2) == 0
        assert await _events(session) == []
