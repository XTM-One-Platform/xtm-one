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


# --- Mid-run guard (#2042 slices 2 and 3) ---------------------------------

_OAEV_403 = (
    "Error: OpenAEV access denied while trying to list scenarios - insufficient "
    "permissions or scopes. The user should check the integration permissions."
)
_OCTI_401 = "Error: OpenCTI authentication failed - the token may be expired or revoked."


def test_forbidden_mid_run_stops_calls_to_that_module_only():
    with pf.guarding_module_accounts(True):
        assert pf.refused_module("list_openaev_scenarios") is None
        assert pf.note_tool_refusal("list_openaev_scenarios", _OAEV_403) == "openaev"
        assert pf.refused_module("get_openaev_simulation") == "openaev"
        assert pf.refused_module("search_opencti_entities") is None
        assert "OpenAEV" in pf.refusal_answer("openaev")
        assert pf.NO_MODULE_ACCOUNT in pf.refusal_answer("openaev")
    # The guard is scoped to the run: the next block starts clean.
    with pf.guarding_module_accounts(True):
        assert pf.refused_module("get_openaev_simulation") is None


def test_guard_is_off_outside_a_ctem_stage_run():
    assert pf.note_tool_refusal("list_openaev_scenarios", _OAEV_403) is None
    assert pf.refused_module("list_openaev_scenarios") is None
    with pf.guarding_module_accounts(False):
        assert pf.note_tool_refusal("search_opencti_entities", _OCTI_401) is None
        assert pf.refused_module("search_opencti_entities") is None


@pytest.mark.parametrize(
    ("tool", "content", "expected"),
    [
        ("search_opencti_entities", _OCTI_401, "opencti"),
        ("search_opencti_entities", "GraphQL error: You must be logged in to do this. [AUTH_REQUIRED]", "opencti"),
        # A capability denial is not a missing account: the module stays callable.
        ("add_opencti_label", "GraphQL error: You are not allowed to do this. [FORBIDDEN_ACCESS]", None),
        ("list_openaev_scenarios", "Error: OpenAEV server error (502) while trying to list.", None),
        ("web_fetch", _OAEV_403, None),
    ],
)
def test_only_an_account_refusal_trips_the_guard(tool, content, expected):
    with pf.guarding_module_accounts(True):
        assert pf.note_tool_refusal(tool, content) == expected


def test_expected_refusal_is_recorded_disposed_not_as_a_tool_error(monkeypatch):
    from app.services import frictions
    from app.tools import agent_loop
    from app.tools.base import ToolCall, ToolResult

    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(frictions, "note_tool_error", lambda tool, content: recorded.append(("tool_error", tool)))
    monkeypatch.setattr(
        frictions,
        "note_expected_error",
        lambda tool, content, reason: recorded.append(("expected_error", reason)),
    )
    tc = ToolCall(id="c1", name="list_openaev_scenarios", arguments={})
    with pf.guarding_module_accounts(True):
        agent_loop._log_tool_result(tc, ToolResult(tool_call_id="c1", content=_OAEV_403, is_error=True))
    assert recorded == [("expected_error", f"{pf.NO_MODULE_ACCOUNT}: openaev")]

    recorded.clear()
    agent_loop._log_tool_result(tc, ToolResult(tool_call_id="c1", content=_OAEV_403, is_error=True))
    assert recorded == [("tool_error", "list_openaev_scenarios")]
