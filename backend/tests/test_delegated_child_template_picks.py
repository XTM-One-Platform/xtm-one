"""A background task resolves its per-user bindings to its parent run's rows (#1792).

The run that spawns a task records ``{provider: integration id}`` of its
template bindings on the task; the task's turn takes the same row when it is
among its own eligible candidates, so parent and child never act as two
accounts (on OpenAEV: two tenants, where the child answers 404 on what the
parent reads). Inheriting never widens what the child may use: another
user's personal row stays dropped fail-closed.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.integration_tools import (
    DIRECT_BINDING_ORIGIN,
    INHERITED_TEMPLATE_PICKS_KEY,
    TEMPLATE_ORIGIN_COMPANY_FALLBACK,
    TEMPLATE_ORIGIN_OWN,
    inherited_template_candidate,
    inherited_template_picks,
    resolve_template_integration,
    template_picks,
)

USER = uuid.uuid4()
OTHER = uuid.uuid4()


def _integration(*, owner: uuid.UUID | None, company: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="openaev",
        user_id=owner,
        is_company_managed=company,
        auth_type="api_key",
        scope_config=None,
        name="openaev",
    )


def test_template_picks_keep_template_origins_only() -> None:
    bindings = {"openaev": "a", "github": "b", "openaev#2": "c", "jira": "d"}
    origins = {
        "openaev": TEMPLATE_ORIGIN_OWN,
        "github": DIRECT_BINDING_ORIGIN,
        "openaev#2": TEMPLATE_ORIGIN_OWN,
        "jira": TEMPLATE_ORIGIN_COMPANY_FALLBACK,
    }
    assert template_picks(bindings, origins) == {"openaev": "a", "jira": "d"}


def test_inherited_picks_drop_malformed_entries() -> None:
    assert inherited_template_picks(None) == {}
    assert inherited_template_picks("openaev") == {}
    assert inherited_template_picks({"openaev": "x", "": "y", "jira": 3}) == {"openaev": "x"}
    assert INHERITED_TEMPLATE_PICKS_KEY == "inherited_template_picks"


def test_inherited_candidate_only_from_the_childs_eligible_set() -> None:
    own = _integration(owner=USER)
    company = _integration(owner=None, company=True)
    assert inherited_template_candidate([own, company], str(company.id)) is company
    assert inherited_template_candidate([own], str(company.id)) is None
    assert inherited_template_candidate([own], None) is None


@pytest.mark.asyncio
async def test_child_resolves_the_parents_row_and_origin() -> None:
    """Parent picked the company row; the child, which would prefer its own, keeps the parent's."""
    own = _integration(owner=USER)
    company = _integration(owner=None, company=True)
    parent, parent_origin = await resolve_template_integration(None, "openaev", [company], None, acting_user_id=USER)
    assert parent is company and parent_origin == TEMPLATE_ORIGIN_COMPANY_FALLBACK
    child, child_origin = await resolve_template_integration(
        None,
        "openaev",
        [own, company],
        None,
        acting_user_id=USER,
        inherited_id=str(parent.id),
    )
    assert child is parent
    assert child_origin == parent_origin


def test_openaev_not_found_names_the_tenant_and_how_it_was_chosen() -> None:
    from app.integrations.providers.openaev._common import _with_scope_hint

    hint = " Note: this request went to the OpenAEV tenant t-1 (chosen automatically)."
    assert _with_scope_hint("OpenAEV error (HTTP 404): Not Found", hint).endswith(hint)
    assert _with_scope_hint("TENANT_ACCESS_DENIED", hint).endswith(hint)
    assert _with_scope_hint("OpenAEV error (HTTP 500): boom", hint) == "OpenAEV error (HTTP 500): boom"
    assert _with_scope_hint(None, hint) is None


@pytest.mark.asyncio
async def test_never_inherits_another_users_personal_row() -> None:
    foreign = _integration(owner=OTHER)
    own = _integration(owner=USER)
    child, origin = await resolve_template_integration(
        None,
        "openaev",
        [foreign, own],
        None,
        acting_user_id=USER,
        inherited_id=str(foreign.id),
    )
    assert child is own
    assert origin == TEMPLATE_ORIGIN_OWN
