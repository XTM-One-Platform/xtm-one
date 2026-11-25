# SPDX-License-Identifier: Apache-2.0
"""CTEM per-user stage runs: module account preflight (#2042)."""

from __future__ import annotations

import pytest

from app.services import ctem_module_preflight as pf


@pytest.fixture(autouse=True)
def _clear_cache():
    pf.forget()
    yield
    pf.forget()


def _resolver(available: set[str]):
    async def _resolve(module: str):
        return (f"integ-{module}", f"https://{module}.example", "tok") if module in available else None

    return _resolve


def _prober(states: dict[str, str], calls: list[str]):
    async def _probe(module: str, base_url: str, token: str):
        calls.append(module)
        return states[module]

    return _probe


async def test_no_account_on_either_module_skips_the_run():
    calls: list[str] = []
    verdict = await pf.preflight(
        pf.ANY_CTEM_MODULE,
        "u1",
        _resolver({"opencti", "openaev"}),
        _prober({"opencti": "missing", "openaev": "missing"}, calls),
    )
    assert not verdict.proceed
    assert verdict.missing == ("opencti", "openaev")
    assert verdict.summary.startswith(pf.NO_MODULE_ACCOUNT)
    assert "OpenCTI and OpenAEV" in verdict.summary
    assert "Sign in to OpenCTI and OpenAEV once" in verdict.summary


async def test_opencti_only_a_stage_needing_both_names_openaev():
    need = (("opencti",), ("openaev",))
    verdict = await pf.preflight(
        need, "u1", _resolver({"opencti", "openaev"}), _prober({"opencti": "present", "openaev": "missing"}, [])
    )
    assert not verdict.proceed
    assert verdict.missing == ("openaev",)


async def test_opencti_only_a_stage_needing_opencti_runs():
    verdict = await pf.preflight((("opencti",),), "u1", _resolver({"opencti"}), _prober({"opencti": "present"}, []))
    assert verdict.proceed


async def test_opencti_only_any_module_stage_runs():
    verdict = await pf.preflight(
        pf.ANY_CTEM_MODULE,
        "u1",
        _resolver({"opencti", "openaev"}),
        _prober({"opencti": "present", "openaev": "missing"}, []),
    )
    assert verdict.proceed
    assert verdict.states == {"opencti": "present", "openaev": "missing"}


async def test_unknown_state_fails_open():
    verdict = await pf.preflight(
        pf.ANY_CTEM_MODULE,
        "u1",
        _resolver({"opencti", "openaev"}),
        _prober({"opencti": "unknown", "openaev": "missing"}, []),
    )
    assert verdict.proceed


async def test_no_credential_is_unknown_and_fails_open():
    verdict = await pf.preflight(pf.ANY_CTEM_MODULE, "u1", _resolver(set()), _prober({}, []))
    assert verdict.proceed
    assert verdict.states == {"opencti": "unknown", "openaev": "unknown"}


async def test_answers_are_cached_per_member():
    calls: list[str] = []
    probe = _prober({"opencti": "missing", "openaev": "missing"}, calls)
    await pf.preflight(pf.ANY_CTEM_MODULE, "u1", _resolver({"opencti", "openaev"}), probe)
    await pf.preflight(pf.ANY_CTEM_MODULE, "u1", _resolver({"opencti", "openaev"}), probe)
    assert calls == ["opencti", "openaev"]
    pf.forget("u1")
    await pf.preflight(pf.ANY_CTEM_MODULE, "u1", _resolver({"opencti", "openaev"}), probe)
    assert len(calls) == 4


@pytest.mark.parametrize(
    ("module", "status", "body", "expected"),
    [
        ("opencti", 401, None, "missing"),
        ("opencti", 200, {"data": {"me": {"id": "x"}}}, "present"),
        ("opencti", 200, {"errors": [{"message": "You must be logged in to do this."}]}, "missing"),
        ("opencti", 200, {"errors": [{"message": "boom", "extensions": {"code": "AUTH_REQUIRED"}}]}, "missing"),
        ("opencti", 502, None, "unknown"),
        ("openaev", 403, None, "missing"),
        ("openaev", 401, None, "missing"),
        ("openaev", 200, {"user_id": "x"}, "present"),
        ("openaev", 200, "html", "unknown"),
        ("openaev", 500, None, "unknown"),
    ],
)
def test_classify_status(module, status, body, expected):
    assert pf.classify_status(module, status, body) == expected


def test_stage_need_is_for_seeded_stage_agents_only():
    assert pf.stage_need("ctem-validation-analyst", is_builtin=True) == pf.ANY_CTEM_MODULE
    assert pf.stage_need("exposure-briefing-analyst", is_builtin=True) == pf.ANY_CTEM_MODULE
    assert pf.stage_need("ctem-validation-analyst", is_builtin=False) is None
    assert pf.stage_need("some-agent", is_builtin=True) is None


def test_stage_needs_cover_every_ctem_stage_slug():
    from app.services.ctem_verticals import CTEM_STAGE_AGENT_SLUGS

    assert set(CTEM_STAGE_AGENT_SLUGS.values()) <= set(pf.STAGE_NEEDS)


def test_module_of_tool():
    assert pf.module_of_tool("search_opencti_entities") == "opencti"
    assert pf.module_of_tool("list_openaev_scenarios") == "openaev"
    assert pf.module_of_tool("web_fetch") is None
