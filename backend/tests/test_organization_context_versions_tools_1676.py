"""Profile versions, restore, based_on_version and the OpenCRQ reconcile as agent tools (#1676)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.builtin_tools import build_organization_context_write_tools
from app.services.builtin_tools import organization_context_write as module

_ADMIN_ID = "00000000-0000-4000-8000-000000001676"


class _Session:
    """An async session double: an administrator (or not), commits counted."""

    def __init__(self, *, admin: bool = True):
        self.admin, self.commits = admin, 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _id):
        return SimpleNamespace(id=_id, is_admin=self.admin)

    async def commit(self):
        self.commits += 1


def _tools(monkeypatch, session):
    from app.core import database

    monkeypatch.setattr(database, "async_session", lambda: session)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True, agent_name="Ops")
    return executor


@pytest.mark.asyncio
async def test_a_non_administrator_reads_restores_and_reconciles_nothing(monkeypatch):
    executor = _tools(monkeypatch, _Session(admin=False))
    assert await executor.handler(module.ORGANIZATION_VERSIONS_TOOL)() == module.REFUSAL
    assert await executor.handler(module.ORGANIZATION_RESTORE_TOOL)(version=3) == module.REFUSAL
    assert await executor.handler("reconcile_organization_with_opencrq")() == module.REFUSAL


@pytest.mark.asyncio
async def test_the_versions_are_listed_and_one_is_read_whole(monkeypatch):
    from app.api.v1 import organization_context as routes

    seen: dict = {}

    async def _list(limit, db, _admin):
        seen["limit"] = limit
        return [
            routes.OrganizationContextVersionSummary(
                version=4,
                source="form",
                created_at=datetime(2026, 11, 23, tzinfo=UTC),
                author_user_id=None,
                missing_fields=[],
                primary_assets=7,
            )
        ]

    async def _one(version, db, _admin):
        if version != 2:
            raise HTTPException(
                status_code=404, detail=f"Version {version} of the organization profile does not exist."
            )
        return routes.OrganizationContextResponse(
            version=2,
            source="form",
            created_at=None,
            author_user_id=None,
            profile={"primary_assets": [{"name": "Billing"}]},
            missing_fields=[],
        )

    monkeypatch.setattr(routes, "list_organization_context_versions", _list)
    monkeypatch.setattr(routes, "get_organization_context_version", _one)
    executor = _tools(monkeypatch, _Session())
    listed = json.loads(await executor.handler(module.ORGANIZATION_VERSIONS_TOOL)(limit=500))
    assert listed["versions"][0]["version"] == 4 and listed["versions"][0]["primary_assets"] == 7
    assert seen["limit"] == 100
    one = json.loads(await executor.handler(module.ORGANIZATION_VERSIONS_TOOL)(version=2))
    assert one["profile"]["primary_assets"] == [{"name": "Billing"}]
    missing = await executor.handler(module.ORGANIZATION_VERSIONS_TOOL)(version=9)
    assert missing.startswith("Not read: Version 9")


@pytest.mark.asyncio
async def test_a_restore_goes_through_the_route_with_its_base_and_is_audited(monkeypatch):
    from app.api.v1 import organization_context as routes
    from app.services import audit

    seen: dict = {}

    async def _restore(version, body, db, admin):
        seen["call"] = (version, body.fields, body.based_on_version, admin.is_admin)
        return routes.OrganizationContextResponse(
            version=6,
            source="api",
            created_at=None,
            author_user_id=None,
            profile={},
            missing_fields=[],
            removed_rows={"assets_exposure": ["old host"]},
        )

    async def _log_audit(db, **kwargs):
        seen["audit"] = kwargs

    session = _Session()
    monkeypatch.setattr(routes, "restore_organization_context_version", _restore)
    monkeypatch.setattr(audit, "log_audit", _log_audit)
    executor = _tools(monkeypatch, session)
    answer = json.loads(
        await executor.handler(module.ORGANIZATION_RESTORE_TOOL)(version=3, fields=["primary_assets"], based_on_version=5)
    )
    assert seen["call"] == (3, ["primary_assets"], 5, True)
    assert answer["version"] == 6 and answer["restored_from"] == 3
    assert answer["removed_rows"] == {"assets_exposure": ["old host"]}
    assert seen["audit"]["details"]["restored_from"] == 3 and seen["audit"]["details"]["agent"] == "Ops"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_a_stale_restore_answers_the_conflict_and_commits_nothing(monkeypatch):
    from app.api.v1 import organization_context as routes

    async def _restore(version, body, db, admin):
        raise HTTPException(status_code=409, detail="The profile changed since version 5 (now version 6).")

    session = _Session()
    monkeypatch.setattr(routes, "restore_organization_context_version", _restore)
    executor = _tools(monkeypatch, session)
    answer = await executor.handler(module.ORGANIZATION_RESTORE_TOOL)(version=3, based_on_version=5)
    assert answer == "Not restored: The profile changed since version 5 (now version 6)."
    assert session.commits == 0


@pytest.mark.asyncio
async def test_the_opencrq_reconcile_runs_the_service_dry_first(monkeypatch):
    from app.services import organization_context as org_context
    from app.services import organization_context_opencrq as opencrq
    from app.services.organization_context_agent_tools import OPENCRQ_RECONCILE_TOOL

    seen: list = []

    async def _run_sync(db, **kwargs):
        seen.append(kwargs)
        if kwargs["tenant_id"] == "gone":
            return None, ["OpenCRQ cannot be read"]
        return SimpleNamespace(summary=lambda: {"linked": []}), []

    async def _current(db, organization_id=None):
        return SimpleNamespace(version=4)

    session = _Session()
    monkeypatch.setattr(opencrq, "run_sync", _run_sync)
    monkeypatch.setattr(org_context, "current_version", _current)
    executor = _tools(monkeypatch, session)
    reconcile = executor.handler(OPENCRQ_RECONCILE_TOOL)
    dry = json.loads(await reconcile())
    assert dry == {"applied": False, "plan": {"linked": []}, "errors": [], "version": 4}
    assert seen[-1]["apply"] is False and seen[-1]["user_id"] == uuid.UUID(_ADMIN_ID)
    applied = json.loads(await reconcile(apply=True, min_criticality="medium"))
    assert applied["applied"] is True and seen[-1]["min_criticality"] == "medium"
    assert (await reconcile(tenant_id="gone")) == "Not run: OpenCRQ cannot be read"
    calls = len(seen)
    assert (await reconcile(min_criticality="extreme")).startswith("Not run: min_criticality")
    assert len(seen) == calls


@pytest.mark.asyncio
async def test_update_passes_its_base_version_to_apply_changes(monkeypatch):
    from app.services import audit
    from app.services import organization_context_proposals as proposals

    seen: dict = {}

    async def _apply(db, **kwargs):
        seen.update(kwargs)
        return {"organization_id": str(uuid.uuid4()), "organization": "Acme", "version": 5, "fields": ["sector"]}

    async def _log_audit(db, **kwargs):
        return None

    monkeypatch.setattr(proposals, "apply_changes", _apply)
    monkeypatch.setattr(audit, "log_audit", _log_audit)
    executor = _tools(monkeypatch, _Session())
    await executor.handler(module.ORGANIZATION_UPDATE_TOOL)(
        changes=[{"field": "sector", "value": "x"}], based_on_version=4
    )
    assert seen["based_on_version"] == 4


@pytest.mark.asyncio
async def test_apply_changes_refuses_a_stale_base_before_any_write(monkeypatch):
    from app.services import organization_context as org_context
    from app.services import organization_context_proposals as proposals
    from app.services import organizations as orgs

    target = SimpleNamespace(id=uuid.uuid4(), name="Acme", role="primary")

    async def _resolve(db, name):
        return target

    async def _current(db, organization_id=None):
        return SimpleNamespace(version=6, profile={})

    async def _save(*_args, **_kwargs):  # pragma: no cover - the refusal comes first
        raise AssertionError("saved on a stale base")

    monkeypatch.setattr(orgs, "resolve", _resolve)
    monkeypatch.setattr(org_context, "current_version", _current)
    monkeypatch.setattr(org_context, "save_version", _save)
    with pytest.raises(proposals.ProposalError, match="changed since version 5"):
        await proposals.apply_changes(
            None, author_user_id=uuid.UUID(_ADMIN_ID), changes=[{"field": "sector", "value": "x"}], based_on_version=5
        )
