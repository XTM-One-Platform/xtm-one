"""The "Run checks as" user of Platform Health (platform-health.md 12.3): the save refuses a user who
cannot read a registered module, and each module card reads one state when the checks have no usable user."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.api.v1 import platform_health
from app.services.health import run_as
from app.services.health.settings import HealthSettings


def test_the_setting_defaults_to_not_set():
    assert HealthSettings().module_run_as_user_id is None


def test_the_refusal_names_every_module():
    message = run_as.run_as_refusal(["opencti", "openaev"])
    assert "OpenCTI, OpenAEV" in message
    assert "pick a user who has it" in message


def test_the_card_reason_names_the_module_and_the_setting():
    reason = run_as.run_as_needed_reason("openaev")
    assert reason.startswith("Checks need a run-as user with OpenAEV access")
    assert "Run checks as" in reason


def _patch_access(monkeypatch, *, active: bool, integrations: dict, reachable: set):
    async def _active(db, user_id):
        return active

    async def _registered(db):
        return integrations

    async def _reaches(db, user_id, integration_id):
        return integration_id in reachable

    monkeypatch.setattr(run_as, "user_is_active", _active)
    monkeypatch.setattr(run_as, "registered_module_integrations", _registered)
    monkeypatch.setattr(run_as, "reaches_integration", _reaches)


async def test_missing_module_access_lists_the_unreachable_modules(monkeypatch):
    cti, aev, crq = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _patch_access(
        monkeypatch, active=True, integrations={"opencti": [cti], "openaev": [aev], "opencrq": [crq]}, reachable={crq}
    )
    assert await run_as.missing_module_access(None, uuid.uuid4()) == ["opencti", "openaev"]


async def test_an_inactive_user_misses_every_module(monkeypatch):
    cti = uuid.uuid4()
    _patch_access(monkeypatch, active=False, integrations={"opencti": [cti]}, reachable={cti})
    assert await run_as.missing_module_access(None, uuid.uuid4()) == ["opencti"]


async def test_the_save_refuses_a_user_without_module_access(monkeypatch):
    cti = uuid.uuid4()
    _patch_access(monkeypatch, active=True, integrations={"opencti": [cti]}, reachable=set())
    with pytest.raises(HTTPException) as refused:
        await platform_health.check_module_run_as(None, uuid.uuid4())
    assert refused.value.status_code == 400
    detail = refused.value.detail
    assert detail["code"] == run_as.RUN_AS_MISSING_MODULE_ACCESS
    assert detail["modules"] == ["OpenCTI"]
    assert "no access to OpenCTI" in detail["message"]


async def test_the_save_accepts_a_user_with_every_module(monkeypatch):
    cti = uuid.uuid4()
    _patch_access(monkeypatch, active=True, integrations={"opencti": [cti]}, reachable={cti})
    await platform_health.check_module_run_as(None, uuid.uuid4())


async def test_the_save_accepts_clearing_the_user():
    await platform_health.check_module_run_as(None, None)


async def test_the_save_refuses_an_unknown_user(monkeypatch):
    _patch_access(monkeypatch, active=False, integrations={}, reachable=set())
    with pytest.raises(HTTPException) as refused:
        await platform_health.check_module_run_as(None, uuid.uuid4())
    assert refused.value.status_code == 400
    assert refused.value.detail["code"] == run_as.RUN_AS_INACTIVE


@pytest.mark.parametrize(
    ("configured", "reachable", "expected"),
    [(None, True, "not_set"), ("user", False, "no_access"), ("user", True, "ok")],
)
async def test_the_card_state(monkeypatch, configured, reachable, expected):
    cti = uuid.uuid4()
    user = uuid.uuid4() if configured else None

    async def _configured(db):
        return user

    monkeypatch.setattr(run_as, "configured_run_as", _configured)
    _patch_access(monkeypatch, active=True, integrations={"opencti": [cti]}, reachable={cti} if reachable else set())
    assert await run_as.run_as_state(None, "opencti") == expected
