"""Default / Customized / Reset on the built-in ROI values (#9836 R3, ``dev-docs/roi-outcomes.md`` 10.4).

Every built-in unit, rate and built-in agent declaration answers its origin
(``default`` or ``customized``, ``custom`` for the organization's own value),
who customized it and when, and the seed's own value beside the effective
one. A customization is a row the seed never writes, so a seed sync never
overwrites it and its value wins over the seeded ``x-xtm-roi``.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.agents import seed
from app.agents.builtin_roi_units import BUILTIN_ROI_UNITS, with_builtin_roi_unit
from app.api.v1 import roi as roi_api
from app.services import roi, roi_records

ADMIN_ID = uuid.uuid4()
WHEN = datetime(2026, 11, 8, 0, 10, tzinfo=UTC)


def test_no_row_is_the_default():
    assert roi_records.origin_fields(None, {}, has_default=True) == {
        "origin": "default",
        "customized_by": None,
        "customized_at": None,
    }


def test_a_row_over_a_default_is_customized_with_who_and_when():
    row = SimpleNamespace(updated_by_user_id=ADMIN_ID, updated_at=WHEN)
    fields = roi_records.origin_fields(row, {ADMIN_ID: "Ada Admin"}, has_default=True)
    assert fields == {"origin": "customized", "customized_by": "Ada Admin", "customized_at": WHEN.isoformat()}


def test_a_row_without_a_default_is_custom():
    row = SimpleNamespace(updated_by_user_id=None, updated_at=None)
    assert roi_records.origin_fields(row, {}, has_default=False)["origin"] == "custom"


def test_the_baseline_default_is_the_catalog_value():
    base = roi.BASELINES_BY_UNIT["threat_report_triaged"]
    default = roi_records.baseline_default("threat_report_triaged")
    assert default is not None
    assert (default["manual_min_low"], default["manual_min_likely"], default["manual_min_high"]) == (
        base.manual_min_low,
        base.manual_min_likely,
        base.manual_min_high,
    )
    assert default["review_min"] == base.review_min
    assert roi_records.baseline_default("contract_reviewed_custom") is None


def test_an_overridden_baseline_answers_customized_and_its_default():
    base = roi.BASELINES_BY_UNIT["cve_assessed"]
    origins = {"cve_assessed": {"origin": "customized", "customized_by": "Ada Admin", "customized_at": "t"}}
    answer = roi_api._baseline_dict(base, True, origins)
    assert answer["origin"] == "customized"
    assert answer["customized_by"] == "Ada Admin"
    assert answer["default"]["manual_min_likely"] == base.manual_min_likely
    # The field the settings table read before R3 is kept.
    assert answer["overridden"] is True


def test_a_baseline_without_override_answers_default():
    answer = roi_api._baseline_dict(roi.BASELINES_BY_UNIT["cve_assessed"], False, {})
    assert answer["origin"] == "default"
    assert answer["customized_by"] is None


def test_a_rate_over_a_builtin_role_is_customized_with_its_default():
    builtin = roi.HOURLY_RATES["analyst"]["EUR"]
    row = SimpleNamespace(
        role="analyst", currency="EUR", amount=95.0, source="", updated_by_user_id=ADMIN_ID, updated_at=WHEN
    )
    answer = roi_records.rate_dict(row, {ADMIN_ID: "Ada Admin"})
    assert answer["origin"] == "customized"
    assert answer["default"] == {"amount": builtin.amount, "label": builtin.label, "source": builtin.source}
    assert answer["amount"] == 95.0
    assert answer["builtin_role"] is True


def test_a_rate_of_a_custom_role_is_custom():
    row = SimpleNamespace(
        role="legal", currency="EUR", amount=120.0, source="", updated_by_user_id=None, updated_at=None
    )
    answer = roi_records.rate_dict(row)
    assert answer["origin"] == "custom"
    assert answer["default"] is None


class _AgentDb:
    def __init__(self, agent: Any) -> None:
        self.agent = agent

    async def get(self, _model: Any, _id: Any) -> Any:
        return self.agent


async def test_a_builtin_agent_declares_its_seeded_unit_as_default():
    slug, declaration = next(iter(BUILTIN_ROI_UNITS.items()))
    db = _AgentDb(SimpleNamespace(is_builtin=True, slug=slug))
    default = await roi_api._declaration_default(db, "agent", uuid.uuid4())
    assert default == {
        "unit": declaration.unit,
        "count_mode": roi_records.COUNT_PER_OUTPUT_ITEM,
        "count_path": declaration.count_path,
        "units_per_count": 1,
    }


async def test_a_custom_agent_or_another_subject_has_no_default():
    slug = next(iter(BUILTIN_ROI_UNITS))
    custom_agent = _AgentDb(SimpleNamespace(is_builtin=False, slug=slug))
    assert await roi_api._declaration_default(custom_agent, "agent", uuid.uuid4()) is None
    assert await roi_api._declaration_default(_AgentDb(None), "agent", uuid.uuid4()) is None
    assert await roi_api._declaration_default(_AgentDb(None), "fleet", uuid.uuid4()) is None


async def test_an_uncustomized_builtin_agent_answers_its_shipped_declaration(monkeypatch):
    """UX round 1 B2 of #9914: no row, built-in agent -> the shipped declaration, origin default."""
    slug, declaration = next(iter(BUILTIN_ROI_UNITS.items()))
    sid = uuid.uuid4()

    async def manageable(*_a: Any) -> uuid.UUID:
        return sid

    async def no_row(*_a: Any) -> None:
        return None

    monkeypatch.setattr(roi_api, "_readable_subject", manageable)
    monkeypatch.setattr(roi_records, "get_declaration", no_row)
    db = _AgentDb(SimpleNamespace(is_builtin=True, slug=slug))
    answer = await roi_api.get_roi_declaration(subject_type="agent", subject_id=str(sid), db=db, user=None)
    assert answer is not None
    assert answer["origin"] == "default"
    assert (answer["unit"], answer["count_path"]) == (declaration.unit, declaration.count_path)
    assert answer["default"]["unit"] == declaration.unit
    # A custom agent with no row still declares nothing.
    custom = _AgentDb(SimpleNamespace(is_builtin=False, slug=slug))
    assert await roi_api.get_roi_declaration(subject_type="agent", subject_id=str(sid), db=custom, user=None) is None


def test_a_reader_reads_the_declaration_and_only_a_manager_changes_it():
    """UX round 2 B1 of #9914: GET follows the subject's visibility, PUT and DELETE its manage rule."""
    assert "_readable_subject(" in inspect.getsource(roi_api.get_roi_declaration)
    assert "_manageable_subject(" not in inspect.getsource(roi_api.get_roi_declaration)
    for write in (roi_api.put_roi_declaration, roi_api.delete_roi_declaration):
        assert "_manageable_subject(" in inspect.getsource(write)


async def test_an_invisible_subject_is_not_found(monkeypatch):
    """A subject the caller cannot see answers 404, never its declaration."""
    from app.api.v1 import helpers

    async def groups(*_a: Any) -> list[str]:
        return []

    class _NothingVisible:
        async def execute(self, *_a: Any) -> Any:
            return SimpleNamespace(scalar=lambda: None)

    monkeypatch.setattr(helpers, "get_user_group_ids", groups)
    user = SimpleNamespace(id=uuid.uuid4(), role="user")
    with pytest.raises(HTTPException) as refused:
        await roi_api._readable_subject(_NothingVisible(), user, "agent", str(uuid.uuid4()))  # type: ignore[arg-type]
    assert refused.value.status_code == 404


def test_a_customized_builtin_declaration_wins_over_the_reseeded_schema():
    """Customize, run the seed again: the seed rewrites x-xtm-roi, the stored row still wins."""
    slug, declaration = next(iter(BUILTIN_ROI_UNITS.items()))
    reseeded = with_builtin_roi_unit(slug, {"type": "object", "properties": {}})
    customized = SimpleNamespace(unit="ttp_extraction", count_mode="per_run", count_path=None, units_per_count=3)
    picked = roi_records.pick_declaration(assignment=None, agent=customized, output_schema=reseeded, fleet=None)
    assert picked is not None
    assert (picked.unit, picked.count_mode, picked.units_per_count) == ("ttp_extraction", "per_run", 3)
    assert picked.source == roi_records.SOURCE_AGENT
    # Reset to default (the row deleted): the seeded declaration counts again.
    reset = roi_records.pick_declaration(assignment=None, agent=None, output_schema=reseeded, fleet=None)
    assert reset is not None
    assert (reset.unit, reset.count_path) == (declaration.unit, declaration.count_path)


def test_the_seed_never_writes_a_customization():
    """The seed sync writes only output_schema: overrides, rates and declarations stay the organization's."""
    source = inspect.getsource(seed)
    for model in ("RoiDeclaration", "RoiBaselineOverride", "RoiRate", "roi_declarations", "roi_baseline_overrides"):
        assert model not in source
