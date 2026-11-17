"""Regression coverage for the always-set-owner side effects.

Preserving ``user_id`` on company-managed entities (instead of nulling it)
means two invariants that used to be implicit must now be enforced explicitly:

1. **Personal-integration resolution** — queries that filter
   ``Integration.user_id == <uid>`` to mean "the user's *private* integration"
   must also exclude company-managed rows, or an admin-owned shared
   integration pre-empts the user's private connection (reversing the
   documented "own first, shared fallback" order).
2. **Owner-email exposure** — ``AgentResponse.owner_email`` is returned to any
   viewer of a company-managed / group-shared agent, so the owner's email
   (admin-only PII elsewhere) must only be revealed to admins or the owner.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# _owner_email_for_viewer (privacy gate on AgentResponse.owner_email)
# ---------------------------------------------------------------------------


def _viewer(*, is_admin: bool, uid: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(is_admin=is_admin, id=uid)


def test_owner_email_visible_to_admin():
    from app.api.v1.agents import _owner_email_for_viewer

    owner = uuid.uuid4()
    admin = _viewer(is_admin=True, uid=uuid.uuid4())
    assert (
        _owner_email_for_viewer(admin, owner, "owner@x.io") == "owner@x.io"
    )


def test_owner_email_visible_to_owner_themself():
    from app.api.v1.agents import _owner_email_for_viewer

    owner = uuid.uuid4()
    viewer = _viewer(is_admin=False, uid=owner)
    assert _owner_email_for_viewer(viewer, owner, "owner@x.io") == "owner@x.io"


def test_owner_email_hidden_from_other_non_admin():
    from app.api.v1.agents import _owner_email_for_viewer

    owner = uuid.uuid4()
    other = _viewer(is_admin=False, uid=uuid.uuid4())
    assert _owner_email_for_viewer(other, owner, "owner@x.io") is None


def test_owner_email_none_passthrough():
    from app.api.v1.agents import _owner_email_for_viewer

    admin = _viewer(is_admin=True, uid=uuid.uuid4())
    assert _owner_email_for_viewer(admin, uuid.uuid4(), None) is None


# ---------------------------------------------------------------------------
# kb_sync._get_integration_token — "own" query excludes company-managed
# ---------------------------------------------------------------------------


class _NoneResult:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


def _integration_queries(db) -> list[str]:
    """The recorded queries on ``integrations`` (the grants / role lookups aside)."""
    return [sql.lower() for sql in db.sql if "from integrations" in sql.lower()]


class _RecordingDB:
    """Fake async session that records the compiled SQL of every query and
    returns no rows (forcing the resolver through all fallbacks to the raise)."""

    def __init__(self):
        self.sql: list[str] = []

    async def execute(self, statement):
        self.sql.append(
            str(statement.compile(compile_kwargs={"literal_binds": True}))
        )
        return _NoneResult()


@pytest.mark.asyncio
async def test_get_integration_token_own_query_excludes_company_managed():
    """The first "user's own" query must carry an ``is_company_managed``
    exclusion so an admin-owned shared integration can't be returned ahead of
    the user's private one. Before the fix the own query filtered on
    ``user_id`` alone."""
    from app.services import kb_sync

    db = _RecordingDB()
    with pytest.raises(ValueError):
        await kb_sync._get_integration_token(db, uuid.uuid4(), "notion")

    queries = _integration_queries(db)
    assert len(queries) == 2, "expected own-query then company-managed fallback"
    own_sql = queries[0]
    # Own query: scoped to the user AND excluding shared rows.
    assert "user_id" in own_sql
    assert "is_company_managed" in own_sql
    # Fallback query: the shared company-managed lookup, narrowed by
    # access_restricted for users outside the grants.
    assert "is_company_managed" in queries[1]
    assert "access_restricted" in queries[1]


@pytest.mark.asyncio
async def test_get_drata_integration_own_query_excludes_company_managed():
    from app.services import kb_sync

    db = _RecordingDB()
    result = await kb_sync._get_drata_integration(db, uuid.uuid4())

    assert result is None
    queries = _integration_queries(db)
    assert len(queries) == 2
    own_sql = queries[0]
    assert "user_id" in own_sql
    assert "is_company_managed" in own_sql
    assert "access_restricted" in queries[1]


# ---------------------------------------------------------------------------
# _resolve_run_file_owner_id — generated-file ownership on assignment runs
# ---------------------------------------------------------------------------
#
# Files an assignment run generates are persisted with this owner as
# ``FileAttachment.user_id``. ``_authorize_file_access`` grants the file owner
# BEFORE consulting the linked run's company-managed admin gate, so a
# company-managed run must stamp NO personal owner — otherwise the always-set
# run-as / triggering user (a non-admin) could download admin-gated artifacts.


def _run(*, is_company_managed: bool, user_id):
    return SimpleNamespace(is_company_managed=is_company_managed, user_id=user_id)


def test_run_file_owner_none_for_company_managed_run():
    """Even though a company-managed run now carries a real execution identity,
    its generated files must have NO personal owner (NULL)."""
    from app.services.assignment_engine import _resolve_run_file_owner_id

    run = _run(is_company_managed=True, user_id=uuid.uuid4())
    assert _resolve_run_file_owner_id(run) is None


def test_run_file_owner_is_execution_identity_for_personal_run():
    from app.services.assignment_engine import _resolve_run_file_owner_id

    uid = uuid.uuid4()
    run = _run(is_company_managed=False, user_id=uid)
    assert _resolve_run_file_owner_id(run) == str(uid)


def test_run_file_owner_none_for_personal_run_without_owner():
    from app.services.assignment_engine import _resolve_run_file_owner_id

    run = _run(is_company_managed=False, user_id=None)
    assert _resolve_run_file_owner_id(run) is None
