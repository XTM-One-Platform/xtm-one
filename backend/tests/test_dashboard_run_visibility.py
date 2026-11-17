"""Regression coverage: the dashboard (`GET /api/v1/activity/stats`) scopes
run-derived data to run ownership, not agent visibility.

`dashboard_stats` (scope="user") used to filter run-derived data — run
counts, the notification count, recent digests, failed runs, per-agent
last-run / weekly-runs, and the top-agents / top-assignments-by-run charts —
by *agent* visibility (`_visible_agent_ids`, which includes group-shared
agents). Run visibility must be NARROWER: a group-shared agent is read-only
visible to every group member, but its runs stay private to the run owner.
The fix routes every run query through ``run_visible_filter`` (own +
company-managed), mirroring the runs list / detail endpoints.

These tests drive the endpoint against a capturing fake session and assert
the SQL-level invariant the reviewer asked for: every statement that touches
the ``assignment_runs`` table is scoped by ``run_visible_filter`` (own +
company-managed) and NEVER references the group-share overlap
(``shared_group_ids``). The single deliberate exception is the platform-wide
"top active users" aggregate, which counts activity per user across everyone
(names + counts only, never openable run content) and is identified by its
``user_id IS NOT NULL`` predicate.

Driving the real endpoint (rather than asserting on a hand-built query) means
a future edit that adds an unscoped run surface, or that re-widens one back to
agent visibility, fails here. Exercising ``scope="platform"`` additionally
proves every ``_run_vis`` use is guarded by ``if not _platform`` — an
unguarded reference would raise ``NameError`` in that path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import activity as activity_mod

_FIXED_DT = datetime(2026, 1, 1, tzinfo=UTC)


class _Row(tuple):
    """Universal fake result row.

    Behaves as a ``(key, count)`` 2-tuple so ``dict(result.all())`` and
    ``for a, b in result`` unpacking work, while also exposing arbitrary
    attributes (``.total``, ``.agent_id``, ``.status`` …) for the row /
    ORM-object access paths. ``*_at`` attributes return a real datetime
    (the code calls ``.isoformat()``); ``actions_taken`` is falsy; every
    other attribute is ``None``.
    """

    def __new__(cls) -> _Row:
        return super().__new__(cls, (None, 0))

    def __getattr__(self, name: str):
        if name.endswith("_at"):
            return _FIXED_DT
        if name == "actions_taken":
            return None
        return None


class _Result:
    def one(self):
        return _Row()

    def all(self):
        return [_Row()]

    def scalar(self):
        return 0

    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self


class _CapturingSession:
    """Async-session stub that records every statement passed to it."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(str(statement))
        return _Result()

    async def scalar(self, statement, *_args, **_kwargs):
        self.statements.append(str(statement))
        return 0


def _patch_helpers(monkeypatch, *, groups: list[str]) -> None:
    """Patch the two helpers that issue their own queries / shape results so
    the fake session only has to serve ``dashboard_stats``'s own queries."""
    monkeypatch.setattr(
        activity_mod, "get_user_group_ids", AsyncMock(return_value=groups)
    )
    monkeypatch.setattr(
        activity_mod, "resolve_owner_names", AsyncMock(return_value={})
    )


async def _run_dashboard(monkeypatch, *, scope: str, is_admin: bool, groups):
    _patch_helpers(monkeypatch, groups=groups)
    session = _CapturingSession()
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin)
    await activity_mod.dashboard_stats(
        user=user, db=session, activity_range="all", scope=scope
    )
    return session.statements


@pytest.mark.asyncio
async def test_user_scope_run_queries_are_run_visible_and_never_group_shared(
    monkeypatch,
):
    """Every ``assignment_runs`` query in the user-scoped dashboard is scoped
    to own + company-managed and never references group sharing."""
    sqls = await _run_dashboard(
        monkeypatch, scope="user", is_admin=False, groups=["group-1", "group-2"]
    )

    run_sqls = [s for s in sqls if "assignment_runs" in s]
    assert run_sqls, "dashboard must query assignment_runs"

    for s in run_sqls:
        # Anti-leak: a run query must NEVER widen to the group-share overlap.
        assert "shared_group_ids" not in s, f"run query leaked group sharing:\n{s}"

        # The platform-wide "top active users" aggregate is the one deliberate
        # exception — it counts activity per user across everyone and never
        # surfaces openable run content.
        if "assignment_runs.user_id IS NOT NULL" in s:
            continue

        # Everything else is run-content the user can open → must be scoped to
        # own + company-managed (the run_visible_filter shape).
        assert "assignment_runs.user_id =" in s, f"run query not owner-scoped:\n{s}"
        assert "assignment_runs.is_company_managed IS true" in s, (
            f"run query missing company-managed tier:\n{s}"
        )


@pytest.mark.asyncio
async def test_user_scope_per_agent_window_query_is_scoped(monkeypatch):
    """The per-agent "last run" window query and the per-agent weekly-runs
    query (both newly scoped by this fix) must carry the run-visibility
    predicate — these are the surfaces that previously used agent visibility."""
    sqls = await _run_dashboard(
        monkeypatch, scope="user", is_admin=False, groups=["group-1"]
    )

    window_sqls = [s for s in sqls if "row_number" in s.lower()]
    assert window_sqls, "expected the per-agent last-run window query"
    for s in window_sqls:
        assert "assignment_runs.user_id =" in s
        assert "assignment_runs.is_company_managed IS true" in s
        assert "shared_group_ids" not in s


@pytest.mark.asyncio
async def test_user_scope_agent_visibility_still_uses_group_sharing(monkeypatch):
    """Sanity guard for the anti-leak assertion above: agent / entity
    visibility queries DO include the group-share overlap, so the
    ``shared_group_ids not in run_sql`` checks are actually meaningful and
    not vacuously true because the marker never appears at all."""
    sqls = await _run_dashboard(
        monkeypatch, scope="user", is_admin=False, groups=["group-1"]
    )

    non_run_sqls = [s for s in sqls if "assignment_runs" not in s]
    assert any("shared_group_ids" in s for s in non_run_sqls), (
        "agent-visibility queries should reference shared_group_ids; if they "
        "don't, the run-query anti-leak assertions are vacuous"
    )


@pytest.mark.asyncio
async def test_platform_scope_does_not_self_scope_run_queries(monkeypatch):
    """The admin platform dashboard (scope="platform") shows ALL runs, so no
    run query is scoped to a single user. Exercising this path also proves
    every ``_run_vis`` reference is guarded by ``if not _platform`` — an
    unguarded use would raise ``NameError`` before we reach the assertions."""
    sqls = await _run_dashboard(
        monkeypatch, scope="platform", is_admin=True, groups=[]
    )

    run_sqls = [s for s in sqls if "assignment_runs" in s]
    assert run_sqls, "platform dashboard must still query assignment_runs"

    scoped = [
        s
        for s in run_sqls
        if "assignment_runs.user_id =" in s
        and "assignment_runs.is_company_managed IS true" in s
    ]
    assert scoped == [], (
        "platform scope must not apply the per-user run_visible_filter:\n"
        + "\n".join(scoped)
    )
