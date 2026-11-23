"""Trigger provisioning through a per-user template binding (#1378).

An agent that reaches OpenCTI or OpenAEV only through
``integration_template_bindings`` (the seeded built-in agents) binds no
integration id: the provisioners resolve the one the run-as user gets at run
time, a direct binding first.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import true

from app.services import openaev_trigger_provisioning as aev
from app.services import opencti_trigger_provisioning as prov
from app.services import trigger_provisioning as tp
from app.services.fleet_run_as import NO_RUN_AS_REASON

RUN_AS = uuid.uuid4()
OTHER = uuid.uuid4()


def _agent(bindings: Any = None, templates: Any = None, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Loop Orchestrator",
        user_id=OTHER,
        integration_bindings=bindings,
        integration_template_bindings=templates,
        is_company_managed=kw.get("company", True),
    )


def _assignment(run_as: Any = RUN_AS, bindings: Any = None, config: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="New relevant threat",
        user_id=OTHER,
        run_as_user_id=run_as,
        integration_bindings=bindings,
        enabled=True,
        trigger_config=config or {},
    )


def _integration(owner: Any, *, provider: str = "opencti", company: bool = False, scope: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider=provider,
        user_id=owner,
        is_company_managed=company,
        auth_type="api_key",
        scope_config=scope,
    )


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return self._rows


class FakeDb:
    """Answers the candidate query with *rows* and ``get`` by id."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.queries = 0

    async def execute(self, _stmt: Any) -> _Rows:
        self.queries += 1
        return _Rows(self.rows)

    async def get(self, _model: Any, key: Any) -> Any:
        return next((r for r in self.rows if str(r.id) == str(key)), None)


@pytest.fixture(autouse=True)
def _candidate_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    # The SQL eligibility is the run path's own (covered with it); the fake db
    # answers the candidates the user is eligible for.
    async def _filter(_db: Any, _user: Any, *, company_automation: bool = False) -> Any:
        return true()

    monkeypatch.setattr("app.services.integration_tools.template_candidate_filter", _filter)


@pytest.mark.asyncio
async def test_template_binding_resolves_the_run_as_users_integration() -> None:
    own = _integration(RUN_AS)
    db = FakeDb([own])
    integration_id, reason = await prov.opencti_integration_id(db, _assignment(), _agent({}, ["opencti"]))
    assert (integration_id, reason) == (str(own.id), "")


@pytest.mark.asyncio
async def test_the_users_own_integration_wins_over_a_company_one_and_the_pick_is_stable() -> None:
    company = _integration(OTHER, company=True)
    own = _integration(RUN_AS)
    db = FakeDb([company, own])
    first, _ = await prov.opencti_integration_id(db, _assignment(), _agent({}, ["opencti"]))
    second, _ = await prov.opencti_integration_id(db, _assignment(), _agent({}, ["opencti"]))
    assert first == second == str(own.id)
    company_only, _ = await prov.opencti_integration_id(FakeDb([company]), _assignment(), _agent({}, ["opencti"]))
    assert company_only == str(company.id)


@pytest.mark.asyncio
async def test_a_direct_binding_wins_over_the_template() -> None:
    db = FakeDb([_integration(RUN_AS)])
    agent = _agent({"opencti": ["direct-id"]}, ["opencti"])
    assert await prov.opencti_integration_id(db, _assignment(), agent) == ("direct-id", "")
    assert db.queries == 0
    own = await prov.opencti_integration_id(db, _assignment(bindings={"opencti": "on-assignment"}), agent)
    assert own == ("on-assignment", "")


@pytest.mark.asyncio
async def test_no_resolvable_integration_is_said_plainly() -> None:
    integration_id, reason = await prov.opencti_integration_id(FakeDb([]), _assignment(), _agent({}, ["opencti"]))
    assert integration_id is None
    assert reason == "template binding opencti: no OpenCTI integration resolvable for the run-as user"


@pytest.mark.asyncio
async def test_a_colleagues_personal_integration_is_never_picked() -> None:
    db = FakeDb([_integration(OTHER)])
    integration_id, reason = await prov.opencti_integration_id(db, _assignment(), _agent({}, ["opencti"]))
    assert integration_id is None and "no OpenCTI integration resolvable" in reason


@pytest.mark.asyncio
async def test_several_scoped_integrations_are_not_auto_picked() -> None:
    db = FakeDb([_integration(RUN_AS, scope={"a": 1}), _integration(RUN_AS, scope={"b": 1})])
    integration_id, reason = await prov.opencti_integration_id(db, _assignment(), _agent({}, ["opencti"]))
    assert integration_id is None and "several scoped OpenCTI integrations" in reason


@pytest.mark.asyncio
async def test_no_template_and_no_binding_keeps_the_old_reason() -> None:
    integration_id, reason = await prov.opencti_integration_id(FakeDb([]), _assignment(), _agent({}, []))
    assert integration_id is None
    assert reason == "no OpenCTI integration bound to the assignment or its agent"


@pytest.mark.asyncio
async def test_a_fleet_automation_without_run_as_never_resolves_as_the_seed_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.fleet_run_as.is_fleet_automation", lambda _agent, _assignment: True)
    db = FakeDb([_integration(OTHER)])
    integration_id, reason = await prov.opencti_integration_id(db, _assignment(run_as=None), _agent({}, ["opencti"]))
    assert (integration_id, reason) == (None, NO_RUN_AS_REASON)
    assert db.queries == 0


@pytest.mark.asyncio
async def test_provisioning_reaches_provisioned_through_the_template(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_opencti_trigger_provisioning import SPEC_CONFIG, FakeOpenCTI

    own = _integration(RUN_AS)
    db = FakeDb([own])
    octi = FakeOpenCTI(labels={"ai": "label-ai"})
    seen: dict[str, Any] = {}

    async def _secret(_integration: Any) -> str:
        return "s"

    async def _gql(_db: Any, integration: Any, user_id: Any, *, timeout: int = 30) -> Any:
        seen["integration"], seen["user"] = integration, user_id
        return octi, "https://octi.example"

    monkeypatch.setattr(tp, "receiver_secret", _secret)
    monkeypatch.setattr(prov, "integration_gql", _gql)
    assignment = _assignment(config=dict(SPEC_CONFIG))
    state = await prov.provision_one(db, assignment, _agent({}, ["opencti"]))
    assert state is not None and state["status"] == "provisioned", state
    assert state["integration_id"] == str(own.id)
    assert seen == {"integration": own, "user": RUN_AS}
    assert len(octi.notifiers) == 1 and len(octi.triggers) == 1
    url = octi.notifiers[state["notifier_id"]]["notifier_configuration"]
    assert f"/integrations/{own.id}/assignments/{assignment.id}" in url


@pytest.mark.asyncio
async def test_openaev_resolves_its_template_binding_the_same_way() -> None:
    own = _integration(RUN_AS, provider="openaev")
    db = FakeDb([own])
    assert await aev.openaev_integration_id(db, _assignment(), _agent({}, ["openaev"])) == (str(own.id), "")
    assert await aev.openaev_integration_id(db, _assignment(), _agent({"openaev": ["d"]}, ["openaev"])) == ("d", "")
    integration_id, reason = await aev.openaev_integration_id(FakeDb([]), _assignment(), _agent({}, ["openaev"]))
    assert integration_id is None
    assert reason == "template binding openaev: no OpenAEV integration resolvable for the run-as user"


@pytest.mark.asyncio
async def test_the_pir_score_reread_uses_the_provisioned_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_trigger_rules

    own = _integration(RUN_AS)
    db = FakeDb([own])
    used: dict[str, Any] = {}

    async def _gql(_db: Any, integration: Any, user_id: Any, *, timeout: int = 30) -> Any:
        used["integration"] = integration
        return "gql", "https://octi.example"

    monkeypatch.setattr(prov, "integration_gql", _gql)
    config = {"provider": "opencti", prov.STATE_KEY: {"status": "provisioned", "integration_id": str(own.id)}}
    assert await ctem_trigger_rules._live_gql(db, config, _agent({}, ["opencti"]), None) == "gql"
    assert used["integration"] is own
