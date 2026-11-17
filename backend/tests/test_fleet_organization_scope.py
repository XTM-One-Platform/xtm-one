"""Organization scope of fleets and members (R3 of #9162, dev-docs/ctem-fleets.md 10.3).

Unit tests of the resolver and the validation; no database.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.fleet import FLEET_ROLE_CONNECTED, FLEET_ROLE_MEMBER
from app.services.fleets import (
    FleetActionRefusedError,
    effective_member_settings,
    normalize_organization_scope,
    resolve_organization_scope,
)

ORG_A = str(uuid.uuid4())
ORG_B = str(uuid.uuid4())


def _fleet(scope=None):
    return SimpleNamespace(
        default_context_inclusion="summary",
        default_collaboration_instruction="Ask first.",
        organization_scope=scope,
    )


def _row(role=FLEET_ROLE_CONNECTED, scope=None):
    return SimpleNamespace(role=role, context_inclusion=None, collaboration_instruction=None, organization_scope=scope)


def test_an_existing_fleet_reads_the_primary_as_before():
    """No scope stored anywhere: the primary, so every run reads what it read before R3."""
    settings = effective_member_settings(_fleet(None), _row(FLEET_ROLE_MEMBER))
    assert settings.organization_scope == {"mode": "primary"}
    assert "organization_scope" not in settings.overridden


def test_a_member_inherits_the_fleet_scope():
    settings = effective_member_settings(_fleet({"mode": "monitored"}), _row(FLEET_ROLE_MEMBER))
    assert settings.organization_scope == {"mode": "monitored"}
    assert settings.overridden == ()


def test_a_home_member_may_override_its_scope():
    row = _row(FLEET_ROLE_MEMBER, {"mode": "list", "organization_ids": [ORG_A]})
    settings = effective_member_settings(_fleet({"mode": "monitored"}), row)
    assert settings.organization_scope == {"mode": "list", "organization_ids": [ORG_A]}
    assert settings.overridden == ("organization_scope",)


def test_a_connected_row_override_keeps_the_other_settings_inherited():
    settings = effective_member_settings(_fleet(None), _row(scope={"mode": "monitored"}))
    assert settings.context_inclusion == "summary"
    assert settings.collaboration_instruction == "Ask first."
    assert settings.overridden == ("organization_scope",)


def test_an_unreadable_stored_scope_falls_back_to_the_fleet_then_the_primary():
    assert resolve_organization_scope({"mode": "monitored"}, {"mode": "bogus"}) == {"mode": "monitored"}
    assert resolve_organization_scope({"mode": "list", "organization_ids": []}, None) == {"mode": "primary"}


def test_normalize_drops_duplicates_and_keeps_order():
    got = normalize_organization_scope({"mode": "LIST", "organization_ids": [ORG_B, ORG_A, ORG_B]})
    assert got == {"mode": "list", "organization_ids": [ORG_B, ORG_A]}
    assert normalize_organization_scope({"mode": "monitored", "organization_ids": [ORG_A]}) == {"mode": "monitored"}


@pytest.mark.parametrize(
    "value",
    [
        None,
        "primary",
        {"mode": "everyone"},
        {"mode": "list"},
        {"mode": "list", "organization_ids": []},
        {"mode": "list", "organization_ids": ["not-an-id"]},
        {"mode": "list", "organization_ids": [str(uuid.uuid4()) for _ in range(51)]},
    ],
)
def test_normalize_refuses_a_malformed_scope(value):
    with pytest.raises(FleetActionRefusedError) as exc:
        normalize_organization_scope(value)
    assert exc.value.status_code == 422
