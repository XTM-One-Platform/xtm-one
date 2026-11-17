"""A catalog install says which variables still need a value.

"Add to my account" copies the variables an agent's text names into the
installer's account. A secret - a company-managed one included - is copied
WITHOUT its value, on purpose: the secret never leaves its owner. The install
used to say nothing, so the agent silently ran with an unresolved placeholder.
The install result now lists the variables that still need a value; the agent
page reads the same list live (``GET /agents/{id}/missing-variables``, proven
against PostgreSQL in ``test_catalog_clone_variables_postgres.py``).
"""

from __future__ import annotations

import uuid

from app.api.v1 import public_catalog
from app.models.agent import Agent
from app.models.variable import Variable
from app.services.variable_resolver import variable_has_value
from tests.test_catalog_clone import _CatalogSession, _installer, _noop, _published_agent


def test_a_variable_has_a_value_where_its_kind_keeps_it():
    assert variable_has_value(Variable(is_secret=True, value_enc="cipher"))
    assert not variable_has_value(Variable(is_secret=True, value=None, value_enc=None))
    assert variable_has_value(Variable(is_secret=False, value="emea"))
    assert not variable_has_value(Variable(is_secret=False, value=""))


async def test_an_install_lists_the_secrets_it_copied_without_their_value(monkeypatch):
    monkeypatch.setattr(public_catalog, "log_audit", _noop)
    monkeypatch.setattr(public_catalog, "reconcile_agent_skill_package_bindings", _noop)
    monkeypatch.setattr(public_catalog, "_seed_initial_versions", _noop)
    secret = Variable(
        id=uuid.uuid4(), name="API_KEY", is_secret=True, value_enc="cipher", is_company_managed=True
    )
    region = Variable(
        id=uuid.uuid4(), name="REGION", is_secret=False, value="emea", is_company_managed=True
    )
    source: Agent = _published_agent()
    source.persona = f"Key {{{{var_id.{secret.id}}}}} in {{{{var_id.{region.id}}}}}"
    session = _CatalogSession(source, [], [secret, region])

    response = await public_catalog.add_catalog_item_to_account(
        entity_type="agents",
        entity_id=str(source.id),
        body=None,
        user=_installer(),
        db=session,
        _ai_gate=None,
    )

    copies = {v.name: v for v in session.of(Variable)}
    # The secret is never copied; the plain value is.
    assert copies["API_KEY"].value is None and copies["API_KEY"].value_enc is None
    assert copies["REGION"].value == "emea"
    assert [(v.name, v.is_secret, v.id) for v in response.variables_needing_value] == [
        ("API_KEY", True, str(copies["API_KEY"].id))
    ]


async def test_an_install_with_nothing_to_fill_says_so(monkeypatch):
    monkeypatch.setattr(public_catalog, "log_audit", _noop)
    monkeypatch.setattr(public_catalog, "reconcile_agent_skill_package_bindings", _noop)
    monkeypatch.setattr(public_catalog, "_seed_initial_versions", _noop)
    source = _published_agent()
    session = _CatalogSession(source, [], [])

    response = await public_catalog.add_catalog_item_to_account(
        entity_type="agents",
        entity_id=str(source.id),
        body=None,
        user=_installer(),
        db=session,
        _ai_gate=None,
    )

    assert response.variables_needing_value == []
