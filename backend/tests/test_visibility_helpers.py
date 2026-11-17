from __future__ import annotations

import uuid
from typing import get_args

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

# Importing the model registry populates ``Base.metadata`` with every table.
import app.models  # noqa: F401
from app.api.v1.helpers import VisibilityQueryable, user_visible_filter
from app.core.database import Base
from app.models.agent import Agent
from app.models.integration import Integration


def _rendered_where(model_cls, user_id, user_group_ids) -> str:
    """Render only the visibility predicate.

    Compiling the whole ``SELECT`` would drag the column list into the string,
    so ``shared_group_ids`` / ``shared_user_ids`` appear whether or not they are
    part of the WHERE clause — which makes any per-column assertion below
    vacuously true.
    """
    return str(
        user_visible_filter(model_cls, user_id, user_group_ids).compile(
            dialect=postgresql.dialect()
        )
    )


def test_user_visible_filter_uses_jsonb_has_any_for_shared_groups():
    user_id = uuid.uuid4()
    user_group_ids = [str(uuid.uuid4()) for _ in range(24)]

    rendered = _rendered_where(Integration, user_id, user_group_ids).lower()

    assert "?|" in rendered
    # Two ``?|`` predicates: one per principal kind (groups + users).
    # Each column must contribute exactly one — a second on either would mean
    # the group ids stopped being collapsed into a single array bind.
    assert rendered.count("?|") == 2
    assert rendered.count("shared_group_ids") == 1
    assert rendered.count("shared_user_ids") == 1
    assert "jsonb_build_array" not in rendered
    assert "@>" not in rendered


def test_user_visible_filter_binds_text_array_not_varchar_array():
    """The ``?|`` JSONB operator is defined as ``jsonb ?| text[]`` — binding
    the right-hand side as ``varchar[]`` forces an implicit cast that can
    suppress the GIN expression index. Pin the rendered SQL to ``text[]`` so
    a future refactor cannot silently regress to ``varchar[]``.
    """
    user_id = uuid.uuid4()
    user_group_ids = [str(uuid.uuid4()) for _ in range(3)]

    statement = select(Integration).where(
        user_visible_filter(Integration, user_id, user_group_ids)
    )
    rendered = str(statement.compile(dialect=postgresql.dialect()))

    assert "TEXT[]" in rendered.upper(), (
        "shared_group_ids overlap bind must be text[] for direct ?| operator "
        f"match; got: {rendered}"
    )
    assert "VARCHAR[]" not in rendered.upper(), (
        "shared_group_ids overlap bind must NOT be varchar[] — implicit "
        f"varchar[]→text[] casts can disable GIN index lookups; got: {rendered}"
    )


@pytest.mark.parametrize("model_cls", [Integration, Agent])
def test_user_visible_filter_jsonb_has_any_for_multiple_models(model_cls):
    """The visibility helper is generic across every model that exposes
    ``shared_group_ids``. Pin the new ``?|`` + ``text[]`` shape on multiple
    callers so a regression in the shared helper trips here for any of them.
    """
    user_id = uuid.uuid4()
    user_group_ids = [str(uuid.uuid4()) for _ in range(5)]

    statement = select(model_cls).where(
        user_visible_filter(model_cls, user_id, user_group_ids)
    )
    rendered = str(statement.compile(dialect=postgresql.dialect()))

    assert "?|" in rendered
    assert "TEXT[]" in rendered.upper()
    assert "@>" not in rendered
    assert "jsonb_build_array" not in rendered


def test_user_visible_filter_empty_groups_returns_no_overlap_predicate():
    """When the user has no groups, the shared-group branch must short-circuit
    and not emit a ``?|`` predicate for ``shared_group_ids`` at all (avoids a
    pointless index lookup on every query for users without group memberships).

    The ``shared_user_ids`` predicate has no equivalent short-circuit and is
    expected here: every request carries exactly one concrete acting user, so
    there is no "empty" case to collapse.
    """
    user_id = uuid.uuid4()

    rendered = _rendered_where(Integration, user_id, [])

    assert "shared_group_ids" not in rendered, (
        "empty user_group_ids must short-circuit, not emit a group predicate; "
        f"got: {rendered}"
    )
    assert rendered.count("?|") == 1, (
        "only the per-user grant predicate should remain; got: " + rendered
    )
    assert "shared_user_ids" in rendered


def test_user_visible_filter_matches_direct_user_grants():
    """A per-user grant is one of the visibility tiers, not a bolt-on: it must
    be OR-ed into the same predicate the API, chat and assignment engines all
    share, so no surface can be reached that filters on the old rules.
    """
    user_id = uuid.uuid4()

    rendered = _rendered_where(Agent, user_id, [])

    assert "shared_user_ids" in rendered
    assert "TEXT[]" in rendered.upper(), (
        "the user-grant bind must be text[] to match ?| directly and keep the "
        f"GIN expression index usable; got: {rendered}"
    )


def test_user_visible_filter_skips_company_managed_when_access_restricted():
    """``access_restricted`` withdraws the blanket company-managed clause.

    ``IS NOT TRUE`` rather than ``IS FALSE``: the column can exist bare-nullable
    on databases where ``_ensure_model_columns`` created it ahead of the
    migration, and a NULL there means "never configured" — it must not
    retroactively hide every company-managed entity on those installs.
    """
    rendered = _rendered_where(Agent, uuid.uuid4(), []).lower()

    assert "access_restricted is not true" in rendered
    assert "access_restricted is false" not in rendered


# ── The column-set contract every queried entity must satisfy ────────────────
#
# ``user_visible_filter`` is generic over ``VisibilityQueryable`` and reads
# ``access_restricted`` and ``shared_user_ids`` unconditionally on its non-admin
# branch. An entity queried through it without those columns therefore raises
# ``AttributeError`` at query-build time — invisibly, because the admin branch
# short-circuits to ``TRUE`` before touching them. That is exactly how ``Flow``
# and ``FlowEventType`` (created with only the group columns, then queried
# through this helper) 500ed ``/flows`` and ``/chat`` for every non-admin user
# the moment the flow builder shipped unflagged. These guards pin the contract
# for the whole union, so the next entity to miss the sweep fails here instead
# of in production.

_REQUIRED_VISIBILITY_COLUMNS = (
    "is_company_managed",
    "shared_group_ids",
    "shared_group_permissions",
    "shared_user_ids",
    "shared_user_permissions",
    "access_restricted",
)

#: Tables carrying ``shared_group_ids`` that are deliberately NOT queried
#: through ``user_visible_filter``: they snapshot an audience and evaluate it in
#: Python instead. Anything else with the group columns must be in the union —
#: that is the omission this whole block exists to catch.
#:
#: An exemption is not a lighter contract. A hand-written copy of the rule must
#: evaluate every tier the SQL predicate does, so each exempt table maps to the
#: audience columns its copy needs, asserted below. ``flow_runs`` shipped
#: covering only the group tier, which left the run journal of an
#: ``access_restricted`` company flow readable by every authenticated user.
_HAND_FILTERED_AUDIENCE_COLUMNS = {
    "flow_runs": (
        "is_company_managed",
        "access_restricted",
        "shared_group_ids",
        "shared_user_ids",
    ),
    # An LLM provider is not a library entity: admins own every row, and the
    # grant only decides who a run may bill to it. Its rule is
    # ``app.llm.provider_access.provider_usable_by``: unrestricted, or the
    # caller named directly or through a group.
    "llm_provider_configs": (
        "access_restricted",
        "shared_group_ids",
        "shared_user_ids",
    ),
}
_HAND_FILTERED_TABLES = frozenset(_HAND_FILTERED_AUDIENCE_COLUMNS)


def _visibility_union_models() -> list[type]:
    """Every model the visibility helpers are declared to accept.

    ``VisibilityQueryable`` nests ``CompanyManagedEntity``, and ``get_args``
    does not expand a nested alias, so flatten one level.
    """
    models: list[type] = []
    for member in get_args(VisibilityQueryable.__value__):
        value = getattr(member, "__value__", None)
        models.extend(get_args(value) if value is not None else [member])
    return models


@pytest.mark.parametrize(
    "model_cls", _visibility_union_models(), ids=lambda m: m.__tablename__
)
def test_visibility_union_models_declare_the_full_column_set(model_cls):
    missing = [
        col for col in _REQUIRED_VISIBILITY_COLUMNS if not hasattr(model_cls, col)
    ]
    assert not missing, (
        f"{model_cls.__name__} is in VisibilityQueryable but declares none of "
        f"{missing} — user_visible_filter reads them unconditionally on the "
        "non-admin branch, so every list endpoint for this entity 500s for "
        "non-admin users while working fine for admins."
    )


@pytest.mark.parametrize(
    "model_cls", _visibility_union_models(), ids=lambda m: m.__tablename__
)
def test_user_visible_filter_builds_for_every_union_model(model_cls):
    """The non-admin predicate must actually compile for every union member.

    The column-set check above is the *why*; this is the failure mode itself —
    building the predicate is what raised ``AttributeError: type object 'Flow'
    has no attribute 'access_restricted'`` in production.
    """
    rendered = _rendered_where(model_cls, uuid.uuid4(), [str(uuid.uuid4())]).lower()

    assert "access_restricted is not true" in rendered
    assert "shared_user_ids" in rendered
    assert "shared_group_ids" in rendered


def test_every_shared_group_table_is_either_queried_or_hand_filtered():
    """No table may carry the group columns yet sit outside both lists.

    ``Flow`` was in neither: queried through ``user_visible_filter`` but absent
    from the union, so nothing typed or tested the mismatch.
    """
    union_tables = {m.__tablename__ for m in _visibility_union_models()}
    with_group_columns = {
        name
        for name, table in Base.metadata.tables.items()
        if "shared_group_ids" in table.columns
    }

    unaccounted = with_group_columns - union_tables - _HAND_FILTERED_TABLES

    assert not unaccounted, (
        f"{sorted(unaccounted)} carry shared_group_ids but are neither in "
        "VisibilityQueryable nor documented as hand-filtered. If the entity is "
        "listed through user_visible_filter, add the per-user columns and put "
        "it in the union; if it filters visibility itself, add it to "
        "_HAND_FILTERED_TABLES with the reason."
    )


@pytest.mark.parametrize(
    ("table", "columns"), sorted(_HAND_FILTERED_AUDIENCE_COLUMNS.items())
)
def test_hand_filtered_tables_carry_the_audience_they_must_evaluate(table, columns):
    """Exempt from the helper, not from the rule.

    A table that re-implements visibility in Python can only enforce the tiers
    it stores. Missing one is not a crash — it is silently permissive, which is
    strictly worse than the 500 the union members get.
    """
    declared = Base.metadata.tables[table].columns
    missing = [col for col in columns if col not in declared]

    assert not missing, (
        f"{table} filters visibility in Python but does not store {missing} — "
        "the tier it cannot see is the tier it cannot enforce."
    )
