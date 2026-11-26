"""Tests for user-scope isolation in the persistent agent memory system.

Covers the cross-user memory leakage findings (MEM-1, MEM-2, MEM-9, MEM-10):

* **MEM-1** — the memory *list* endpoint (and the update/delete entry
  endpoints) must mirror the ``/memory/search`` semantics: a non-owner only
  sees agent-level entries (``user_id IS NULL``) plus their own user-scoped
  entries.  Previously the list endpoint filtered by ``agent_id`` only, so
  on a group-shared agent User A could read User B's scoped facts.
* **MEM-2** — fact consolidation must batch and merge facts strictly within
  their ``user_id`` scope and persist the merged entry with that same scope.
* **MEM-9** — search results must label user-scoped rows ``"scope": "user"``
  (previously every hit was labelled ``"agent"``).
* **MEM-10** — the fact cap must be enforced per (agent, scope) so one
  user's burst of facts cannot evict another user's facts.

These tests use the repo's fake-DB style (scripted ``execute`` responses +
statement capture) and stub the LLM/embedding calls — no live DB required.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api.v1 import agent_memory as agent_memory_api
from app.services import persistent_memory as pm

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

SCOPE_CLAUSE = "agent_memory_entries.user_id IS NULL"


def _fake_user(*, user_id: uuid.UUID | None = None, role: str = "user"):
    uid = user_id or uuid.uuid4()
    return SimpleNamespace(id=uid, role=role, is_admin=role == "admin")


def _fake_agent(
    *,
    owner_id: uuid.UUID | None = None,
    is_company_managed: bool = False,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        name="Test Agent",
        is_company_managed=is_company_managed,
    )


class _FakeResult:
    """Generic result shim supporting the access patterns under test."""

    def __init__(self, *, scalar=None, rows=None, scalars=None, one=None):
        self._scalar = scalar
        self._rows = rows or []
        self._scalars = scalars or []
        self._one = one

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._one

    def all(self):
        return list(self._rows)

    def fetchall(self):
        return list(self._rows)

    def scalars(self):
        items = list(self._scalars)
        return SimpleNamespace(all=lambda: items)


class _AsyncNullContext:
    """No-op stand-in for ``AsyncSession.begin_nested()`` (savepoint)."""

    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ScriptedDB:
    """Fake AsyncSession returning scripted results and capturing statements."""

    def __init__(self, responses: list[_FakeResult] | None = None):
        self.responses = list(responses or [])
        self.statements = []
        self.begin_nested_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if self.responses:
            return self.responses.pop(0)
        return _FakeResult()

    async def flush(self):
        return None

    def in_transaction(self):
        # The commit-release points only commit when a transaction
        # is open; the fake session never opens one.
        return False

    async def commit(self):
        return None

    def begin_nested(self):
        self.begin_nested_count += 1
        return _AsyncNullContext()


class _FakeLLM:
    """LLM stub that records every prompt it receives."""

    def __init__(self, reply: str = "- merged knowledge bullet"):
        self.reply = reply
        self.calls: list[list] = []

    async def chat(self, *, messages, **_kwargs):
        self.calls.append(messages)
        return SimpleNamespace(content=self.reply)


# ---------------------------------------------------------------------------
# MEM-1 — _memory_scope_filter semantics
# ---------------------------------------------------------------------------


class TestMemoryScopeFilter:
    def test_owner_gets_full_visibility(self):
        user = _fake_user()
        agent = _fake_agent(owner_id=user.id)
        assert agent_memory_api._memory_scope_filter(agent, user) is None

    def test_admin_on_company_managed_gets_full_visibility(self):
        user = _fake_user(role="admin")
        agent = _fake_agent(owner_id=None, is_company_managed=True)
        assert agent_memory_api._memory_scope_filter(agent, user) is None

    def test_group_member_is_scoped(self):
        user = _fake_user()
        agent = _fake_agent(owner_id=uuid.uuid4())  # someone else's agent
        clause = agent_memory_api._memory_scope_filter(agent, user)
        assert clause is not None
        assert SCOPE_CLAUSE in str(clause)
        # Stable substring only — the bind-param suffix (":user_id_1") is an
        # SQLAlchemy implementation detail.
        assert "agent_memory_entries.user_id =" in str(clause)

    def test_admin_on_group_shared_private_agent_is_scoped(self):
        """Conservative choice: admin role does not grant access to other
        users' personal memory on a private (group-shared) agent the admin
        neither owns nor curates via company-managed status."""
        user = _fake_user(role="admin")
        agent = _fake_agent(owner_id=uuid.uuid4(), is_company_managed=False)
        clause = agent_memory_api._memory_scope_filter(agent, user)
        assert clause is not None
        assert SCOPE_CLAUSE in str(clause)

    def test_non_admin_on_company_managed_is_scoped(self):
        user = _fake_user()
        agent = _fake_agent(owner_id=None, is_company_managed=True)
        clause = agent_memory_api._memory_scope_filter(agent, user)
        assert clause is not None
        assert SCOPE_CLAUSE in str(clause)


# ---------------------------------------------------------------------------
# MEM-1 — list endpoint applies the scope filter to count + entries queries
# ---------------------------------------------------------------------------


class TestListEndpointScoping:
    async def _run_list(self, *, user, agent) -> _ScriptedDB:
        db = _ScriptedDB(
            [
                _FakeResult(scalar=0),  # count query
                _FakeResult(scalars=[]),  # entries query
            ]
        )
        with patch.object(
            agent_memory_api, "get_user_agent", new=AsyncMock(return_value=agent)
        ):
            await agent_memory_api.list_agent_memories(
                str(agent.id), user=user, db=db, memory_type=None, limit=50
            )
        return db

    async def test_group_member_queries_are_scoped(self):
        user = _fake_user()
        agent = _fake_agent(owner_id=uuid.uuid4())
        db = await self._run_list(user=user, agent=agent)

        assert len(db.statements) == 2
        for stmt in db.statements:  # count + entries
            assert SCOPE_CLAUSE in str(stmt)

    async def test_owner_queries_are_not_scoped(self):
        user = _fake_user()
        agent = _fake_agent(owner_id=user.id)
        db = await self._run_list(user=user, agent=agent)

        assert len(db.statements) == 2
        for stmt in db.statements:
            assert SCOPE_CLAUSE not in str(stmt)

    async def test_admin_on_company_managed_queries_are_not_scoped(self):
        user = _fake_user(role="admin")
        agent = _fake_agent(owner_id=None, is_company_managed=True)
        db = await self._run_list(user=user, agent=agent)

        for stmt in db.statements:
            assert SCOPE_CLAUSE not in str(stmt)


# ---------------------------------------------------------------------------
# MEM-1 — update/delete entry endpoints cannot touch out-of-scope entries
# ---------------------------------------------------------------------------


class TestUpdateDeleteScoping:
    async def test_update_other_users_scoped_entry_404s(self):
        user = _fake_user()
        agent = _fake_agent(owner_id=uuid.uuid4())
        # Entry lookup returns None — simulates the scoped query excluding
        # another user's entry.
        db = _ScriptedDB([_FakeResult(one=None)])

        with patch.object(
            agent_memory_api, "_managed_agent", new=AsyncMock(return_value=agent)
        ), pytest.raises(HTTPException) as exc_info:
            await agent_memory_api.update_memory_entry(
                str(agent.id),
                str(uuid.uuid4()),
                agent_memory_api.UpdateMemoryRequest(content="new"),
                request=None,
                user=user,
                db=db,
            )

        assert exc_info.value.status_code == 404
        # The lookup statement itself must carry the scope filter.
        assert SCOPE_CLAUSE in str(db.statements[0])

    async def test_delete_other_users_scoped_entry_404s(self):
        user = _fake_user()
        agent = _fake_agent(owner_id=uuid.uuid4())
        db = _ScriptedDB([_FakeResult(one=None)])

        with patch.object(
            agent_memory_api, "_managed_agent", new=AsyncMock(return_value=agent)
        ), pytest.raises(HTTPException) as exc_info:
            await agent_memory_api.delete_memory_entry(
                str(agent.id),
                str(uuid.uuid4()),
                request=None,
                user=user,
                db=db,
            )

        assert exc_info.value.status_code == 404
        assert SCOPE_CLAUSE in str(db.statements[0])

    async def test_owner_lookup_is_not_scoped(self):
        user = _fake_user()
        agent = _fake_agent(owner_id=user.id)
        db = _ScriptedDB([_FakeResult(one=None)])

        with patch.object(
            agent_memory_api, "_managed_agent", new=AsyncMock(return_value=agent)
        ), pytest.raises(HTTPException):
            await agent_memory_api.delete_memory_entry(
                str(agent.id),
                str(uuid.uuid4()),
                request=None,
                user=user,
                db=db,
            )

        assert SCOPE_CLAUSE not in str(db.statements[0])


# ---------------------------------------------------------------------------
# MEM-9 — search results carry the correct scope label
# ---------------------------------------------------------------------------


class TestSearchScopeLabel:
    async def test_scope_label_reflects_user_id(self):
        agent_id = str(uuid.uuid4())
        searching_user = uuid.uuid4()
        now = datetime.now(UTC)

        unsuperseded = {"subject_key": None, "superseded_by_id": None, "superseded": False}
        rows = [
            SimpleNamespace(
                id=uuid.uuid4(),
                content="agent-level fact",
                memory_type="fact",
                source="chat",
                user_id=None,
                created_at=now,
                score=0.9,
                **unsuperseded,
            ),
            SimpleNamespace(
                id=uuid.uuid4(),
                content="my own scoped fact",
                memory_type="fact",
                source="chat",
                user_id=searching_user,
                created_at=now,
                score=0.8,
                **unsuperseded,
            ),
        ]
        db = _ScriptedDB([_FakeResult(rows=rows)])

        with patch.object(
            pm, "embed_text", new=AsyncMock(return_value=[0.1, 0.2, 0.3])
        ):
            results = await pm.search_agent_memories(
                db,
                agent_id=agent_id,
                query="anything",
                user_id=str(searching_user),
                limit=5,
            )

        by_content = {r["content"]: r["scope"] for r in results}
        assert by_content["agent-level fact"] == "agent"
        assert by_content["my own scoped fact"] == "user"


# ---------------------------------------------------------------------------
# MEM-2 — consolidation never mixes scopes
# ---------------------------------------------------------------------------


def _fact(content: str, *, scope: uuid.UUID | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        content=content,
        user_id=scope,
        memory_type="fact",
    )


class TestConsolidationScoping:
    async def test_consolidation_batches_per_scope(self):
        agent_id = str(uuid.uuid4())
        user_b = uuid.uuid4()

        agent_facts = [_fact(f"agent fact {i}", scope=None) for i in range(3)]
        user_facts = [_fact(f"user-b fact {i}", scope=user_b) for i in range(3)]

        db = _ScriptedDB(
            [
                _FakeResult(scalar=50),  # total fact count (>= min_facts)
                _FakeResult(rows=[(None,), (user_b,)]),  # distinct scopes
                _FakeResult(scalars=agent_facts),  # oldest batch, scope NULL
                _FakeResult(),  # delete (scope NULL)
                _FakeResult(scalars=user_facts),  # oldest batch, scope user_b
                _FakeResult(),  # delete (scope user_b)
            ]
        )

        llm = _FakeLLM()
        # ``None`` would now mean "dedup skip" and roll the savepoint back;
        # return a sentinel entry so the merge counts as persisted.
        persist_mock = AsyncMock(return_value=SimpleNamespace())

        with (
            patch(
                "app.llm.router.get_llm_for_user",
                new=AsyncMock(return_value=(llm, "gpt-4o")),
            ),
            patch.object(pm, "persist_memory", new=persist_mock),
        ):
            merged = await pm.consolidate_agent_facts(db, agent_id=agent_id)

        assert merged == 6
        # delete + persist for each scope ran inside a savepoint
        assert db.begin_nested_count == 2

        # The scope-selection query must pre-filter eligible scopes (HAVING)
        # and bound the per-run fan-out (LIMIT) so a heavily shared agent
        # can't turn one maintenance cycle into O(#scopes) LLM calls.
        scopes_sql = str(db.statements[1]).upper()
        assert "HAVING" in scopes_sql
        assert "LIMIT" in scopes_sql

        # One merged entry persisted per scope, each with its own scope.
        assert persist_mock.await_count == 2
        persisted_scopes = [
            call.kwargs["user_id"] for call in persist_mock.await_args_list
        ]
        assert persisted_scopes == [None, str(user_b)]

        # Each LLM merge prompt must contain ONLY facts from one scope.
        assert len(llm.calls) == 2
        first_prompt = next(m.content for m in llm.calls[0] if m.role == "user")
        second_prompt = next(m.content for m in llm.calls[1] if m.role == "user")
        assert "agent fact" in first_prompt
        assert "user-b fact" not in first_prompt
        assert "user-b fact" in second_prompt
        assert "agent fact" not in second_prompt

    async def test_consolidation_skips_scopes_with_too_few_facts(self):
        """In production the HAVING filter already excludes small scopes;
        this exercises the in-loop safety net for a scope that shrank
        between the two queries (concurrent prune/eviction)."""
        agent_id = str(uuid.uuid4())
        user_b = uuid.uuid4()

        agent_facts = [_fact(f"agent fact {i}", scope=None) for i in range(3)]
        small_scope = [_fact("lonely fact", scope=user_b)]  # < 3 — skipped

        db = _ScriptedDB(
            [
                _FakeResult(scalar=50),
                _FakeResult(rows=[(None,), (user_b,)]),
                _FakeResult(scalars=agent_facts),
                _FakeResult(),  # delete (scope NULL)
                _FakeResult(scalars=small_scope),
            ]
        )

        llm = _FakeLLM()
        persist_mock = AsyncMock(return_value=SimpleNamespace())

        with (
            patch(
                "app.llm.router.get_llm_for_user",
                new=AsyncMock(return_value=(llm, "gpt-4o")),
            ),
            patch.object(pm, "persist_memory", new=persist_mock),
        ):
            merged = await pm.consolidate_agent_facts(db, agent_id=agent_id)

        assert merged == 3
        assert persist_mock.await_count == 1
        assert persist_mock.await_args_list[0].kwargs["user_id"] is None

    async def test_consolidation_below_threshold_is_noop(self):
        db = _ScriptedDB([_FakeResult(scalar=10)])  # < min_facts (50)
        merged = await pm.consolidate_agent_facts(db, agent_id=str(uuid.uuid4()))
        assert merged == 0
        assert len(db.statements) == 1

    async def test_consolidation_merge_persists_with_skip_dedup(self):
        """The merged entry must be persisted with ``skip_dedup=True``.

        Consolidation deletes the oldest batch then persists one curated
        merge.  If that merge were subject to the fact dedup check it could
        match an out-of-batch fact (cosine >= threshold), return ``None``,
        roll the savepoint back, and strand the SAME oldest batch — which
        is then re-merged on every maintenance run forever (the production
        ``merged entry was not persisted`` treadmill)."""
        agent_id = str(uuid.uuid4())
        agent_facts = [_fact(f"agent fact {i}", scope=None) for i in range(3)]

        db = _ScriptedDB(
            [
                _FakeResult(scalar=50),  # total fact count (>= min_facts)
                _FakeResult(rows=[(None,)]),  # single eligible scope
                _FakeResult(scalars=agent_facts),  # oldest batch
                _FakeResult(),  # delete (inside savepoint)
            ]
        )
        llm = _FakeLLM()
        persist_mock = AsyncMock(return_value=SimpleNamespace())

        with (
            patch(
                "app.llm.router.get_llm_for_user",
                new=AsyncMock(return_value=(llm, "gpt-4o")),
            ),
            patch.object(pm, "persist_memory", new=persist_mock),
        ):
            merged = await pm.consolidate_agent_facts(db, agent_id=agent_id)

        assert merged == 3
        persist_mock.assert_awaited_once()
        assert persist_mock.await_args.kwargs["skip_dedup"] is True

    async def test_persist_failure_retains_originals(self):
        """When ``persist_memory`` raises or returns ``None`` (the defensive
        persistence-failure guard — consolidation now passes
        ``skip_dedup=True`` so a real dedup skip can no longer happen here),
        the savepoint must roll back the delete so the scope's original
        facts survive the outer maintenance commit — nothing is counted
        as merged."""
        agent_id = str(uuid.uuid4())
        agent_facts = [_fact(f"agent fact {i}", scope=None) for i in range(3)]

        class _TrackingSavepoint:
            """Records whether the savepoint exited with an exception
            (i.e. would have rolled back in a real session)."""

            rolled_back = False

            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, tb):
                if exc_type is not None:
                    _TrackingSavepoint.rolled_back = True
                return False

        db = _ScriptedDB(
            [
                _FakeResult(scalar=50),  # total fact count
                _FakeResult(rows=[(None,)]),  # single eligible scope
                _FakeResult(scalars=agent_facts),  # oldest batch
                _FakeResult(),  # delete (inside savepoint)
            ]
        )
        db.begin_nested = lambda: _TrackingSavepoint()  # type: ignore[method-assign]

        llm = _FakeLLM()
        persist_mock = AsyncMock(return_value=None)  # dedup skip

        with (
            patch(
                "app.llm.router.get_llm_for_user",
                new=AsyncMock(return_value=(llm, "gpt-4o")),
            ),
            patch.object(pm, "persist_memory", new=persist_mock),
        ):
            merged = await pm.consolidate_agent_facts(db, agent_id=agent_id)

        assert merged == 0
        assert persist_mock.await_count == 1
        assert _TrackingSavepoint.rolled_back is True


class _StopReasonLLM:
    """Scripted ``(content, stop_reason)`` answers; records each call's kwargs."""

    def __init__(self, *answers: tuple[str, str]):
        self._answers = list(answers)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        content, stop_reason = self._answers.pop(0)
        return SimpleNamespace(content=content, stop_reason=stop_reason)


class TestConsolidationCutAtMaxTokens:
    """The originals are deleted once the merge lands, so a merge cut at
    ``max_tokens`` must never land: it would replace whole facts with a list
    missing its tail."""

    async def _consolidate(self, llm):
        agent_facts = [_fact(f"agent fact {i}", scope=None) for i in range(3)]
        db = _ScriptedDB(
            [
                _FakeResult(scalar=50),  # total fact count (>= min_facts)
                _FakeResult(rows=[(None,)]),  # single eligible scope
                _FakeResult(scalars=agent_facts),  # oldest batch
            ]
        )
        persist_mock = AsyncMock(return_value=SimpleNamespace())
        with (
            patch(
                "app.llm.router.get_llm_for_user",
                new=AsyncMock(return_value=(llm, "gpt-4o")),
            ),
            patch.object(pm, "persist_memory", new=persist_mock),
        ):
            merged = await pm.consolidate_agent_facts(db, agent_id=str(uuid.uuid4()))
        return merged, db, persist_mock

    async def test_a_cut_merge_is_retried_and_the_complete_one_lands(self):
        complete = "- agent fact 0\n- agent fact 1\n- agent fact 2"
        llm = _StopReasonLLM(
            ("- agent fact 0\n- agent fa", "max_tokens"), (complete, "end_turn")
        )

        merged, db, persist_mock = await self._consolidate(llm)

        assert merged == 3
        assert [call["max_tokens"] for call in llm.calls] == [
            pm._CONSOLIDATION_MAX_TOKENS,
            2 * pm._CONSOLIDATION_MAX_TOKENS,
        ]
        # A mechanical merge: no thinking to share the budget with.
        assert all(call["disable_thinking"] is True for call in llm.calls)
        assert persist_mock.await_args.kwargs["content"] == complete
        assert db.begin_nested_count == 1

    async def test_a_merge_cut_twice_deletes_nothing(self):
        llm = _StopReasonLLM(
            ("- agent fact 0\n- agent fa", "max_tokens"),
            ("- agent fact 0\n- agent fact 1\n- agent f", "max_tokens"),
        )

        merged, db, persist_mock = await self._consolidate(llm)

        assert merged == 0
        assert len(llm.calls) == 2
        persist_mock.assert_not_awaited()
        assert db.begin_nested_count == 0
        assert len(db.statements) == 3
        assert not any("DELETE" in str(s).upper() for s in db.statements)


# ---------------------------------------------------------------------------
# MEM-10 — cap enforcement is scope-aware
# ---------------------------------------------------------------------------


class TestCapEnforcementScoping:
    async def test_only_over_cap_scope_is_evicted(self):
        agent_id = str(uuid.uuid4())
        user_a = uuid.uuid4()
        evict_ids = [uuid.uuid4(), uuid.uuid4()]

        db = _ScriptedDB(
            [
                # Per-scope counts: agent-level over cap, user A under cap
                _FakeResult(rows=[(None, 5), (user_a, 2)]),
                # Oldest ids within the over-cap scope only
                _FakeResult(rows=[(evict_ids[0],), (evict_ids[1],)]),
                _FakeResult(),  # delete
            ]
        )

        evicted = await pm.enforce_memory_cap(db, agent_id=agent_id, max_facts=3)

        assert evicted == 2
        # counts (group by) + eviction select + delete = 3 statements
        assert len(db.statements) == 3
        # The grouped count query must let the DB pre-filter over-cap
        # scopes (HAVING) instead of returning every scope.
        assert "HAVING" in str(db.statements[0]).upper()
        # The eviction select must be restricted to the over-cap scope
        # (agent-level => user_id IS NULL).
        assert SCOPE_CLAUSE in str(db.statements[1])

    async def test_user_burst_does_not_evict_other_scopes(self):
        """Total (6) exceeds the cap (4) but no individual scope does —
        the old agent-global cap would have evicted 2 entries here.

        In production the HAVING clause already keeps these under-cap
        scopes out of the result set; the scripted rows exercise the
        in-loop ``excess <= 0`` safety net behind it."""
        agent_id = str(uuid.uuid4())
        user_a, user_b = uuid.uuid4(), uuid.uuid4()

        db = _ScriptedDB(
            [
                _FakeResult(rows=[(None, 2), (user_a, 2), (user_b, 2)]),
            ]
        )

        evicted = await pm.enforce_memory_cap(db, agent_id=agent_id, max_facts=4)

        assert evicted == 0
        assert len(db.statements) == 1  # only the group-by count query

    async def test_overflowing_user_scope_evicts_within_itself(self):
        agent_id = str(uuid.uuid4())
        user_a = uuid.uuid4()
        evict_id = uuid.uuid4()

        db = _ScriptedDB(
            [
                _FakeResult(rows=[(None, 1), (user_a, 4)]),
                _FakeResult(rows=[(evict_id,)]),
                _FakeResult(),  # delete
            ]
        )

        evicted = await pm.enforce_memory_cap(db, agent_id=agent_id, max_facts=3)

        assert evicted == 1
        # The eviction select targets the user scope, not the NULL scope.
        # (Stable substring only — the bind-param suffix is an SQLAlchemy
        # implementation detail. The WHERE clause alone: the ORDER BY evicts
        # superseded entries first, with an IS NULL of its own.)
        eviction_where = str(db.statements[1].whereclause)
        assert "agent_memory_entries.user_id =" in eviction_where
        assert SCOPE_CLAUSE not in eviction_where
