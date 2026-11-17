"""The quota the embedded chat shows.

``GET /platform/chat/quota`` must show the numbers the web chat's
``QuotaIndicator`` shows: two surfaces of one quota that disagree would each
look like a bug in the other. These pin the reading case by case, check it
against what ``GET /settings/my-quotas`` (the indicator's own source) serves
for the same stored state, and pin the sandbox merge against the table
the frontend twin is checked against. The prompt list and the message
feedback, which are about SQL, are pinned against PostgreSQL in
``test_platform_chat_extras_postgres.py``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api import deps
from app.api.v1 import platform as platform_api
from app.api.v1 import settings as settings_api
from app.core import redis as redis_module
from app.core.database import get_db
from app.services import platform_chat_extras as extras
from app.services import quota as quota_module

_FIXTURE = (
    Path(__file__).resolve().parents[2] / "frontend" / "tests_e2e" / "fixtures" / "agentic-quota-count-cases.json"
)


def _entry(usage_type: str, count: int, *, model: str | None = None, scope: str | None = "user") -> dict:
    entry: dict = {"usage_type": usage_type, "model": model, "period": "2026-09", "count": count}
    if scope is not None:
        entry["scope"] = scope
    return entry


# ---------------------------------------------------------------------------
# The sandbox merge, against the frontend twin's table
# ---------------------------------------------------------------------------


def _fixture_cases() -> list[dict]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _fixture_cases(), ids=lambda case: case["name"])
def test_the_merge_matches_the_frontend_twin(case):
    """``mergeSandboxIntoAgenticCount`` and its backend twin read one table."""
    usage = case["usage"]
    total = extras.agentic_total_entry(usage)
    assert extras.merge_sandbox_into_agentic_count(usage, total) == case["expected"]


def test_the_fixture_covers_both_scopes_and_the_sandbox():
    """A table that lost its global or sandbox case would pin nothing."""
    usages = [entry for case in _fixture_cases() for entry in case["usage"]]
    assert any(entry.get("scope") == "global" for entry in usages)
    assert any(entry["usage_type"] == "sandbox" for entry in usages)
    assert any("scope" not in entry for entry in usages)


# ---------------------------------------------------------------------------
# resolve_platform_chat_quota
# ---------------------------------------------------------------------------


# The per-period rows both payloads carry; their reading is pinned in
# ``test_agentic_quota_periods.py``, here only that both surfaces serve them.
_PERIODS = [
    {"period": "daily", "used": 3, "limit": 100, "scope": "user", "resets_at": "2026-09-02T00:00:00Z"},
    {"period": "monthly", "used": 17, "limit": None, "scope": "user", "resets_at": "2026-10-01T00:00:00Z"},
    {"period": "yearly", "used": 40, "limit": None, "scope": "global", "resets_at": "2027-01-01T00:00:00Z"},
]


def _stub_quota_state(monkeypatch, *, resolved: dict, usage: list[dict], licensed: bool = True) -> list[str]:
    """One stored quota state, seen alike by the service and by ``/my-quotas``."""
    calls: list[str] = []

    async def _is_licensed(_db):
        return licensed

    async def _redis():
        return SimpleNamespace()

    async def _resolve(_db, _redis, _user_id):
        calls.append("resolve")
        return dict(resolved), {"agentic": "global"}

    async def _usage(_db, _user_id):
        calls.append("usage")
        return [dict(entry) for entry in usage]

    async def _periods(_db, _user_id):
        calls.append("periods")
        return [dict(row) for row in _PERIODS]

    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _is_licensed)
    for module in (extras, redis_module):
        monkeypatch.setattr(module, "redis_client", _redis)
    for module in (extras, quota_module):
        monkeypatch.setattr(module, "resolve_full_quota_info", _resolve)
        monkeypatch.setattr(module, "get_user_quota_usage", _usage)
        monkeypatch.setattr(module, "get_agentic_quota_periods", _periods)
    return calls


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), email="user@example.com", role="user", is_admin=False)


def _quota(monkeypatch, **state) -> dict | None:
    _stub_quota_state(monkeypatch, **state)
    return asyncio.run(extras.resolve_platform_chat_quota(SimpleNamespace(), _user()))  # type: ignore[arg-type]


def _indicator(monkeypatch, **state) -> dict:
    """What ``QuotaIndicator`` (``pages/chat/QuotaIndicator.tsx``) shows over ``/my-quotas``.

    A transliteration of the component's reading: the resolved agentic
    ``default`` and ``period`` (``"monthly"`` when none resolves), the
    current-period agentic total (``usage_type === "agentic" && model === null``)
    merged with the sandbox slice, and ``scope === "global"`` for the shared pool.
    """
    _stub_quota_state(monkeypatch, **state)
    info = asyncio.run(settings_api.get_my_quotas(user=_user(), db=SimpleNamespace())).model_dump()  # type: ignore[arg-type]
    agentic = (info["resolved_config"] or {}).get("agentic")
    entry = next((u for u in info["usage"] if u["usage_type"] == "agentic" and u["model"] is None), None)
    return {
        "used": extras.merge_sandbox_into_agentic_count(info["usage"], entry),
        "limit": agentic["default"] if agentic else None,
        "period": agentic["period"] if agentic else "monthly",
        "scope": "global" if entry is not None and entry["scope"] == "global" else "user",
        "periods": info["periods"],
    }


_STATES = {
    "per-user limit": (
        {
            "resolved": {"agentic": {"default": 100, "period": "daily"}},
            "usage": [
                _entry("agentic", 12),
                _entry("sandbox", 5),
                _entry("agentic", 3, model="gpt-5"),
                _entry("llm_proxy", 40),
            ],
        },
        {"used": 17, "limit": 100, "period": "daily", "scope": "user"},
    ),
    "unlimited, nothing resolved": (
        {"resolved": {}, "usage": [_entry("agentic", 4)]},
        {"used": 4, "limit": None, "period": "monthly", "scope": "user"},
    ),
    "explicitly unlimited": (
        {
            "resolved": {"agentic": {"default": None, "unlimited": True, "period": "monthly"}},
            "usage": [_entry("agentic", 4), _entry("sandbox", 2)],
        },
        {"used": 6, "limit": None, "period": "monthly", "scope": "user"},
    ),
    "a zero limit is a limit": (
        {"resolved": {"agentic": {"default": 0}}, "usage": []},
        {"used": 0, "limit": 0, "period": "monthly", "scope": "user"},
    ),
    "shared global pool": (
        {
            "resolved": {"agentic": {"default": 300, "period": "monthly"}},
            "usage": [_entry("agentic", 40, scope="global"), _entry("sandbox", 9, scope="global")],
        },
        {"used": 40, "limit": 300, "period": "monthly", "scope": "global"},
    ),
    "sandbox alone": (
        {"resolved": {"agentic": {"default": 50, "period": "monthly"}}, "usage": [_entry("sandbox", 6)]},
        {"used": 6, "limit": 50, "period": "monthly", "scope": "user"},
    ),
    "an entry without a scope is per-user": (
        {"resolved": {"agentic": {"default": 10}}, "usage": [_entry("agentic", 7, scope=None)]},
        {"used": 7, "limit": 10, "period": "monthly", "scope": "user"},
    ),
    "only the llm proxy has a quota": (
        {"resolved": {"llm_proxy": {"default": 1000}}, "usage": [_entry("agentic", 2)]},
        {"used": 2, "limit": None, "period": "monthly", "scope": "user"},
    ),
}


@pytest.mark.parametrize(("state", "expected"), list(_STATES.values()), ids=list(_STATES))
def test_the_quota_reads_the_stored_state(monkeypatch, state, expected):
    assert _quota(monkeypatch, **state) == {**expected, "periods": _PERIODS}


@pytest.mark.parametrize("state", [state for state, _ in _STATES.values()], ids=list(_STATES))
def test_the_quota_is_the_one_the_web_chat_shows(monkeypatch, state):
    """Same stored state, same numbers as ``QuotaIndicator`` over ``/my-quotas``."""
    assert _quota(monkeypatch, **state) == _indicator(monkeypatch, **state)


def test_community_edition_has_no_quota_to_show(monkeypatch):
    """No licence, no indicator: ``None``, and the counters are never read."""
    calls = _stub_quota_state(
        monkeypatch,
        resolved={"agentic": {"default": 100}},
        usage=[_entry("agentic", 12)],
        licensed=False,
    )
    assert asyncio.run(extras.resolve_platform_chat_quota(SimpleNamespace(), _user())) is None  # type: ignore[arg-type]
    assert calls == []


# ---------------------------------------------------------------------------
# GET /platform/chat/quota
# ---------------------------------------------------------------------------


async def _get_quota(monkeypatch, answer: dict | None) -> httpx.Response:
    async def _resolve(_db, _user):
        return answer

    monkeypatch.setattr(platform_api, "resolve_platform_chat_quota", _resolve)
    app = FastAPI()
    app.include_router(platform_api.router, prefix="/api/v1/platform")

    async def _get_db():
        yield SimpleNamespace()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.get_current_user] = _user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/v1/platform/chat/quota")


async def test_the_route_serves_the_quota(monkeypatch):
    quota = {"used": 17, "limit": 100, "period": "daily", "scope": "user", "periods": _PERIODS}
    resp = await _get_quota(monkeypatch, quota)
    assert resp.status_code == 200
    assert resp.json() == quota


async def test_the_route_serves_null_without_a_licence(monkeypatch):
    resp = await _get_quota(monkeypatch, None)
    assert resp.status_code == 200
    assert resp.json() is None


async def test_the_route_keeps_an_unlimited_quota_explicit(monkeypatch):
    """``limit: null`` is part of the shape, never dropped; ``periods`` defaults to none."""
    resp = await _get_quota(monkeypatch, {"used": 3, "limit": None, "period": "monthly", "scope": "global"})
    assert resp.json() == {"used": 3, "limit": None, "period": "monthly", "scope": "global", "periods": []}
