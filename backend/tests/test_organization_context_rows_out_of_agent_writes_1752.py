"""Slice 2 of #1752: the agent tools and the AI draft carry profile fields, never the asset rows.

The asset records own the asset and group rows (#1543), each with its own lock
and history; ``update_organization_context`` and
``propose_organization_context_change`` refuse a row field and name the row
tool, an accepted proposal stored before #1752 never applies one through the
profile, and the AI draft prompt sends counts instead of the rows, so its size
never grows with the inventory. No database: the pure functions.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import organization_context as org_context
from app.services import organization_context_ai_draft as ai
from app.services import organization_context_proposals as proposals

PRIMARY = SimpleNamespace(role="primary")


@pytest.mark.parametrize("field", ["assets_exposure", "asset_groups", "ai_systems"])
def test_a_row_field_change_is_refused_with_the_row_tool_named(field: str) -> None:
    with pytest.raises(proposals.ProposalError, match="upsert_organization_rows") as caught:
        proposals.validate_field_changes(PRIMARY, {}, [{"field": field, "value": [{"name": "host-1"}]}])
    assert f"'{field}' holds rows" in str(caught.value)


def test_the_refusal_names_the_section_that_writes_the_rows() -> None:
    assert "(section assets)" in proposals.row_field_refusal("ai_systems")
    assert "(section asset_groups)" in proposals.row_field_refusal("asset_groups")


def test_a_profile_field_beside_the_rows_is_still_accepted() -> None:
    kept = proposals.validate_field_changes(PRIMARY, {}, [{"field": "sector", "value": "Retail"}])
    assert kept[0]["field"] == "sector"


@pytest.mark.asyncio
async def test_an_old_proposal_naming_a_row_field_is_never_applied_through_the_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import organizations as orgs

    target = SimpleNamespace(id=uuid.uuid4(), role="primary", archived_at=None)

    async def _get(_db: object, _id: object) -> SimpleNamespace:
        return target

    async def _must_not_read(*_args: object, **_kwargs: object) -> None:  # pragma: no cover - the refusal comes first
        raise AssertionError("the profile was read for a row field")

    monkeypatch.setattr(orgs, "get_organization", _get)
    monkeypatch.setattr(org_context, "current_version", _must_not_read)
    stored = SimpleNamespace(
        status="pending",
        kind="fields",
        organization_id=target.id,
        proposals=[{"field": "assets_exposure", "value": [{"name": "host-1"}], "basis": "chat"}],
    )
    with pytest.raises(proposals.ProposalError, match="upsert_organization_rows"):
        await proposals.decide(None, stored, admin_user_id=uuid.uuid4(), accept=["assets_exposure"])  # type: ignore[arg-type]
    assert stored.status == "pending"


def _profile_with_assets(count: int) -> org_context.OrganizationProfile:
    assets = [{"name": f"host-{i}", "category": "host"} for i in range(count)]
    assets.append({"name": "Support bot", "category": "ai_target"})
    return org_context.parse_profile({"name": "Acme", "sector": "Retail", "assets_exposure": assets})


def test_the_ai_draft_prompt_sends_counts_not_rows() -> None:
    data = ai.prompt_profile(_profile_with_assets(50))
    assert "assets_exposure" not in data
    assert "asset_groups" not in data
    assert data["row_counts"]["assets_exposure"] == 51
    assert "ai_systems" not in data
    assert data["sector"] == "Retail"


def test_the_ai_draft_prompt_size_does_not_grow_with_the_inventory() -> None:
    small = len(json.dumps(ai.prompt_profile(_profile_with_assets(10))))
    large = len(json.dumps(ai.prompt_profile(_profile_with_assets(2_000))))
    # Only the digits of the count differ.
    assert large - small < 10


def test_a_drafted_row_field_is_never_proposed() -> None:
    """The draft proposes profile fields; an ``ai_systems`` answer never becomes a profile proposal."""
    current = org_context.parse_profile({"name": "Acme"})
    evidence = ai.Evidence(texts={"description": "x"})
    answer = json.dumps(
        {
            "proposals": [
                {"field": "ai_systems", "value": [{"name": "Bot", "kind": "assistant"}], "source": "description"},
                {"field": "sector", "value": "Retail", "source": "description"},
            ]
        }
    )
    found, not_supported, _pirs, notes = ai.parse_proposals(answer, current, evidence)
    assert [p.field for p in found] == ["sector"]
    assert any("ai_systems" in n for n in notes)
    assert "ai_systems" not in not_supported


def test_the_draft_prompt_never_asks_for_rows() -> None:
    assert "ai_systems:" not in ai._SYSTEM
    assert "ai_systems" not in ai.CONTRACT_PATHS
