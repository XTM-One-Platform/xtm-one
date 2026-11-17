"""Every ROI write logs an audit row the ``audit_logs`` table accepts (issue #9394).

``audit_logs.entity_id`` is a UUID column and ``services.audit.audit_entry``
converts what it is given with ``uuid.UUID``: a composite key such as
``"agent:<id>"`` or ``"analyst:EUR"`` raised ``ValueError`` after the row was
written, and ``PUT /roi/declarations`` answered 500 to an administrator.
Each route is called with its services stubbed and the real ``audit_entry``
building the row, so a non-UUID ``entity_id`` fails here as it failed in the
lab.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.v1 import roi as roi_api
from app.services import audit

ADMIN = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com", role="admin", is_admin=True)


class _Db:
    async def commit(self) -> None:
        return None

    async def refresh(self, _row: Any) -> None:
        return None


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The audit rows the routes build, through the real ``audit_entry``."""
    rows: list[Any] = []

    async def log_audit(_db: Any, **kw: Any) -> Any:
        row = audit.audit_entry(**kw)
        rows.append(row)
        return row

    monkeypatch.setattr(roi_api, "log_audit", log_audit)
    return rows


def _declaration(subject_type: str, sid: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        subject_type=subject_type,
        subject_id=sid,
        unit="threat_report_triaged",
        count_mode="per_run",
        count_path=None,
        units_per_count=1,
        updated_at=None,
    )


async def test_a_declaration_is_audited_on_its_subject_id(monkeypatch: pytest.MonkeyPatch, logged: list[Any]):
    sid = uuid.uuid4()

    async def manageable(_db, _user, _type, subject_id):
        return uuid.UUID(subject_id)

    async def put_declaration(_db, **kw):
        return _declaration(kw["subject_type"], kw["subject_id"])

    async def delete_declaration(_db, _type, _sid):
        return True

    async def no_default(_db, _type, _sid):
        return None

    async def user_names(_db, _ids):
        return {}

    monkeypatch.setattr(roi_api, "_manageable_subject", manageable)
    monkeypatch.setattr(roi_api, "_declaration_default", no_default)
    monkeypatch.setattr(roi_api.roi_records, "user_names", user_names)
    monkeypatch.setattr(roi_api.roi_records, "put_declaration", put_declaration)
    monkeypatch.setattr(roi_api.roi_records, "delete_declaration", delete_declaration)

    body = roi_api.DeclarationRequest(unit="threat_report_triaged", count_mode="per_run")
    answer = await roi_api.put_roi_declaration(body, subject_type="agent", subject_id=str(sid), db=_Db(), user=ADMIN)
    assert answer["subject_id"] == str(sid)
    response = await roi_api.delete_roi_declaration(subject_type="agent", subject_id=str(sid), db=_Db(), user=ADMIN)
    assert response.status_code == 204

    assert [row.entity_id for row in logged] == [sid, sid]
    assert all(row.details["subject_type"] == "agent" for row in logged)


async def test_rates_baselines_and_units_carry_their_key_in_the_details(
    monkeypatch: pytest.MonkeyPatch, logged: list[Any]
):
    rate = SimpleNamespace(role="analyst", currency="EUR")
    unit = SimpleNamespace(key="contract_reviewed", name="Contract reviewed", version=2)

    async def put_rate(_db, **_kw):
        return rate

    async def delete_rate(_db, **_kw):
        return True

    async def upsert_override(_db, **_kw):
        return None

    async def delete_override(_db, _unit):
        return True

    async def effective_baselines(_db):
        return {"cve_assessed": SimpleNamespace()}, set()

    async def create_unit(_db, **_kw):
        return unit

    async def baseline_origins(_db):
        return {}

    async def user_names(_db, _ids):
        return {}

    async def update_unit(_db, _key, **_kw):
        return unit

    async def archive_unit(_db, _key, **_kw):
        return unit

    for name, fake in {
        "put_rate": put_rate,
        "delete_rate": delete_rate,
        "upsert_override": upsert_override,
        "delete_override": delete_override,
        "effective_baselines": effective_baselines,
        "baseline_origins": baseline_origins,
        "user_names": user_names,
        "create_unit": create_unit,
        "update_unit": update_unit,
        "archive_unit": archive_unit,
        "rate_dict": lambda row, _names=None: {"role": row.role},
        "unit_snapshot": lambda row: {"key": row.key},
    }.items():
        monkeypatch.setattr(roi_api.roi_records, name, fake, raising=False)
    monkeypatch.setattr(roi_api, "_baseline_dict", lambda *_a: {})

    await roi_api.put_roi_rate("analyst", roi_api.RateRequest(currency="EUR", amount=90), db=_Db(), admin=ADMIN)
    await roi_api.delete_roi_rate("analyst", currency="eur", db=_Db(), admin=ADMIN)
    override = roi_api.BaselineOverrideRequest(manual_min_low=1, manual_min_likely=2, manual_min_high=3, review_min=1)
    await roi_api.put_roi_baseline("cve_assessed", override, db=_Db(), admin=ADMIN)
    await roi_api.delete_roi_baseline("cve_assessed", db=_Db(), admin=ADMIN)
    fields = {
        "name": "Contract reviewed",
        "manual_min_low": 10,
        "manual_min_likely": 20,
        "manual_min_high": 30,
        "review_min": 5,
    }
    await roi_api.create_roi_unit(roi_api.UnitCreateRequest(key="contract_reviewed", **fields), db=_Db(), admin=ADMIN)
    await roi_api.update_roi_unit("contract_reviewed", roi_api.UnitRequest(**fields), db=_Db(), admin=ADMIN)
    await roi_api.archive_roi_unit("contract_reviewed", db=_Db(), admin=ADMIN)

    assert [row.entity_type for row in logged] == [
        "roi_rate",
        "roi_rate",
        "roi_baseline",
        "roi_baseline",
        "roi_unit",
        "roi_unit",
        "roi_unit",
    ]
    assert all(row.entity_id is None for row in logged)
    assert logged[0].details["role"] == "analyst"
    assert logged[1].details == {"role": "analyst", "currency": "EUR"}
    assert logged[2].details["unit"] == "cve_assessed"
    assert logged[3].details == {"unit": "cve_assessed"}
    assert logged[4].details["key"] == "contract_reviewed"
    assert logged[5].details["key"] == "contract_reviewed"
    assert logged[6].details == {"key": "contract_reviewed"}


def test_no_roi_route_passes_a_composite_entity_id():
    """A static guard: the shape that answered 500 never comes back."""
    import inspect

    source = inspect.getsource(roi_api)
    assert 'entity_id=f"' not in source
    assert "entity_id=unit" not in source
