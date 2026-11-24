# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Copilot mode keeps the organization profile to its identity (#1690).

Copilot has no CTEM loop: the summary, the missing fields, the categories,
the onboarding and the proposals name the identity alone there, and XTM One
mode keeps the whole contract. Stored data is never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import organization_context as org_context
from app.services import organization_context_categories as cats
from app.services import organization_onboarding as onboarding
from app.services.organization_context_injection import render_summary

FULL_PROFILE = {
    "name": "Acme",
    "sector": "Finance",
    "regions": ["FR"],
    "size": "51-250",
    "primary_assets": [],
    "currency": "EUR",
    "loaded_hourly_rate": 80,
    "risk_appetite": {"annual_loss_threshold": 100000, "approval_threshold": 200000},
}


def _row(profile: dict) -> SimpleNamespace:
    return SimpleNamespace(
        profile=profile, version=3, created_at=datetime(2026, 11, 24, tzinfo=UTC), source="manual", organization_id=None
    )


@pytest.fixture
def copilot(monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", "copilot")


@pytest.fixture
def xtm_one(monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")


def test_copilot_contract_is_identity(copilot):
    assert {path for path, _label in org_context.contract_fields()} == {"name", "sector", "regions", "size"}
    assert org_context.missing_fields(org_context.parse_profile(FULL_PROFILE)) == []


def test_xtm_one_contract_is_whole(xtm_one):
    assert org_context.contract_fields() == org_context.CONTRACT_FIELDS
    missing = org_context.missing_fields(org_context.parse_profile(FULL_PROFILE))
    assert "primary_assets" in missing and "ticketing" in missing


def test_copilot_render_for_agent_names_identity_only(copilot):
    text = org_context.render_for_agent(_row(FULL_PROFILE), ctem_work=False)
    assert "Fields not filled: none." in text
    assert "Acme" in text
    for absent in ("currency", "loaded_hourly_rate", "risk_appetite", "primary_assets"):
        assert absent not in text


def test_copilot_summary_has_no_category_index(copilot):
    text = render_summary(_row(FULL_PROFILE), ctem_work=False)
    assert "Categories:" not in text
    assert "EUR" not in text and "risk_appetite" not in text
    assert "Not filled: none." in text


def test_xtm_one_summary_keeps_the_index(xtm_one):
    text = render_summary(_row(FULL_PROFILE), ctem_work=True)
    assert "Categories:" in text
    assert "EUR" in text


def test_copilot_lists_no_category(copilot):
    assert cats.available() == ()
    assert "No organization context category" in cats.render_categories({})
    # A stored selection is kept as it is: switching the mode never loses it.
    assert cats.normalize_categories(["software"]) == ["software"]


def test_xtm_one_lists_every_category(xtm_one):
    assert cats.available_names() == cats.CATEGORY_NAMES


def test_copilot_onboarding_completes_on_identity(copilot):
    step = onboarding.render_step(org_context.parse_profile(FULL_PROFILE), version=3)
    assert "Completeness: 4 of 4 fields filled." in step
    assert "The onboarding is complete" in step
    with pytest.raises(onboarding.OnboardingError):
        onboarding.apply_answers(org_context.parse_profile(FULL_PROFILE), {"currency": "USD"})


def test_copilot_proposals_take_identity_only(copilot):
    from app.services.organization_context_proposals import _contract_paths

    assert set(_contract_paths("primary")) == {"name", "sector", "regions", "size"}
