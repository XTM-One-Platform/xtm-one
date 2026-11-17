"""Tests for intent binding sharing scope enforcement.

When agents are exposed via intents to external platforms (OpenCTI, OpenAEV),
the agent's sharing scope must be respected.  Specifically:

* The intent catalog (``GET /intents/catalog``) must never expose private
  agents — only company-managed or group-shared agents appear.
* Global intent bindings (admin, ``user_id=NULL``) cannot target private agents.
* Non-admin users can only bind agents they have visibility on.
* The platform-chat agent list (``GET /platform/chat/agents``) must not
  bypass three-tier visibility for admin / service-account callers.
* Malformed agent UUIDs sent to ``PUT /intents/{id}/bind`` must return HTTP
  422 (parse-error), not 500.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_agent(
    *,
    is_company_managed: bool = False,
    user_id: uuid.UUID | None = None,
    shared_group_ids: list | None = None,
    enabled: bool = True,
    name: str = "Test Agent",
    slug: str = "test-agent",
    description: str = "A test agent",
):
    """Create a mock Agent with the relevant fields."""
    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.is_company_managed = is_company_managed
    agent.user_id = user_id
    agent.shared_group_ids = shared_group_ids
    agent.enabled = enabled
    agent.name = name
    agent.slug = slug
    agent.description = description
    return agent


def _sharing_scope_passes(agent) -> bool:
    """Replicate the sharing scope filter logic from get_intent_catalog.

    Python equivalent of the SQL predicate now applied in ``get_intent_catalog``
    (``app.api.v1.intents._agent_shared_scope_filter``):

        or_(
            Agent.is_company_managed.is_(True),
            case(
                (json_typeof(shared) == "array", json_array_length(shared)),
                else_=0,
            ) > 0,
        )

    Per the Agent model contract, ``shared_group_ids`` is "NULL or empty = not
    shared", so an agent with ``shared_group_ids = []`` must NOT pass — using
    ``bool(...)`` mirrors the SQL length-of-array check (and rejects ``None``,
    ``[]`` and any non-array scalar as not-shared).
    """
    return agent.is_company_managed or bool(agent.shared_group_ids)


def _agent_is_shared_for_binding(agent) -> bool:
    """Replicate the upsert_binding validation logic.

    Global bindings require the agent to be company-managed or shared with
    at least one group (non-empty shared_group_ids).
    """
    return agent.is_company_managed or bool(agent.shared_group_ids)


# ── upsert_binding validation tests (unit) ───────────────────────────────────


class TestUpsertBindingShareScope:
    """Tests that upsert_binding rejects global bindings to private agents."""

    def test_private_agent_not_shared(self):
        """A private agent (not company-managed, no groups) should be
        detected as not shared.
        """
        agent = _make_agent(
            is_company_managed=False,
            user_id=uuid.uuid4(),
            shared_group_ids=None,
        )
        assert not _agent_is_shared_for_binding(agent)

    def test_company_managed_agent_is_shared(self):
        """Company-managed agents are always considered shared."""
        agent = _make_agent(is_company_managed=True, user_id=None)
        assert _agent_is_shared_for_binding(agent)

    def test_group_shared_agent_is_shared(self):
        """Agents shared with groups are considered shared."""
        agent = _make_agent(
            is_company_managed=False,
            user_id=uuid.uuid4(),
            shared_group_ids=[str(uuid.uuid4())],
        )
        assert _agent_is_shared_for_binding(agent)

    def test_empty_shared_group_ids_not_shared(self):
        """An empty shared_group_ids list is equivalent to no sharing."""
        agent = _make_agent(
            is_company_managed=False,
            user_id=uuid.uuid4(),
            shared_group_ids=[],
        )
        assert not _agent_is_shared_for_binding(agent)


# ── Catalog sharing scope filter logic tests ─────────────────────────────────


class TestCatalogSharingScope:
    """Tests that the catalog excludes private agents regardless of caller."""

    def test_private_agent_excluded_from_catalog_filter(self):
        """The sharing scope filter condition must exclude agents that are
        neither company-managed nor shared.
        """
        agent = _make_agent(
            is_company_managed=False,
            user_id=uuid.uuid4(),
            shared_group_ids=None,
        )
        assert not _sharing_scope_passes(agent)

    def test_company_managed_passes_catalog_filter(self):
        """Company-managed agents pass the sharing scope filter."""
        agent = _make_agent(is_company_managed=True, user_id=None)
        assert _sharing_scope_passes(agent)

    def test_group_shared_passes_catalog_filter(self):
        """Group-shared agents pass the sharing scope filter."""
        agent = _make_agent(
            is_company_managed=False,
            user_id=uuid.uuid4(),
            shared_group_ids=[str(uuid.uuid4())],
        )
        assert _sharing_scope_passes(agent)

    def test_admin_cannot_bypass_sharing_scope_in_catalog(self):
        """Even admin queries must not expose private agents in the catalog.

        This is the key behavioral requirement: the catalog is consumed by
        external platforms, and private agents must never leak to platform users
        regardless of how the catalog is queried.
        """
        agent = _make_agent(
            is_company_managed=False,
            user_id=uuid.uuid4(),
            shared_group_ids=None,
        )
        # The sharing scope filter is independent of admin status
        assert not _sharing_scope_passes(agent), (
            "Private agents must never pass the catalog sharing scope filter, "
            "regardless of the requester's admin status."
        )

    def test_empty_shared_groups_excluded_from_catalog(self):
        """An agent with ``shared_group_ids = []`` must be excluded from the
        catalog at the SQL level — *not* just rejected by the binding-side
        Python check.

        Per the Agent model contract, ``shared_group_ids`` is "NULL or empty
        = not shared". The catalog SQL predicate uses ``json_array_length``,
        which evaluates to 0 for an empty array (and NULL for a NULL column,
        falsy in WHERE), so both ``None`` and ``[]`` are correctly excluded.

        This test is a regression guard: an earlier draft of the filter used
        ``shared_group_ids.isnot(None)`` and would have leaked agents with
        ``shared_group_ids = []`` to all platform users.
        """
        agent = _make_agent(
            is_company_managed=False,
            user_id=uuid.uuid4(),
            shared_group_ids=[],
        )
        assert not _sharing_scope_passes(agent), (
            "shared_group_ids=[] must NOT qualify an agent as 'shared' in the "
            "catalog filter — the Agent model treats NULL and [] as equivalent."
        )
        # Binding-side validation must be consistent
        assert not _agent_is_shared_for_binding(agent)

    def _compiled_catalog_filter(self) -> str:
        """Compile the *real* ``get_intent_catalog`` sharing-scope predicate."""
        from sqlalchemy.dialects import postgresql

        from app.api.v1.intents import _agent_shared_scope_filter

        return str(
            _agent_shared_scope_filter().compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

    def test_catalog_sql_predicate_excludes_empty_arrays(self):
        """Assert the actual SQL clause built by ``get_intent_catalog``.

        Compiles the *real* filter against the PostgreSQL dialect and verifies
        it still measures the array length (not ``IS NOT NULL``). This pins the
        defensive-SQL fix so a future refactor cannot silently regress to
        ``isnot(None)`` and re-open the empty-list leak.
        """
        compiled = self._compiled_catalog_filter()
        assert "json_array_length" in compiled, (
            f"catalog sharing-scope filter must use json_array_length; got: {compiled}"
        )
        assert "IS NOT NULL" not in compiled.upper().replace("IS_COMPANY_MANAGED", ""), (
            "catalog sharing-scope filter must not reintroduce IS NOT NULL on "
            f"shared_group_ids; got: {compiled}"
        )

    def test_catalog_sql_predicate_guards_against_json_scalar(self):
        """Regression guard for the production 500 (``cannot get array length
        of a scalar``).

        ``shared_group_ids`` could hold the JSON ``null`` scalar (legacy
        ``none_as_null=False`` writes), and ``json_array_length`` raises on any
        JSON scalar/``null``. The predicate MUST guard the length behind a
        ``json_typeof(...) = 'array'`` test (via ``CASE``) so PostgreSQL never
        evaluates ``json_array_length`` on a non-array value. The ``json``
        pair, not the ``jsonb`` one: no ``::jsonb`` cast, which a row holding a
        lone surrogate escape aborts for everyone.

        A refactor that drops the guard and calls ``json_array_length``
        unconditionally trips this test before it can reach production again.
        """
        compiled = self._compiled_catalog_filter()
        assert "json_typeof" in compiled, (
            "catalog sharing-scope filter must guard json_array_length with "
            f"json_typeof; got: {compiled}"
        )
        assert "JSONB" not in compiled.upper(), (
            f"catalog sharing-scope filter must not cast to jsonb; got: {compiled}"
        )
        assert "CASE" in compiled.upper(), (
            "catalog sharing-scope filter must use a CASE expression so the "
            "type guard short-circuits before json_array_length runs on a "
            f"scalar; got: {compiled}"
        )
        # The type guard must come before the length call in the SQL text so
        # the length is only taken once the value is known to be an array.
        assert compiled.find("json_typeof") < compiled.find("json_array_length"), (
            "json_typeof guard must precede json_array_length in the compiled "
            f"predicate; got: {compiled}"
        )


# ── Platform-chat visibility (no admin bypass on external-facing endpoint) ───


class TestPlatformChatAgentVisibility:
    """``GET /platform/chat/agents`` must not expand admin visibility.

    The endpoint is consumed by OpenCTI / OpenAEV platform users (potentially
    via admin / service-account tokens). Passing ``is_admin=True`` into
    ``user_visible_filter`` short-circuits the filter to ``TRUE`` and would
    expose every enabled agent across all users — exactly the leak this PR
    is closing for the intent catalog. The list endpoint must apply the
    standard three-tier filter without the admin-bypass.
    """

    def test_platform_chat_filter_does_not_short_circuit_for_admins(self):
        from sqlalchemy.dialects import postgresql

        from app.api.v1.helpers import user_visible_filter
        from app.models.agent import Agent

        user_id = uuid.uuid4()
        group_id = str(uuid.uuid4())
        filt = user_visible_filter(Agent, user_id, [group_id])  # is_admin defaults to False
        compiled = str(
            filt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert compiled.strip().lower() != "true", (
            "platform-chat visibility filter must not collapse to TRUE; got: " + compiled
        )
        assert "user_id" in compiled.lower(), (
            "platform-chat visibility filter must restrict to the caller's own "
            f"agents; got: {compiled}"
        )

    def test_admin_short_circuit_only_when_explicitly_requested(self):
        """Sanity check: ``is_admin=True`` *does* still collapse the filter to
        the rows no listing shows anyone (a Concierge sandbox copy, #8255; the
        fleet while its flag is off, #8261).

        Pinning the existing behaviour of ``user_visible_filter`` so the
        platform-chat endpoint must remain on the default (False) variant.
        """
        from sqlalchemy.dialects import postgresql

        from app.api.v1.helpers import hidden_rows_excluded, user_visible_filter
        from app.models.agent import Agent

        def _sql(clause) -> str:
            return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

        compiled = _sql(user_visible_filter(Agent, uuid.uuid4(), [], is_admin=True))
        assert compiled.strip().lower() == _sql(hidden_rows_excluded(Agent)).strip().lower(), (
            "user_visible_filter must still collapse to the hidden-row exclusion "
            "alone when is_admin=True; got: " + compiled
        )


# ── Malformed UUID handling on upsert_binding (HTTP 422, not 500) ────────────


class TestUpsertBindingMalformedAgentId:
    """``parse_uuid`` is the repo helper that turns malformed UUIDs into
    HTTP 422 responses. The binding endpoint must use it for ``body.agent_id``
    so a junk client payload doesn't crash with a 500 from ``ValueError``.
    """

    def test_parse_uuid_rejects_garbage_with_422(self):
        from app.api.v1.helpers import parse_uuid

        with pytest.raises(HTTPException) as exc_info:
            parse_uuid("not-a-uuid", label="agent ID")
        assert exc_info.value.status_code == 422
        assert "agent ID" in str(exc_info.value.detail)

    def test_parse_uuid_accepts_valid_uuid(self):
        from app.api.v1.helpers import parse_uuid

        candidate = str(uuid.uuid4())
        parsed = parse_uuid(candidate, label="agent ID")
        assert str(parsed) == candidate
